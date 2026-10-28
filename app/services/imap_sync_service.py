"""
IMAP reply sync (BR-DF-05, B11, T-1): received replies and sent mail of every mailbox with IMAP are
threaded into the unified inbox, attributed to their campaign, stop the contact's sequence, and are
labelled (S-09).

One folder sync, step by step:
  select + position   the folder's UIDVALIDITY and the stored UID position (app/domain/inbox_sync.py)
  search              UIDs after the position, or the date window when there is none
  per message         fetch (no transaction open) -> parse -> record in ONE short transaction: the
                      contact, duplicates, the sent email it answers, the conversation, the campaign,
                      stop on reply, reply-based opt-out, analytics events, and the UID position
  classify            the reply label (LLM through the budget, or rules) after that commit
SQL lives in app/repositories/inbox_sync.py. Each step runs in its own unit of work, and leaving one
rolls back whatever it did not commit, so no transaction is open during an IMAP round trip or an LLM
call (T-1). Connections come from ``providers.mail_connector()`` (verified TLS, DNS-pinned when deployed).

Stop on reply: step mail is stopped by sequence_stop.stop_pending_steps under each step's
stop_on_reply flag (the rule shared with the bounce / unsubscribe handlers); pending mail of no step
(automation mail) is cancelled too, as it always has been on reply -- the BRD (BR-DF-05) only says a
reply stops the sequence.
"""

from __future__ import annotations

import imaplib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional, Tuple, Union

from app.adapters import providers
from app.adapters.mail import ImapConnector
from app.core.clock import utcnow
from app.core.observability import record_imap_sync
from app.domain import inbox_reply_rules
from app.domain.inbox_sync import (
    _UNSUB_PHRASES,
    INBOUND,
    OUTBOUND,
    REPLY_STOP_REASON,
    SENT_FOLDERS,
    SINCE_OVERLAP,
    FolderCursor,
    InboxRef,
    ParsedMessage,
    days_back_for,
    fetched_bytes,
    new_uids,
    ooo_label,
    parse_message,
    parse_uidvalidity,
    quote_mailbox,
    search_criteria,
    start_uid,
    sync_due,
)
from app.domain.inbox_sync import is_unsubscribe_reply as _is_unsubscribe_reply
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.sending_inbox import SendingInbox
from app.repositories.inbox_replies import ReplyUnitOfWork
from app.repositories.inbox_sync import ImapSyncRepository, ImapSyncUnitOfWork, Session
from app.services import ms365_oauth, sequence_stop, suppression, webhooks
from app.services.reply_classifier import ReplyClassificationService, apply_label, classify_reply_intent
from app.utils.campaign_prospect_status import set_prospect_status
from app.utils.conversation_lock import conversation_creation_lock
from app.utils.email_utils import (extract_latest_message_text, normalize_subject, ses_id_from_message_id,
                                   thread_message_ids)
from app.utils.reply_classifier import is_out_of_office

logger = logging.getLogger(__name__)

__all__ = ["IMAPSyncService", "imap_sync_service", "_UNSUB_PHRASES", "_is_unsubscribe_reply"]

# (reply message id, replied-to message id): a reply to classify after its transaction committed
ClassifyJob = Tuple[str, Optional[str]]
StopSteps = Callable[..., int]


class ReplyClassifier:
    """What the sync needs of ReplyClassificationService (unit tests pass a fake)."""

    def classify_and_store(self, reply_message_id: str, replied_to_message_id: Optional[str]) -> Any: ...


@dataclass(frozen=True)
class _Login:
    host: str
    port: Optional[int]
    username: str
    secret: Any


@dataclass
class _Match:
    """The sent email a reply answers and its conversation (either may be missing)."""
    outbound: Optional[EmailMessage] = None
    conversation: Optional[Conversation] = None


@dataclass
class _Record:
    """One message being stored: what it is and what it was matched to."""
    ref: InboxRef
    parsed: ParsedMessage
    direction: str
    prospect: Prospect
    match: _Match
    message: Any = None  # the EmailMessage being stored (set once it is built)
    replied_to: Optional[EmailMessage] = None
    is_ooo: bool = False


