
from typing import Any, NamedTuple, Optional, Tuple
import hashlib
import re
import imaplib
import email
import email.message
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr
import logging
from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import desc, func, or_

from app.core.clock import utcnow
from app.core.observability import record_imap_sync
from app.core.tls import open_imap

from app.models.sending_inbox import SendingInbox
from app.models.email_message import EmailMessage, EmailEvent
from app.models.prospect import Prospect
from app.models.conversation import Conversation
from app.models.campaign import CampaignProspect
from app.models.email_sequence import EmailSequence
from app.models.prospect import GlobalUnsubscribe
from app.utils.email_utils import (extract_latest_message_text, normalize_subject, ses_id_from_message_id,
                                   thread_message_ids)
from app.domain import inbox_reply_rules
from app.domain.inbox import LabelSource, ReplyClassification, ReplyLabel
from app.repositories.inbox_replies import ReplyUnitOfWork
from app.services.reply_classifier import ReplyClassificationService, apply_label, classify_reply_intent
from app.utils.reply_classifier import is_out_of_office
from app.utils.campaign_prospect_status import set_prospect_status
from app.utils.conversation_lock import conversation_creation_lock

logger = logging.getLogger(__name__)


_UNSUB_PHRASES = {"unsubscribe", "please unsubscribe", "please unsubscribe me", "unsubscribe me", "remove me",
                  "please remove me", "remove me from your list", "opt out", "opt-out", "stop"}


