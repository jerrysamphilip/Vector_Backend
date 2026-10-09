
import hashlib
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime, parseaddr
import logging
from datetime import datetime, timedelta, timezone
import uuid
from sqlalchemy.orm import Session
from sqlalchemy import desc, func

from app.core.observability import record_imap_sync

from app.models.sending_inbox import SendingInbox
from app.models.email_message import EmailMessage, EmailEvent
from app.models.prospect import Prospect
from app.models.conversation import Conversation
from app.models.campaign import CampaignProspect
from app.models.prospect import GlobalUnsubscribe
from app.utils.email_utils import extract_latest_message_text
from app.utils.reply_classifier import is_out_of_office, classify_reply_intent
from app.utils.campaign_prospect_status import set_prospect_status
from app.utils.conversation_lock import conversation_creation_lock

logger = logging.getLogger(__name__)

def _utcnow() -> datetime:
    """Return current UTC time as a naive datetime (compatible with DB-stored naive values)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class IMAPSyncService:
    def __init__(self, db: Session = None):
        self.db = db

    # Seconds before a stalled IMAP server is given up on, so one dead mailbox
    # cannot hang the whole sync loop (B11)
    IMAP_TIMEOUT_SECONDS = 30
    # Overlap applied to the date window when there is no UID position yet
    SINCE_OVERLAP = timedelta(days=1)
    SENT_FOLDERS = ("Sent", "[Gmail]/Sent Mail", "Sent Items", "Sent Messages")

    def sync_all_active_inboxes(self, db: Session):
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
                if inbox.last_sync_at and (_utcnow() - inbox.last_sync_at).total_seconds() < 240:
                    continue

                # Date window used only when a folder has no UID position yet:
                # back to the last sync (minus an overlap), at most 7 days.
                if not inbox.last_sync_at:
                    days_back = 7
                else:
                    since = inbox.last_sync_at - self.SINCE_OVERLAP
                    days_back = min(7, max(1, (_utcnow() - since).days + 1))

                logger.info(f"Background syncing IMAP for {inbox.email_address} (days_back={days_back})")
                self.sync_inbox(inbox, days_back=days_back)
                record_imap_sync("ok")
            except Exception as e:
                record_imap_sync("error")
                logger.error(f"[IMAP] Sync of {getattr(inbox, 'email_address', '?')} failed: {e}")
                try:
                    db.rollback()
                except Exception:
                    pass

    def sync_inbox(self, inbox_id_or_obj, days_back: int = 30):
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
        mail = None
        try:
            # Connect to IMAP: password, or XOAUTH2 for Microsoft 365 (BR-DF-05).
            # A socket timeout so a stalled server fails this inbox, not the loop.
            from app.services.ms365_oauth import imap_login, imap_secret
            mail = imaplib.IMAP4_SSL(inbox.imap_host, inbox.imap_port or 993,
                                     timeout=self.IMAP_TIMEOUT_SECONDS)
            imap_login(mail, inbox.imap_username or inbox.email_address, imap_secret(self.db, inbox))

            # 1. Sync INBOX (Received emails)
            self._sync_folder(mail, inbox, "INBOX", days_back, direction="INBOUND")

            # 2. Sync SENT (Sent emails) - folder names vary by provider; use the
            #    first one that exists on this server.
            for folder in self.SENT_FOLDERS:
                try:
                    if self._sync_folder(mail, inbox, folder, days_back, direction="OUTBOUND"):
                        break
                except (imaplib.IMAP4.abort, OSError):
                    raise  # connection is gone: stop this inbox
                except Exception as exc:
                    logger.debug(f"[IMAP] Sent folder {folder!r} not usable: {exc}")
                    continue

            inbox.last_sync_at = datetime.utcnow()
            inbox.imap_last_error = None
            self.db.commit()
            return True
        except Exception as e:
            logger.error(f"[IMAP] Sync failed for {inbox_email_for_log}: {e}")
            self.db.rollback()
            # Keep the failure visible on the inbox instead of only in the logs
            try:
                inbox.imap_last_error = f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC: {e}"[:2000]
                self.db.commit()
            except Exception:
                self.db.rollback()
            return False
        finally:
            # Always release the connection, whatever happened above
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    try:
                        mail.shutdown()
                    except Exception:
                        pass

    @staticmethod
    def _quote_mailbox(name: str) -> str:
        if name.startswith('"') or not any(c in name for c in ' []/%*"\\'):
            return name
        return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'

    def _load_sync_state(self, inbox_model, folder_name):
        """The stored UID position for this folder, or None (also when the table is missing)."""
        from app.models.imap_sync_state import ImapSyncState
        try:
            with self.db.begin_nested():
                return self.db.query(ImapSyncState).filter(
                    ImapSyncState.inbox_id == inbox_model.inbox_id,
                    ImapSyncState.folder == folder_name,
                ).first()
        except Exception as exc:
            logger.warning(f"[IMAP] UID position unavailable ({exc.__class__.__name__}); "
                           f"using the date window")
            return False  # table missing: don't try to store either

    def _save_sync_state(self, state, inbox_model, folder_name, uidvalidity, last_uid):
        from app.models.imap_sync_state import ImapSyncState
        if state is False:
            return
        try:
            with self.db.begin_nested():
                if state is None:
                    state = ImapSyncState(inbox_id=inbox_model.inbox_id, folder=folder_name)
                    self.db.add(state)
                state.uidvalidity = uidvalidity
                state.last_uid = last_uid
        except Exception as exc:
            logger.warning(f"[IMAP] Could not store UID position for {folder_name}: {exc}")

    @staticmethod
    def _synthetic_message_id(from_: str, date_str: str, subject: str, to_: str) -> str:
        """Stable id for a message with no Message-ID header, so it is stored once (B11)."""
        digest = hashlib.sha256(
            "\x1f".join([(from_ or "").strip().lower(), (to_ or "").strip().lower(),
                          (date_str or "").strip(), (subject or "").strip()]).encode("utf-8", "ignore")
        ).hexdigest()[:40]
        return f"<no-message-id-{digest}@imap-sync.invalid>"

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
        except Exception:
            return False

        uidvalidity = None
        try:
            _, uv = mail.response('UIDVALIDITY')
            if uv and uv[0]:
                uidvalidity = int(uv[0])
        except Exception:
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
                self._save_sync_state(state, inbox_model, folder_name, uidvalidity, last_uid)
            return True

        logger.info(f"[IMAP] Found {len(uids)} new message(s) in {folder_name}")
        highest = last_uid

        for uid in uids:
            msg_id = str(uid)
            highest = max(highest, uid)
            try:
                sp = self.db.begin_nested()  # SAVEPOINT per message — failure rolls back only this message
                # Fetch message
                status, msg_data = mail.uid('FETCH', msg_id, '(BODY.PEEK[])')
                if status != 'OK' or not msg_data or not isinstance(msg_data[0], tuple):
                    sp.rollback()
                    continue
                
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                # Parse Headers
                subject_header = msg["Subject"] or ""
                decoded_list = decode_header(subject_header)
                subject = ""
                for part, encoding in decoded_list:
                    if isinstance(part, bytes):
                        subject += part.decode(encoding or "utf-8", errors="ignore")
                    else:
                        subject += part
                
                from_ = msg.get("From") or ""
                to_ = msg.get("To") or ""
                date_str = msg.get("Date")

                # Basic email extraction - Normalize
                sender_email = parseaddr(from_)[1].lower().strip()
                receiver_email = parseaddr(to_)[1].lower().strip()

                if not sender_email or not receiver_email:
                    logger.info(f"[IMAP] Skipped message — missing From or To header (From='{from_}' To='{to_}')")
                    sp.rollback()
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
                    sp.rollback()
                    continue # Ignore emails from unknown people for now
                
                logger.info(f"[IMAP] Processing email '{subject}' for prospect {prospect_email}")

                # Parse body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            try:
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                            except: pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(errors="ignore")
                    except: pass

                # Store only the newest human-readable reply block.
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
                        sp.rollback()
                        continue

                # Parse Date
                sent_at = datetime.utcnow()
                if date_str:
                    try:
                        sent_at = parsedate_to_datetime(date_str)
                        if sent_at.tzinfo:
                            # Convert to UTC first, then make naive
                            sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
                    except: pass

                # ── 1. Find/Create Conversation ──────────────────────────────
                # We attempt four strategies in order of reliability.

                # Extract standard email-threading headers from the reply.
                in_reply_to = (msg.get("In-Reply-To") or "").strip()
                references_header = (msg.get("References") or "").strip()

                conv = None
                original_outbound_msg = None  # the specific sent email being replied to

                # Strategy A (INBOUND only): RFC 5322 In-Reply-To / References.
                # When a prospect hits Reply, their mail client sets In-Reply-To to
                # the Message-ID of the email we sent. We now store that Message-ID
                # in provider_message_id, so we can do an exact match here.
                if direction == "INBOUND":
                    # Collect candidate header values: In-Reply-To first, then References chain.
                    candidates = []
                    if in_reply_to:
                        candidates.append(in_reply_to)
                    if references_header:
                        # References is a space-separated list; newest entry last.
                        candidates.extend(references_header.split())

                    for header_val in candidates:
                        header_val = header_val.strip()
                        if not header_val:
                            continue
                        orig = self.db.query(EmailMessage).filter(
                            EmailMessage.provider_message_id == header_val,
                            EmailMessage.direction == "OUTBOUND",
                            EmailMessage.prospect_id == prospect.prospect_id
                        ).first()
                        if orig:
                            # Always capture for campaign attribution even if conversation_id
                            # is missing (messages sent before conversation threading was added).
                            original_outbound_msg = orig
                            if orig.conversation_id:
                                conv = self.db.query(Conversation).filter(
                                    Conversation.id == orig.conversation_id,
                                    Conversation.tenant_id == prospect.tenant_id
                                ).first()
                                if conv:
                                    logger.info(
                                        f"[IMAP] Matched reply to conv {conv.id} "
                                        f"via In-Reply-To/References ({header_val[:60]})"
                                    )
                                    break
                            else:
                                logger.info(
                                    f"[IMAP] Matched outbound msg {orig.message_id[:8]} via "
                                    f"In-Reply-To ({header_val[:60]}) — no conversation yet, will create"
                                )

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
                            # Stop on reply (BR-DF-05): follow-ups are scheduled at launch,
                            # so cancel this contact's remaining steps in the campaign now.
                            self.db.query(EmailMessage).filter(
                                EmailMessage.campaign_id == attributed_campaign_id,
                                EmailMessage.prospect_id == prospect.prospect_id,
                                EmailMessage.direction == "OUTBOUND",
                                EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"]),
                            ).update({EmailMessage.status: "CANCELLED",
                                      EmailMessage.failure_reason: "Stopped: the contact replied"},
                                     synchronize_session=False)
                        except Exception as exc:
                            logger.warning(f"[IMAP] Could not stop sequence after reply: {exc}")

                    # --- Reply-based unsubscribe detection ---
                    # Check if the reply body is essentially just "unsubscribe"
                    reply_text = extract_latest_message_text(body).strip().lower()
                    if reply_text in ("unsubscribe", "unsubscribe.", "please unsubscribe", "please unsubscribe me"):
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

                        # Tag the reply as OOO or (if not OOO) run AI intent
                        # classification for the "Positive Replies" metric.
                        # Both are additive to EVENT_REPLY, not a replacement —
                        # the analytics layer derives "clean replies" as
                        # replied_count - ooo_count.
                        if is_ooo:
                            self.db.add(EmailEvent(
                                message_id=replied_to_msg.message_id,
                                event_type=EmailEvent.EVENT_REPLY_OOO,
                                event_time=sent_at
                            ))
                        else:
                            intent = classify_reply_intent(subject, body)
                            if intent == "positive":
                                self.db.add(EmailEvent(
                                    message_id=replied_to_msg.message_id,
                                    event_type=EmailEvent.EVENT_POSITIVE_REPLY,
                                    event_time=sent_at
                                ))

                sp.commit()  # Release SAVEPOINT — persist this message

            except (imaplib.IMAP4.abort, OSError):
                sp.rollback()
                # Connection lost: keep the position at what was fully processed
                self._save_sync_state(state, inbox_model, folder_name, uidvalidity, max(last_uid, uid - 1))
                raise
            except Exception as e:
                logger.warning(f"[IMAP] Failed to process message UID {msg_id} in {folder_name}: {e}")
                sp.rollback()  # Roll back only this message; previous messages are safe
                continue

        # Next sync starts after the newest message seen here
        self._save_sync_state(state, inbox_model, folder_name, uidvalidity, highest)
        self.db.flush()
        return True

# Create singleton instance
imap_sync_service = IMAPSyncService()