def _imap_configured(inbox: Optional[SendingInbox]) -> bool:
    oauth = inbox is not None and (inbox.auth_type or "PASSWORD") == ms365_oauth.AUTH_TYPE
    return bool(inbox and inbox.imap_host and (inbox.imap_password or oauth))


def header_match_ids(header_val: str) -> List[str]:
    """What a reply's In-Reply-To / References entry can match on our sent email (17): the value itself
    (the scheduler stores our MIME Message-ID, send-now the bare SES id) and, for an SES-form header
    <ses-id@email.amazonses.com> (or a regional variant), the bare SES id."""
    ses_id = ses_id_from_message_id(header_val)
    return [header_val, ses_id] if ses_id else [header_val]


def _reply_text(parsed: ParsedMessage) -> str:
    """The newest reply block, boilerplate kept, lower case (opt-out detection)."""
    return extract_latest_message_text(parsed.raw_body, strip_boilerplate=False).strip().lower()


def _llm(subject: str, body: str) -> Any:
    # Looked up at call time: tests stub classify_reply_intent on this module
    return classify_reply_intent(subject, body)


def _logout(mail: Any) -> None:
    """Always release the connection, whatever happened."""
    if mail is None:
        return
    try:
        mail.logout()
    except Exception as logout_exc:
        logger.debug("[IMAP] Logout failed (%s); closing the socket", logout_exc)
        try:
            mail.shutdown()
        except Exception as shutdown_exc:
            logger.debug("[IMAP] Socket shutdown failed: %s", shutdown_exc)