def _is_unsubscribe_reply(text: str) -> bool:
    """A short reply whose first line asks to unsubscribe (a signature or quoted mail may follow)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    first = re.sub(r"[^a-z\- ]", "", lines[0].lower()).strip()
    return first in _UNSUB_PHRASES and len(lines[0]) <= 60

class _InboxRef(NamedTuple):
    """The inbox fields the folder sync needs, read once before the IMAP session starts."""
    inbox_id: str
    tenant_id: Optional[str]
    email_address: str


class _SyncPosition(NamedTuple):
    uidvalidity: Optional[int]
    last_uid: Optional[int]


# Pending statuses a reply cancels (stop on reply, BR-DF-05)
_STOPPABLE_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")
# How many recent sent emails of the contact the subject fallback compares
_SUBJECT_MATCH_WINDOW = 50


class IMAPSyncService:
    def __init__(self, db: Optional[Session] = None) -> None:
        # The module-level instance starts without a session; sync_all binds one before any use
        self.db: Session = db  # type: ignore[assignment]

    # Seconds before a stalled IMAP server is given up on, so one dead mailbox
    # cannot hang the whole sync loop (B11)
    IMAP_TIMEOUT_SECONDS = 30
    # Overlap applied to the date window when there is no UID position yet
    SINCE_OVERLAP = timedelta(days=1)
    SENT_FOLDERS = ("Sent", "[Gmail]/Sent Mail", "Sent Items", "Sent Messages")

    def sync_all_active_inboxes(self, db: Session) -> None:
        """Background task to sync all INBOX and SENT folders for active inboxes."""
        self.db = db
        try:
            inboxes = db.query(SendingInbox).filter(SendingInbox.imap_host != None).all()
        except Exception as e:
            logger.error(f"[IMAP] Could not list inboxes to sync: {e}")
            db.rollback()
            return
        for inbox in inboxes:
            # One bad mailbox must never stop the others (B11)
            try:
                # Sync each inbox about every 4 minutes, so replies show within 10 (BR-DF-05)
                if inbox.last_sync_at and (utcnow() - inbox.last_sync_at).total_seconds() < 240:
                    continue

                # Date window used only when a folder has no UID position yet:
                # back to the last sync (minus an overlap), at most 7 days.
                if not inbox.last_sync_at:
                    days_back = 7
                else:
                    since = inbox.last_sync_at - self.SINCE_OVERLAP
                    days_back = min(7, max(1, (utcnow() - since).days + 1))

                logger.info(f"Background syncing IMAP for {inbox.email_address} (days_back={days_back})")
                self.sync_inbox(inbox, days_back=days_back)
                record_imap_sync("ok")
            except Exception as e:
                record_imap_sync("error")
                logger.error(f"[IMAP] Sync of {getattr(inbox, 'email_address', '?')} failed: {e}")
                try:
                    db.rollback()
                except Exception as rb_exc:
                    logger.debug("[IMAP] Rollback after failed sync also failed: %s", rb_exc)

    def sync_inbox(self, inbox_id_or_obj, days_back: int = 30) -> bool:
        """
        Sync historical emails for a specific inbox.
        Accepts inbox_id (str) or SendingInbox object.
        """
        if isinstance(inbox_id_or_obj, str):
            inbox = self.db.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id_or_obj).first()
        else:
            inbox = inbox_id_or_obj

        oauth = inbox is not None and (inbox.auth_type or "PASSWORD") == "OAUTH_MS365"
        if not inbox or not inbox.imap_host or not (inbox.imap_password or oauth):
            logger.warning(f"[IMAP] Inbox {inbox.email_address if inbox else 'Unknown'} not configured for IMAP sync.")
            return False

        inbox_email_for_log = inbox.email_address if inbox else "Unknown"
        # Plain values for the folder syncs: reading attributes of the ORM object after a
        # commit would reload it, i.e. open a transaction during the IMAP round trips (T-1)
        ref = _InboxRef(inbox_id=inbox.inbox_id, tenant_id=inbox.tenant_id, email_address=inbox.email_address)
        mail = None
        try:
            # Connect to IMAP: password, or XOAUTH2 for Microsoft 365 (BR-DF-05).
            # A socket timeout so a stalled server fails this inbox, not the loop.
            from app.services.ms365_oauth import imap_login, imap_secret
            host, port = inbox.imap_host, inbox.imap_port
            username = inbox.imap_username or inbox.email_address
            secret = imap_secret(self.db, inbox)
            # No transaction stays open across the IMAP session: each message is
            # stored in its own short transaction instead (T-1)
            self.db.commit()
            mail = open_imap(host, port, timeout=self.IMAP_TIMEOUT_SECONDS)
            imap_login(mail, username, secret)

            # 1. Sync INBOX (Received emails)
            self._sync_folder(mail, ref, "INBOX", days_back, direction="INBOUND")

            # 2. Sync SENT (Sent emails) - folder names vary by provider; use the
            #    first one that exists on this server.
            for folder in self.SENT_FOLDERS:
                try:
                    if self._sync_folder(mail, ref, folder, days_back, direction="OUTBOUND"):
                        break
                except (imaplib.IMAP4.abort, OSError):
                    raise  # connection is gone: stop this inbox
                except Exception as exc:
                    logger.debug(f"[IMAP] Sent folder {folder!r} not usable: {exc}")
                    continue

            inbox.last_sync_at = utcnow()
            inbox.imap_last_error = None
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[IMAP] Sync failed for {inbox_email_for_log}: {e}")
            self.db.rollback()
            # Keep the failure visible on the inbox instead of only in the logs
            try:
                inbox.imap_last_error = f"{utcnow():%Y-%m-%d %H:%M} UTC: {e}"[:2000]
                self.db.commit()
            except Exception as save_exc:
                logger.warning("[IMAP] Could not store the sync error on inbox %s: %s", inbox.inbox_id, save_exc)
                self.db.rollback()
            return False
        finally:
            # Always release the connection, whatever happened above
            if mail is not None:
                try:
                    mail.logout()
                except Exception as logout_exc:
                    logger.debug("[IMAP] Logout failed (%s); closing the socket", logout_exc)
                    try:
                        mail.shutdown()
                    except Exception as shutdown_exc:
                        logger.debug("[IMAP] Socket shutdown failed: %s", shutdown_exc)

    @staticmethod
    def _quote_mailbox(name: str) -> str:
        if name.startswith('"') or not any(c in name for c in ' []/%*"\\'):
            return name
        return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def _load_sync_state(self, inbox_model, folder_name) -> Any:
        """
        The stored UID position for this folder as a plain (uidvalidity, last_uid) pair,
        None when there is none, or False when the table is missing. The read
        transaction is closed before returning, so the IMAP search runs with none open.
        """
        from app.models.imap_sync_state import ImapSyncState
        try:
            row = self.db.query(ImapSyncState).filter(
                ImapSyncState.inbox_id == inbox_model.inbox_id,
                ImapSyncState.folder == folder_name,
            ).first()
            result = None if row is None else _SyncPosition(row.uidvalidity, row.last_uid)
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[IMAP] UID position unavailable ({exc.__class__.__name__}); "
                           f"using the date window")
            return False  # table missing: don't try to store either

    def _save_sync_state(self, state, inbox_model, folder_name, uidvalidity, last_uid) -> None:
        """Upsert the UID position in the current transaction; the caller commits."""
        from app.models.imap_sync_state import ImapSyncState
        if state is False:
            return
        try:
            with self.db.begin_nested():
                # Looked up each time: the position is now stored after every message (T-1)
                row = self.db.query(ImapSyncState).filter(
                    ImapSyncState.inbox_id == inbox_model.inbox_id,
                    ImapSyncState.folder == folder_name,
                ).first()
                if row is None:
                    row = ImapSyncState(inbox_id=inbox_model.inbox_id, folder=folder_name)
                    self.db.add(row)
                row.uidvalidity = uidvalidity
                row.last_uid = last_uid
        except Exception as exc:
            logger.warning(f"[IMAP] Could not store UID position for {folder_name}: {exc}")

    def _commit_sync_state(self, state, inbox_model, folder_name, uidvalidity, last_uid) -> None:
        """Store the UID position in a short transaction of its own."""
        try:
            self._save_sync_state(state, inbox_model, folder_name, uidvalidity, last_uid)
            self.db.commit()
        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[IMAP] Could not commit UID position for {folder_name}: {exc}")

    def _classify_reply(self, reply_message_id: str, replied_to_message_id: Optional[str]) -> None:
        """
        Reply classification (S-09) after the reply's own transaction committed, so the LLM round
        trip holds no transaction or row lock (T-1). The label, its events and an unsubscribe are
        written by ReplyClassificationService in a second short transaction; a failure leaves the
        reply unlabelled for the reply_classification job.
        """
        service = ReplyClassificationService(
            ReplyUnitOfWork.for_session(self.db),
            # Looked up at call time: tests stub classify_reply_intent on this module
            llm=lambda subject, body: classify_reply_intent(subject, body),
        )
        try:
            service.classify_and_store(reply_message_id, replied_to_message_id)
        except Exception as exc:
            self.db.rollback()
            logger.warning(f"[IMAP] Reply classification failed (retried by the job): {exc}")

    @staticmethod
    def _decode_subject(msg: Any) -> str:
        subject = ""
        for part, encoding in decode_header(msg["Subject"] or ""):
            if isinstance(part, bytes):
                subject += part.decode(encoding or "utf-8", errors="ignore")
            else:
                subject += part
        return subject

    @staticmethod
    def _plain_body(msg: email.message.Message) -> str:
        """The first text/plain part (or the whole payload of a single-part message), decoded."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    try:
                        return part.get_payload(decode=True).decode(errors="ignore")  # type: ignore[union-attr]
                    except AttributeError:  # no decodable payload
                        pass
            return ""
        try:
            return msg.get_payload(decode=True).decode(errors="ignore")  # type: ignore[union-attr]
        except AttributeError:  # no decodable payload
            return ""

    @staticmethod
    def _ooo_label(auto_submitted: Optional[str]) -> ReplyClassification:
        """The label of a reply the sync already recognised as an out-of-office auto-reply."""
        if inbox_reply_rules.is_auto_reply_header({"Auto-Submitted": auto_submitted}):
            return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 1.0, LabelSource.HEADER, "Auto-Submitted header")
        return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 0.8, LabelSource.RULES, "Out-of-office wording")

    @staticmethod
    def _synthetic_message_id(from_: Optional[str], date_str: Optional[str], subject: Optional[str],
                              to_: Optional[str]) -> str:
        """Stable id for a message with no Message-ID header, so it is stored once (B11)."""
        digest = hashlib.sha256(
            "\x1f".join([(from_ or "").strip().lower(), (to_ or "").strip().lower(),
                          (date_str or "").strip(), (subject or "").strip()]).encode("utf-8", "ignore")
        ).hexdigest()[:40]
        return f"<no-message-id-{digest}@imap-sync.invalid>"

    def _outbound_for_header(self, prospect: Prospect, header_val: str) -> Optional[EmailMessage]:
        """
        The sent email a reply's In-Reply-To / References entry names (17). The scheduler stores our
        MIME Message-ID, send-now the bare SES id, and SES rewrites Message-ID to
        <ses-id@email.amazonses.com> (or a regional variant), so an SES-form header also matches
        the bare id in provider_message_id or ses_message_id. Limited to this contact (and so to
        its workspace).
        """
        conditions = [EmailMessage.provider_message_id == header_val]
        ses_id = ses_id_from_message_id(header_val)
        if ses_id:
            conditions += [EmailMessage.ses_message_id == ses_id, EmailMessage.provider_message_id == ses_id]
        return self.db.query(EmailMessage).filter(
            or_(*conditions),
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.prospect_id == prospect.prospect_id,
        ).order_by(desc(EmailMessage.sent_at)).first()

    def _outbound_for_subject(self, prospect: Prospect, inbox_model: _InboxRef,
                              subject: str) -> Optional[EmailMessage]:
        """
        Fallback when no header matches: the newest email sent to this contact from this inbox
        whose subject equals the reply's without its Re:/Fwd:/AW: prefixes.
        """
        key = normalize_subject(subject)
        if not key:
            return None
        recent = self.db.query(EmailMessage).filter(
            EmailMessage.prospect_id == prospect.prospect_id,
            EmailMessage.inbox_id == inbox_model.inbox_id,
            EmailMessage.direction == "OUTBOUND",
        ).order_by(desc(EmailMessage.sent_at)).limit(_SUBJECT_MATCH_WINDOW).all()
        return next((m for m in recent if normalize_subject(m.subject) == key), None)

    def _conversation_of(self, outbound: EmailMessage, prospect: Prospect) -> Optional[Conversation]:
        if not outbound.conversation_id:
            return None
        return self.db.query(Conversation).filter(
            Conversation.id == outbound.conversation_id,
            Conversation.tenant_id == prospect.tenant_id,
        ).first()

    def _match_reply(self, prospect: Prospect, inbox_model: _InboxRef, msg: Any,
                     subject: str) -> Tuple[Optional[EmailMessage], Optional[Conversation]]:
        """
        Strategy A (INBOUND only): the sent email this reply answers and its conversation, from
        RFC 5322 In-Reply-To / References, else from the subject within this contact + inbox.
        The email is returned even without a conversation (campaign attribution).
        """
        first_match: Optional[EmailMessage] = None
        for header_val in thread_message_ids(msg.get("In-Reply-To"), msg.get("References")):
            found = self._outbound_for_header(prospect, header_val)
            if not found:
                continue
            first_match = first_match or found
            conv = self._conversation_of(found, prospect)
            if conv:
                logger.info(f"[IMAP] Matched reply to conv {conv.id} via In-Reply-To/References ({header_val[:60]})")
                return found, conv
        if first_match is not None:
            logger.info(f"[IMAP] Matched outbound msg {first_match.message_id[:8]} via In-Reply-To "
                        f"— no conversation yet, will create")
            return first_match, None
        by_subject = self._outbound_for_subject(prospect, inbox_model, subject)
        if by_subject is None:
            return None, None
        logger.info(f"[IMAP] Matched outbound msg {by_subject.message_id[:8]} via subject")
        return by_subject, self._conversation_of(by_subject, prospect)

    def _stop_sequence_on_reply(self, campaign_id: str, prospect_id: str) -> int:
        """
        Stop on reply (BR-DF-05, 20b): follow-ups are scheduled at launch, so cancel this contact's
        pending emails in the campaign, except those of steps whose stop_on_reply is off (NULL
        counts as on). Emails of no step (automation mail) are cancelled as before.
        """
        keep = [sid for (sid,) in self.db.query(EmailSequence.sequence_id).filter(
            EmailSequence.campaign_id == campaign_id, EmailSequence.stop_on_reply.is_(False))]
        query = self.db.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.status.in_(_STOPPABLE_STATUSES),
        )
        if keep:
            query = query.filter(or_(EmailMessage.sequence_id.is_(None), EmailMessage.sequence_id.notin_(keep)))
        return query.update({EmailMessage.status: "CANCELLED",
                             EmailMessage.failure_reason: "Stopped: the contact replied"},
                            synchronize_session=False)

    def _sync_folder(self, mail, inbox_model, folder_name, days_back, direction) -> bool:
        """
        Sync one folder. Returns False if the folder does not exist on the server.

        Only messages newer than the stored UID position are fetched; without a
        position (first sync, or the folder's UIDVALIDITY changed) the date
        window is used once, then the position is recorded.
        """
        try:
            status, _ = mail.select(self._quote_mailbox(folder_name), readonly=True)
            if status != 'OK':
                return False
        except (imaplib.IMAP4.abort, OSError):
            raise
        except imaplib.IMAP4.error as exc:
            logger.info("[IMAP] Cannot select folder %s: %s", folder_name, exc)
            return False

        uidvalidity = None
        try:
            _, uv = mail.response('UIDVALIDITY')
            if uv and uv[0]:
                uidvalidity = int(uv[0])
        except (imaplib.IMAP4.error, TypeError, ValueError):
            uidvalidity = None

        state = self._load_sync_state(inbox_model, folder_name)
        last_uid = 0
        if state and state.uidvalidity is not None and state.uidvalidity == uidvalidity:
            last_uid = int(state.last_uid or 0)

        if last_uid:
            criteria = f'(UID {last_uid + 1}:*)'
            window = f"UID > {last_uid}"
        else:
            # Search for emails from the last N days
            # Ensure month is in English for IMAP (RFC3501)
            # %b depends on locale, so we map manually
            past_date = datetime.now() - timedelta(days=days_back)
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            date_cutoff = f"{past_date.day}-{months[past_date.month - 1]}-{past_date.year}"
            criteria = f'(SINCE "{date_cutoff}")'
            window = f"SINCE {date_cutoff}"

        logger.info(f"[IMAP] Syncing {folder_name} for {inbox_model.email_address} ({window})")

        try:
            status, data = mail.uid('SEARCH', None, criteria)
            if status != 'OK':
                logger.warning(f"[IMAP] Search failed/empty in {folder_name}")
                return True
        except (imaplib.IMAP4.abort, OSError):
            raise
        except Exception as e:
            logger.error(f"[IMAP] Search error in {folder_name}: {e}")
            return True

        uids = sorted({int(u) for u in (data[0] or b"").split() if u.isdigit()})
        # "UID n:*" always returns the newest message even when it is <= n
        uids = [u for u in uids if u > last_uid]
        if not uids:
            logger.info(f"[IMAP] No new messages in {folder_name}")
            if uidvalidity is not None and (state is None or (state and state.uidvalidity != uidvalidity)):
                self._commit_sync_state(state, inbox_model, folder_name, uidvalidity, last_uid)
            return True

        logger.info(f"[IMAP] Found {len(uids)} new message(s) in {folder_name}")
        highest = last_uid

        for uid in uids:
            msg_id = str(uid)
            highest = max(highest, uid)
            # (reply message id, replied-to message id) when the reply needs classification,
            # done after this message's transaction committed (T-1)
            classify_job: Optional[tuple] = None
            try:
                # Fetch message — no transaction is open here: the previous message
                # committed (or rolled back) before the next IMAP round trip (T-1)
                status, msg_data = mail.uid('FETCH', msg_id, '(BODY.PEEK[])')
                if status != 'OK' or not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                # Parse Headers
                subject = self._decode_subject(msg)
                
                from_ = msg.get("From") or ""
                to_ = msg.get("To") or ""
                date_str = msg.get("Date")

                # Basic email extraction - Normalize
                sender_email = parseaddr(from_)[1].lower().strip()
                receiver_email = parseaddr(to_)[1].lower().strip()

                if not sender_email or not receiver_email:
                    logger.info(f"[IMAP] Skipped message — missing From or To header (From='{from_}' To='{to_}')")
                    continue
                
                # Determine who is the Prospect
                prospect_email = sender_email if direction == "INBOUND" else receiver_email
                
                # Check if prospect exists in our system (Case Insensitive)
                # Scope to the inbox's tenant: the same address can be a prospect in
                # several tenants, and mail must never be filed into another tenant.
                prospect = self.db.query(Prospect).filter(
                    Prospect.tenant_id == inbox_model.tenant_id,
                    func.lower(Prospect.email) == prospect_email
                ).first()
                if not prospect:
                    logger.info(f"[IMAP] Skipped '{subject[:30]}' — {prospect_email} is not a known prospect.")
                    self.db.rollback()  # end the read transaction before the next fetch
                    continue # Ignore emails from unknown people for now
                
                logger.info(f"[IMAP] Processing email '{subject}' for prospect {prospect_email}")

                # Parse body
                body = self._plain_body(msg)

                # Store only the newest human-readable reply block.
                raw_body = body
                body = extract_latest_message_text(body)

                # Deduplicate by provider_message_id (Message-ID header). A message
                # without one gets a stable id from its headers, so it is still
                # stored only once (B11).
                provider_msg_id = (msg.get("Message-ID") or "").strip() or self._synthetic_message_id(
                    from_, date_str, subject, to_)
                if provider_msg_id:
                    exists = self.db.query(EmailMessage).filter(
                        EmailMessage.provider_message_id == provider_msg_id,
                        EmailMessage.prospect_id == prospect.prospect_id
                    ).first()
                    if exists:
                        logger.debug(f"[IMAP] Skipped duplicate message {provider_msg_id}")
                        self.db.rollback()  # end the read transaction before the next fetch
                        continue

                # Parse Date
                sent_at = utcnow()
                if date_str:
                    try:
                        sent_at = parsedate_to_datetime(date_str)
                        if sent_at.tzinfo:
                            # Convert to UTC first, then make naive
                            sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
                    except (TypeError, ValueError):  # unparseable Date header: keep "now"
                        pass

                # ── 1. Find/Create Conversation ──────────────────────────────
                # We attempt four strategies in order of reliability.

                conv = None
                original_outbound_msg = None  # the specific sent email being replied to
                if direction == "INBOUND":
                    original_outbound_msg, conv = self._match_reply(prospect, inbox_model, msg, subject)

                # Strategies B-D all do a check-then-insert keyed on (prospect, inbox),
                # racing against the email scheduler's own find-or-create for the same
                # key (it runs concurrently on a separate thread — see conversation_lock.py).
                with conversation_creation_lock:
                    # Strategy B: match by (prospect, inbox) — works when SMTP and IMAP
                    # are the same account and the conversation was already threaded.
                    if not conv:
                        conv = self.db.query(Conversation).filter(
                            Conversation.prospect_id == prospect.prospect_id,
                            Conversation.inbox_id == inbox_model.inbox_id
                        ).first()

                    # Strategy C (INBOUND only): any conversation for this prospect that
                    # already has outbound messages — avoids orphaned reply threads.
                    if not conv and direction == "INBOUND":
                        conv = self.db.query(Conversation).filter(
                            Conversation.prospect_id == prospect.prospect_id,
                            Conversation.tenant_id == prospect.tenant_id,
                            Conversation.messages.any(EmailMessage.direction == "OUTBOUND")
                        ).order_by(desc(Conversation.last_message_at)).first()
                        if conv:
                            logger.info(f"[IMAP] Linked reply to existing conversation {conv.id} via prospect fallback")

                    # Strategy D: create a new conversation thread.
                    if not conv:
                        conv = Conversation(
                            tenant_id=prospect.tenant_id,
                            prospect_id=prospect.prospect_id,
                            inbox_id=inbox_model.inbox_id,
                            subject=subject,
                            last_message_at=sent_at
                        )
                        self.db.add(conv)
                        self.db.flush()
                        # Backfill conversation_id on the matched outbound message so future
                        # replies in the same thread can be threaded via Strategy A.
                        if original_outbound_msg and not original_outbound_msg.conversation_id:
                            original_outbound_msg.conversation_id = conv.id
                    else:
                        # Update thread timestamp if this message is newer.
                        if not conv.last_message_at or sent_at > conv.last_message_at:
                            conv.last_message_at = sent_at
                            if direction == "INBOUND":
                                conv.is_unread = True

                # ── Campaign Attribution ──────────────────────────────────────
                # Priority 1: original outbound message found via In-Reply-To.
                attributed_campaign_id = (
                    original_outbound_msg.campaign_id if original_outbound_msg else None
                )

                # Priority 2: look at the thread's most recent outbound message.
                thread_last_outbound = None
                if not attributed_campaign_id:
                    thread_last_outbound = self.db.query(EmailMessage).filter(
                        EmailMessage.conversation_id == conv.id,
                        EmailMessage.direction == "OUTBOUND",
                        EmailMessage.campaign_id != None
                    ).order_by(desc(EmailMessage.sent_at)).first()
                    attributed_campaign_id = (
                        thread_last_outbound.campaign_id if thread_last_outbound else None
                    )

                # Priority 3: prospect's most recently enrolled campaign.
                if not attributed_campaign_id:
                    enrollment = self.db.query(CampaignProspect).filter(
                        CampaignProspect.prospect_id == prospect.prospect_id
                    ).order_by(desc(CampaignProspect.enrolled_at)).first()
                    if enrollment:
                        attributed_campaign_id = enrollment.campaign_id

                # For the analytics event we want the specific sent message that
                # was replied to. Prefer the In-Reply-To match; fall back to the
                # most recent outbound in the thread.
                replied_to_msg = original_outbound_msg or thread_last_outbound

                # 2. Add Message
                new_msg = EmailMessage(
                    message_id=str(uuid.uuid4()),
                    campaign_id=attributed_campaign_id,
                    prospect_id=prospect.prospect_id,
                    inbox_id=inbox_model.inbox_id,
                    conversation_id=conv.id,
                    direction=direction,
                    subject=subject,
                    body_text=body,
                    to_email=receiver_email,
                    from_email=sender_email,
                    status="SENT",
                    provider_message_id=provider_msg_id,
                    sent_at=sent_at
                )
                
                # An out-of-office auto-reply is shown in the inbox but does not count as a reply
                is_ooo = direction == "INBOUND" and is_out_of_office(subject, body, msg.get("Auto-Submitted"))

                # Update prospect status only for the attributed campaign.
                if direction == "INBOUND" and attributed_campaign_id:
                    campaign_enrollment = self.db.query(CampaignProspect).filter(
                        CampaignProspect.campaign_id == attributed_campaign_id,
                        CampaignProspect.prospect_id == prospect.prospect_id
                    ).first()
                    if campaign_enrollment and not is_ooo:
                        try:
                            set_prospect_status(campaign_enrollment, "REPLIED")
                            self._stop_sequence_on_reply(attributed_campaign_id, prospect.prospect_id)
                        except Exception as exc:
                            logger.warning(f"[IMAP] Could not stop sequence after reply: {exc}")

                    # --- Reply-based unsubscribe detection ---
                    # Check if the reply body is essentially just "unsubscribe"
                    reply_text = extract_latest_message_text(raw_body, strip_boilerplate=False).strip().lower()
                    opt_out = inbox_reply_rules.pre_classify(subject, reply_text)
                    if opt_out is not None and opt_out.suppresses:
                        try:
                            # Every campaign in the workspace, not just this one (BR-DF-08)
                            from app.services.suppression import suppress
                            suppress(self.db, prospect.tenant_id, prospect.email,
                                     "Prospect replied with unsubscribe", kind="UNSUBSCRIBE",
                                     source=f"reply_unsubscribe:{new_msg.message_id}")

                            if replied_to_msg:
                                self.db.add(EmailEvent(
                                    message_id=replied_to_msg.message_id,
                                    event_type=EmailEvent.EVENT_UNSUBSCRIBE,
                                    event_time=sent_at
                                ))

                            apply_label(new_msg, opt_out)  # labelled and acted on here: not classified again
                            logger.info(f"[IMAP] Auto-unsubscribed {prospect.email} (reply-based)")
                        except Exception as e:
                            logger.warning(f"[IMAP] Failed to auto-unsubscribe {prospect.email}: {e}")

                self.db.add(new_msg)

                # 3. Create Event for Analytics (only for INBOUND replies)
                if direction == "INBOUND":
                    # Link the reply event to the specific sent message being replied to.
                    # replied_to_msg is set from In-Reply-To match or thread fallback.
                    if replied_to_msg:
                        reply_event = EmailEvent(
                            message_id=replied_to_msg.message_id,
                            event_type=EmailEvent.EVENT_REPLY,
                            event_time=sent_at
                        )
                        self.db.add(reply_event)
                        logger.info(f"[IMAP] Created REPLY event for msg {replied_to_msg.message_id}")
                        if not is_ooo:
                            # Outbound webhooks (BR-SF-20), committed with this message's transaction
                            from app.services import webhooks
                            webhooks.email_replied(self.db, prospect.tenant_id, new_msg, replied_to_msg, prospect)

                        # OOO is additive to EVENT_REPLY, not a replacement — the
                        # analytics layer derives "clean replies" as replied_count - ooo_count.
                        if is_ooo:
                            self.db.add(EmailEvent(
                                message_id=replied_to_msg.message_id,
                                event_type=EmailEvent.EVENT_REPLY_OOO,
                                event_time=sent_at
                            ))
                    # Label the reply (S-09): OOO here, everything else after this commit
                    if is_ooo:
                        apply_label(new_msg, self._ooo_label(msg.get("Auto-Submitted")))
                    elif new_msg.reply_classified_at is None:
                        classify_job = (new_msg.message_id, replied_to_msg.message_id if replied_to_msg else None)

                # Persist this message together with the UID position, so a later
                # failure resumes after it instead of re-fetching it (T-1)
                self._save_sync_state(state, inbox_model, folder_name, uidvalidity, uid)
                self.db.commit()

            except (imaplib.IMAP4.abort, OSError):
                self.db.rollback()
                # Connection lost: keep the position at what was fully processed
                self._commit_sync_state(state, inbox_model, folder_name, uidvalidity, max(last_uid, uid - 1))
                raise
            except Exception as e:
                logger.warning(f"[IMAP] Failed to process message UID {msg_id} in {folder_name}: {e}")
                self.db.rollback()  # Roll back only this message; previous messages are committed
                continue

            if classify_job is not None:
                self._classify_reply(*classify_job)

        # Next sync starts after the newest message seen here
        self._commit_sync_state(state, inbox_model, folder_name, uidvalidity, highest)
        return True

# Create singleton instance
imap_sync_service = IMAPSyncService()