class IMAPSyncService:
    # Seconds before a stalled IMAP server is given up on, so one dead mailbox cannot hang the loop (B11)
    IMAP_TIMEOUT_SECONDS = 30
    SINCE_OVERLAP = SINCE_OVERLAP
    SENT_FOLDERS = SENT_FOLDERS

    def __init__(self, db: Optional[Session] = None, *, uow: Optional[ImapSyncUnitOfWork] = None,
                 classifier: Optional[ReplyClassifier] = None, connector: Optional[ImapConnector] = None,
                 stop_steps: Optional[StopSteps] = None) -> None:
        # The module-level instance starts without a session; sync_all_active_inboxes(db) uses its own
        self.uow: Any = uow if uow is not None else (ImapSyncUnitOfWork.for_session(db) if db is not None else None)
        self._classifier: Any = classifier if classifier is not None else (
            ReplyClassificationService(ReplyUnitOfWork.for_session(db), llm=_llm) if db is not None else None)
        self._connector = connector
        self._stop_steps = stop_steps

    @classmethod
    def for_session(cls, db: Session) -> "IMAPSyncService":
        return cls(db)

    @property
    def connector(self) -> ImapConnector:
        return self._connector if self._connector is not None else providers.mail_connector()

    def _end_transaction(self) -> None:
        """Roll back whatever is open (leaving the unit of work does)."""
        with self.uow:
            pass

    # ── All inboxes (background job) ─────────────────────────────────────
    def sync_all_active_inboxes(self, db: Optional[Session] = None) -> None:
        """Background task: sync INBOX and the sent folder of every inbox with IMAP that is due."""
        service = self if db is None else IMAPSyncService.for_session(db)
        service._sync_all()

    def _sync_all(self) -> None:
        try:
            with self.uow as uow:
                inboxes = uow.sync.inboxes_to_sync()
        except Exception as e:
            logger.error(f"[IMAP] Could not list inboxes to sync: {e}")
            return
        for inbox in inboxes:
            # One bad mailbox must never stop the others (B11)
            try:
                self._sync_if_due(inbox)
            except Exception as e:
                record_imap_sync("error")
                logger.error(f"[IMAP] Sync of {getattr(inbox, 'email_address', '?')} failed: {e}")
                try:
                    self._end_transaction()
                except Exception as rb_exc:
                    logger.debug("[IMAP] Rollback after failed sync also failed: %s", rb_exc)

    def _sync_if_due(self, inbox: SendingInbox) -> None:
        now = utcnow()
        if not sync_due(inbox.last_sync_at, now):
            return
        days_back = days_back_for(inbox.last_sync_at, now)
        logger.info(f"Background syncing IMAP for {inbox.email_address} (days_back={days_back})")
        self.sync_inbox(inbox, days_back=days_back)
        record_imap_sync("ok")

    # ── One inbox ─────────────────────────────────────────────────────────
    def sync_inbox(self, inbox_id_or_obj: Union[str, SendingInbox], days_back: int = 30) -> bool:
        """Sync one inbox (an inbox id or a SendingInbox). False when it is not set up for IMAP or the
        sync failed; the failure is kept on the inbox (imap_last_error)."""
        inbox, ref, configured = self._prepare(inbox_id_or_obj)
        if inbox is None or ref is None or not configured:
            logger.warning(f"[IMAP] Inbox {ref.email_address if ref else 'Unknown'} not configured for IMAP sync.")
            return False
        mail = None
        try:
            login = self._login_details(inbox)
            # Connect: password, or XOAUTH2 for Microsoft 365 (BR-DF-05); a socket timeout so a
            # stalled server fails this inbox, not the loop
            mail = self.connector.open_imap(login.host, login.port, timeout=self.IMAP_TIMEOUT_SECONDS)
            ms365_oauth.imap_login(mail, login.username, login.secret)
            self._sync_folders(mail, ref, days_back)
            self._record_result(inbox, None)
            return True
        except Exception as e:
            logger.error(f"[IMAP] Sync failed for {ref.email_address}: {e}")
            self._record_failure(inbox, ref, e)
            return False
        finally:
            _logout(mail)

    def _prepare(self, inbox_id_or_obj: Union[str, SendingInbox]
                 ) -> Tuple[Optional[SendingInbox], Optional[InboxRef], bool]:
        """The inbox, its plain values and whether it is set up for IMAP: reading attributes of the ORM
        object after a commit would reload it, i.e. open a transaction during the IMAP round trips (T-1)."""
        with self.uow as uow:
            inbox = uow.sync.get_inbox(inbox_id_or_obj) if isinstance(inbox_id_or_obj, str) else inbox_id_or_obj
            if inbox is None:
                return None, None, False
            ref = InboxRef(inbox_id=inbox.inbox_id, tenant_id=inbox.tenant_id, email_address=inbox.email_address)
            return inbox, ref, _imap_configured(inbox)

    def _login_details(self, inbox: SendingInbox) -> _Login:
        """Host and secret (a refreshed OAuth token for Microsoft 365), committed before connecting (T-1)."""
        with self.uow as uow:
            login = _Login(host=inbox.imap_host, port=inbox.imap_port,
                           username=inbox.imap_username or inbox.email_address,
                           secret=ms365_oauth.imap_secret(uow.session, inbox))
            uow.commit()
        return login

    def _record_result(self, inbox: SendingInbox, error: Optional[str]) -> None:
        with self.uow as uow:
            if error is None:
                inbox.last_sync_at = utcnow()
            inbox.imap_last_error = error
            uow.commit()

    def _record_failure(self, inbox: SendingInbox, ref: InboxRef, exc: Exception) -> None:
        """Keep the failure visible on the inbox instead of only in the logs."""
        try:
            self._record_result(inbox, f"{utcnow():%Y-%m-%d %H:%M} UTC: {exc}"[:2000])
        except Exception as save_exc:
            logger.warning("[IMAP] Could not store the sync error on inbox %s: %s", ref.inbox_id, save_exc)

    def _sync_folders(self, mail: Any, ref: InboxRef, days_back: int) -> None:
        # 1. Received mail
        self._sync_folder(mail, ref, "INBOX", days_back, direction=INBOUND)
        # 2. Sent mail: folder names vary by provider; the first one that exists on the server
        for folder in self.SENT_FOLDERS:
            try:
                if self._sync_folder(mail, ref, folder, days_back, direction=OUTBOUND):
                    break
            except (imaplib.IMAP4.abort, OSError):
                raise  # connection is gone: stop this inbox
            except Exception as exc:
                logger.debug(f"[IMAP] Sent folder {folder!r} not usable: {exc}")

    # ── One folder ────────────────────────────────────────────────────────
    def _sync_folder(self, mail: Any, ref: InboxRef, folder_name: str, days_back: int, direction: str) -> bool:
        """
        Sync one folder. Returns False if the folder does not exist on the server.

        Only messages newer than the stored UID position are fetched; without a position (first sync,
        or the folder's UIDVALIDITY changed) the date window is used once, then the position is recorded.
        """
        if not self._select(mail, folder_name):
            return False
        cursor = self._cursor(mail, ref, folder_name)
        uids = self._search(mail, ref, cursor, days_back)
        if uids is None:
            return True
        if not uids:
            logger.info(f"[IMAP] No new messages in {folder_name}")
            if cursor.position_changed:
                self._commit_position(ref, cursor, cursor.last_uid)
            return True
        logger.info(f"[IMAP] Found {len(uids)} new message(s) in {folder_name}")
        for uid in uids:
            job = self._sync_message(mail, ref, cursor, uid, direction)
            if job is not None:
                self._classify_reply(*job)
        # Next sync starts after the newest message seen here
        self._commit_position(ref, cursor, max(cursor.last_uid, uids[-1]))
        return True

    @staticmethod
    def _select(mail: Any, folder_name: str) -> bool:
        try:
            status, _ = mail.select(quote_mailbox(folder_name), readonly=True)
        except (imaplib.IMAP4.abort, OSError):
            raise
        except imaplib.IMAP4.error as exc:
            logger.info("[IMAP] Cannot select folder %s: %s", folder_name, exc)
            return False
        return bool(status == 'OK')

    def _cursor(self, mail: Any, ref: InboxRef, folder_name: str) -> FolderCursor:
        try:
            _, response = mail.response('UIDVALIDITY')
            uidvalidity = parse_uidvalidity(response)
        except (imaplib.IMAP4.error, TypeError, ValueError):
            uidvalidity = None
        state = self._load_position(ref, folder_name)
        return FolderCursor(folder=folder_name, state=state, uidvalidity=uidvalidity,
                            last_uid=start_uid(state, uidvalidity))

    def _load_position(self, ref: InboxRef, folder_name: str) -> Any:
        """The stored UID position, None when there is none, or False when the table is unusable. The
        read transaction is closed before returning, so the IMAP search runs with none open."""
        try:
            with self.uow as uow:
                position = uow.sync.position(ref.inbox_id, folder_name)
                uow.commit()
            return position
        except Exception as exc:
            logger.warning(f"[IMAP] UID position unavailable ({exc.__class__.__name__}); using the date window")
            return False  # table missing: don't try to store either

    def _search(self, mail: Any, ref: InboxRef, cursor: FolderCursor, days_back: int) -> Optional[List[int]]:
        """New UIDs, ascending; None when the search failed."""
        criteria, window = search_criteria(cursor.last_uid, days_back, datetime.now())
        logger.info(f"[IMAP] Syncing {cursor.folder} for {ref.email_address} ({window})")
        try:
            status, data = mail.uid('SEARCH', None, criteria)
        except (imaplib.IMAP4.abort, OSError):
            raise
        except Exception as e:
            logger.error(f"[IMAP] Search error in {cursor.folder}: {e}")
            return None
        if status != 'OK':
            logger.warning(f"[IMAP] Search failed/empty in {cursor.folder}")
            return None
        return new_uids(data, cursor.last_uid)

    def _save_position(self, uow: Any, ref: InboxRef, cursor: FolderCursor, last_uid: int) -> None:
        """Upsert the UID position in the current transaction (in a savepoint); the caller commits."""
        if not cursor.can_store:
            return
        try:
            with uow.savepoint():
                uow.sync.save_position(ref.inbox_id, cursor.folder, cursor.uidvalidity, last_uid)
        except Exception as exc:
            logger.warning(f"[IMAP] Could not store UID position for {cursor.folder}: {exc}")

    def _commit_position(self, ref: InboxRef, cursor: FolderCursor, last_uid: int) -> None:
        """Store the UID position in a short transaction of its own."""
        try:
            with self.uow as uow:
                self._save_position(uow, ref, cursor, last_uid)
                uow.commit()
        except Exception as exc:
            logger.warning(f"[IMAP] Could not commit UID position for {cursor.folder}: {exc}")

    # ── One message ───────────────────────────────────────────────────────
    def _sync_message(self, mail: Any, ref: InboxRef, cursor: FolderCursor, uid: int,
                      direction: str) -> Optional[ClassifyJob]:
        """Fetch, parse and record one message. Returns the classification to run after its commit."""
        try:
            # No transaction is open here: the previous message committed (or rolled back) (T-1)
            status, msg_data = mail.uid('FETCH', str(uid), '(BODY.PEEK[])')
            raw = fetched_bytes(status, msg_data)
            if raw is None:
                return None
            parsed = parse_message(raw, utcnow(), extract_latest_message_text)
            if parsed is None:
                logger.info("[IMAP] Skipped message — missing From or To header")
                return None
            return self._record(ref, cursor, uid, parsed, direction)
        except (imaplib.IMAP4.abort, OSError):
            # Connection lost: keep the position at what was fully processed
            self._commit_position(ref, cursor, max(cursor.last_uid, uid - 1))
            raise
        except Exception as e:
            # Only this message is rolled back; previous messages are committed
            logger.warning(f"[IMAP] Failed to process message UID {uid} in {cursor.folder}: {e}")
            return None

    def _record(self, ref: InboxRef, cursor: FolderCursor, uid: int, parsed: ParsedMessage,
                direction: str) -> Optional[ClassifyJob]:
        """Store one message with the UID position in one transaction, so a later failure resumes
        after it instead of re-fetching it (T-1)."""
        with self.uow as uow:
            repo: ImapSyncRepository = uow.sync
            contact_email = parsed.contact_email(direction)
            prospect = repo.find_prospect(ref.tenant_id, contact_email)
            if prospect is None:
                logger.info(f"[IMAP] Skipped '{parsed.subject[:30]}' — {contact_email} is not a known prospect.")
                return None
            logger.info(f"[IMAP] Processing email '{parsed.subject}' for prospect {contact_email}")
            # Deduplicate by Message-ID (or the stable id of a message without one, B11)
            if repo.message_stored(parsed.provider_message_id, prospect.prospect_id):
                logger.debug(f"[IMAP] Skipped duplicate message {parsed.provider_message_id}")
                return None
            match = self._match_reply(repo, prospect, ref, parsed) if direction == INBOUND else _Match()
            record = _Record(ref, parsed, direction, prospect, match)
            conversation = self._thread(repo, record)
            campaign_id, record.replied_to = self._attribute(repo, conversation, prospect, match.outbound)
            record.message = self._new_message(record, conversation, campaign_id)
            job = None
            if direction == INBOUND:
                job = self._apply_inbound(uow, record, campaign_id)
            else:
                repo.add(record.message)
            self._save_position(uow, ref, cursor, uid)
            uow.commit()
            return job

    # Strategy A (INBOUND only): the sent email this reply answers and its conversation
    def _match_reply(self, repo: ImapSyncRepository, prospect: Prospect, ref: InboxRef,
                     parsed: ParsedMessage) -> _Match:
        """From RFC 5322 In-Reply-To / References, else from the subject within this contact + inbox.
        The email is returned even without a conversation (campaign attribution)."""
        first: Optional[EmailMessage] = None
        for header_val in thread_message_ids(parsed.in_reply_to, parsed.references):
            found = repo.outbound_by_header(prospect.prospect_id, header_match_ids(header_val))
            if not found:
                continue
            first = first or found
            conv = repo.conversation(prospect.tenant_id, found.conversation_id)
            if conv:
                logger.info(f"[IMAP] Matched reply to conv {conv.id} via In-Reply-To/References ({header_val[:60]})")
                return _Match(found, conv)
        if first is not None:
            logger.info(f"[IMAP] Matched outbound msg {first.message_id[:8]} via In-Reply-To "
                        f"— no conversation yet, will create")
            return _Match(first, None)
        by_subject = self._outbound_for_subject(repo, prospect, ref, parsed.subject)
        if by_subject is None:
            return _Match()
        logger.info(f"[IMAP] Matched outbound msg {by_subject.message_id[:8]} via subject")
        return _Match(by_subject, repo.conversation(prospect.tenant_id, by_subject.conversation_id))

    @staticmethod
    def _outbound_for_subject(repo: ImapSyncRepository, prospect: Prospect, ref: InboxRef,
                              subject: str) -> Optional[EmailMessage]:
        """The newest email sent to this contact from this inbox whose subject equals the reply's
        without its Re:/Fwd:/AW: prefixes."""
        key = normalize_subject(subject)
        if not key:
            return None
        recent = repo.recent_outbound(prospect.prospect_id, ref.inbox_id)
        return next((m for m in recent if normalize_subject(m.subject) == key), None)

    @staticmethod
    def _thread(repo: ImapSyncRepository, record: _Record) -> Conversation:
        """The conversation of the message. Strategies B-D check-then-insert on (contact, inbox), racing
        the scheduler's own find-or-create (another thread, see conversation_lock.py)."""
        prospect, parsed, inbound = record.prospect, record.parsed, record.direction == INBOUND
        with conversation_creation_lock:
            # B: (contact, inbox) -- SMTP and IMAP are the same account and the thread exists
            conv = record.match.conversation or repo.conversation_for_inbox(prospect.prospect_id, record.ref.inbox_id)
            # C (INBOUND only): any conversation of the contact with sent mail -- no orphaned reply threads
            if not conv and inbound:
                conv = repo.conversation_with_outbound(prospect.tenant_id, prospect.prospect_id)
                if conv:
                    logger.info(f"[IMAP] Linked reply to existing conversation {conv.id} via prospect fallback")
            if conv:
                if not conv.last_message_at or parsed.sent_at > conv.last_message_at:
                    conv.last_message_at = parsed.sent_at
                    if inbound:
                        conv.is_unread = True
                return conv
            # D: a new thread; the matched sent email joins it so later replies thread via strategy A
            conv = repo.add_conversation(Conversation(
                tenant_id=prospect.tenant_id, prospect_id=prospect.prospect_id, inbox_id=record.ref.inbox_id,
                subject=parsed.subject, last_message_at=parsed.sent_at))
            outbound = record.match.outbound
            if outbound and not outbound.conversation_id:
                outbound.conversation_id = conv.id
            return conv

    @staticmethod
    def _attribute(repo: ImapSyncRepository, conversation: Conversation, prospect: Prospect,
                   outbound: Optional[EmailMessage]) -> Tuple[Optional[str], Optional[EmailMessage]]:
        """(campaign of the message, the sent email it replies to): the matched sent email, else the
        thread's latest campaign email, else the contact's latest enrolment (campaign only)."""
        campaign_id = outbound.campaign_id if outbound else None
        thread_last = None
        if not campaign_id:
            thread_last = repo.thread_last_campaign_outbound(conversation.id)
            campaign_id = thread_last.campaign_id if thread_last else None
        if not campaign_id:
            enrollment = repo.latest_enrollment(prospect.prospect_id)
            campaign_id = enrollment.campaign_id if enrollment else None
        return campaign_id, outbound or thread_last

    @staticmethod
    def _new_message(record: _Record, conversation: Conversation, campaign_id: Optional[str]) -> EmailMessage:
        parsed = record.parsed
        # An out-of-office auto-reply is shown in the inbox but does not count as a reply
        record.is_ooo = record.direction == INBOUND and is_out_of_office(parsed.subject, parsed.body,
                                                                          parsed.auto_submitted)
        return EmailMessage(
            message_id=str(uuid.uuid4()), campaign_id=campaign_id, prospect_id=record.prospect.prospect_id,
            inbox_id=record.ref.inbox_id, conversation_id=conversation.id, direction=record.direction,
            subject=parsed.subject, body_text=parsed.body, to_email=parsed.receiver_email,
            from_email=parsed.sender_email, status="SENT", provider_message_id=parsed.provider_message_id,
            sent_at=parsed.sent_at)

    # ── Effects of a received reply ───────────────────────────────────────
    def _apply_inbound(self, uow: Any, record: _Record, campaign_id: Optional[str]) -> Optional[ClassifyJob]:
        """Stop on reply and reply-based opt-out (attributed replies), the message itself, the analytics
        events and the label. Returns the classification to run after the commit, if any."""
        if campaign_id:
            self._mark_replied(uow, record, campaign_id)
            self._opt_out(uow, record)
        message = record.message
        uow.sync.add(message)
        self._reply_events(uow, record)
        # Label the reply (S-09): out of office here, everything else after this commit
        if record.is_ooo:
            apply_label(message, ooo_label(record.parsed.auto_submitted))
            return None
        if message.reply_classified_at is None:
            return message.message_id, (record.replied_to.message_id if record.replied_to else None)
        return None

    def _mark_replied(self, uow: Any, record: _Record, campaign_id: str) -> None:
        """The contact's enrolment in the attributed campaign becomes REPLIED and its sequence stops
        (not for an out-of-office auto-reply)."""
        enrollment = uow.sync.enrollment(campaign_id, record.prospect.prospect_id)
        if not enrollment or record.is_ooo:
            return
        try:
            set_prospect_status(enrollment, "REPLIED")
            self._stop_sequence_on_reply(uow, campaign_id, record.prospect.prospect_id)
        except Exception as exc:
            logger.warning(f"[IMAP] Could not stop sequence after reply: {exc}")

    def _stop_sequence_on_reply(self, uow: Any, campaign_id: str, prospect_id: str) -> int:
        """Stop on reply (BR-DF-05, 20b): follow-ups are scheduled at launch, so cancel this contact's
        pending emails in the campaign -- step emails under each step's stop_on_reply flag (NULL counts
        as on), and emails of no step (automation mail), as before."""
        stop_steps = self._stop_steps or sequence_stop.stop_pending_steps
        stopped = stop_steps(uow.session, campaign_id, prospect_id, "REPLIED", failure_reason=REPLY_STOP_REASON)
        return int(stopped or 0) + uow.sync.cancel_pending_without_step(campaign_id, prospect_id, REPLY_STOP_REASON)

    @staticmethod
    def _opt_out(uow: Any, record: _Record) -> None:
        """A reply that clearly asks to unsubscribe suppresses the contact in every campaign of the
        workspace (BR-DF-08); the reply is labelled here and not classified again."""
        prospect, parsed = record.prospect, record.parsed
        opt_out = inbox_reply_rules.pre_classify(parsed.subject, _reply_text(parsed))
        if opt_out is None or not opt_out.suppresses:
            return
        message = record.message
        try:
            suppression.suppress(uow.session, prospect.tenant_id, prospect.email,
                                 "Prospect replied with unsubscribe", kind="UNSUBSCRIBE",
                                 source=f"reply_unsubscribe:{message.message_id}")
            if record.replied_to:
                uow.sync.add(EmailEvent(message_id=record.replied_to.message_id,
                                        event_type=EmailEvent.EVENT_UNSUBSCRIBE, event_time=parsed.sent_at))
            apply_label(message, opt_out)
            logger.info(f"[IMAP] Auto-unsubscribed {prospect.email} (reply-based)")
        except Exception as e:
            logger.warning(f"[IMAP] Failed to auto-unsubscribe {prospect.email}: {e}")

    @staticmethod
    def _reply_events(uow: Any, record: _Record) -> None:
        """REPLY event on the sent email replied to (plus REPLY_OOO for an auto-reply: additive, analytics
        derives clean replies as replied - ooo) and the email.replied webhook (BR-SF-20)."""
        replied_to = record.replied_to
        if replied_to is None:
            return
        at = record.parsed.sent_at
        uow.sync.add(EmailEvent(message_id=replied_to.message_id, event_type=EmailEvent.EVENT_REPLY, event_time=at))
        logger.info(f"[IMAP] Created REPLY event for msg {replied_to.message_id}")
        if record.is_ooo:
            uow.sync.add(EmailEvent(message_id=replied_to.message_id, event_type=EmailEvent.EVENT_REPLY_OOO,
                                    event_time=at))
        else:
            # Committed with this message's transaction
            webhooks.email_replied(uow.session, record.prospect.tenant_id, record.message, replied_to,
                                   record.prospect)

    def _classify_reply(self, reply_message_id: str, replied_to_message_id: Optional[str]) -> None:
        """
        Reply classification (S-09) after the reply's own transaction committed, so the LLM round trip
        holds no transaction or row lock (T-1). The label, its events and an unsubscribe are written by
        ReplyClassificationService in a second short transaction; a failure leaves the reply unlabelled
        for the reply_classification job.
        """
        try:
            self._classifier.classify_and_store(reply_message_id, replied_to_message_id)
        except Exception as exc:
            self._end_transaction()
            logger.warning(f"[IMAP] Reply classification failed (retried by the job): {exc}")


# Module-level instance for the background job (sync_all_active_inboxes(db) works on that session)
imap_sync_service = IMAPSyncService()
