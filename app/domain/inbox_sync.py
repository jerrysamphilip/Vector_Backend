"""
IMAP reply sync (BR-DF-05, T-1): pure steps of one folder sync. No I/O: the service fetches over IMAP,
these functions turn the server's answers into values, and app/repositories/inbox_sync.py stores them.

  parse_uidvalidity / search_criteria / new_uids   which messages to fetch (UID position or date window)
  parse_message                                    one fetched RFC 5322 message -> ``ParsedMessage``
  ooo_label                                        the label of an out-of-office reply
  sync_due / days_back_for                         when an inbox is synced and how far back
"""

from __future__ import annotations

import email
import email.message
import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, List, Optional, Tuple

from app.domain import inbox_reply_rules
from app.domain.inbox import LabelSource, ReplyClassification, ReplyLabel

INBOUND = "INBOUND"
OUTBOUND = "OUTBOUND"

# Pending statuses a reply cancels (stop on reply, BR-DF-05)
STOPPABLE_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")
REPLY_STOP_REASON = "Stopped: the contact replied"
# How many recent sent emails of the contact the subject fallback compares
SUBJECT_MATCH_WINDOW = 50
# Sync each inbox about every 4 minutes, so replies show within 10 (BR-DF-05)
SYNC_INTERVAL_SECONDS = 240
# Date window used only when a folder has no UID position yet: back to the last sync (minus an
# overlap), at most MAX_DAYS_BACK days
MAX_DAYS_BACK = 7
SINCE_OVERLAP = timedelta(days=1)
SENT_FOLDERS = ("Sent", "[Gmail]/Sent Mail", "Sent Items", "Sent Messages")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_UNSUB_PHRASES = {"unsubscribe", "please unsubscribe", "please unsubscribe me", "unsubscribe me", "remove me",
                  "please remove me", "remove me from your list", "opt out", "opt-out", "stop"}


@dataclass(frozen=True, slots=True)
class InboxRef:
    """The inbox fields the folder sync needs, read once before the IMAP session starts (T-1)."""
    inbox_id: str
    tenant_id: Optional[str]
    email_address: str


@dataclass(frozen=True, slots=True)
class SyncPosition:
    uidvalidity: Optional[int]
    last_uid: Optional[int]


@dataclass(frozen=True, slots=True)
class FolderCursor:
    """Where a folder sync stands: the stored position (None: none yet, False: table unusable), the
    server's UIDVALIDITY and the last UID already processed."""
    folder: str
    state: Any
    uidvalidity: Optional[int]
    last_uid: int

    @property
    def can_store(self) -> bool:
        return self.state is not False

    @property
    def position_changed(self) -> bool:
        """A position worth storing although no message is new (first sync or a new UIDVALIDITY)."""
        if self.uidvalidity is None or self.state is False:
            return False
        return self.state is None or self.state.uidvalidity != self.uidvalidity


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    subject: str
    from_header: str
    to_header: str
    sender_email: str
    receiver_email: str
    raw_body: str
    body: str
    provider_message_id: str
    sent_at: datetime
    in_reply_to: Optional[str]
    references: Optional[str]
    auto_submitted: Optional[str]

    def contact_email(self, direction: str) -> str:
        """The contact's address: the sender of a received email, the recipient of a sent one."""
        return self.sender_email if direction == INBOUND else self.receiver_email


def is_unsubscribe_reply(text: Optional[str]) -> bool:
    """A short reply whose first line asks to unsubscribe (a signature or quoted mail may follow)."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    first = re.sub(r"[^a-z\- ]", "", lines[0].lower()).strip()
    return first in _UNSUB_PHRASES and len(lines[0]) <= 60


# ── Scheduling ────────────────────────────────────────────────────────────────
def sync_due(last_sync_at: Optional[datetime], now: datetime) -> bool:
    return not last_sync_at or (now - last_sync_at).total_seconds() >= SYNC_INTERVAL_SECONDS


def days_back_for(last_sync_at: Optional[datetime], now: datetime) -> int:
    if not last_sync_at:
        return MAX_DAYS_BACK
    since = last_sync_at - SINCE_OVERLAP
    return min(MAX_DAYS_BACK, max(1, (now - since).days + 1))


# ── Folder selection and search ───────────────────────────────────────────────
def quote_mailbox(name: str) -> str:
    if name.startswith('"') or not any(c in name for c in ' []/%*"\\'):
        return name
    return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'


def parse_uidvalidity(response: Any) -> Optional[int]:
    """UIDVALIDITY from ``mail.response('UIDVALIDITY')``'s data, None when absent or unreadable."""
    try:
        if response and response[0]:
            return int(response[0])
    except (TypeError, ValueError):
        return None
    return None


def start_uid(state: Any, uidvalidity: Optional[int]) -> int:
    """The last UID already synced: the stored one while UIDVALIDITY is unchanged, else 0."""
    if state and state.uidvalidity is not None and state.uidvalidity == uidvalidity:
        return int(state.last_uid or 0)
    return 0


def imap_date(day: date) -> str:
    """RFC 3501 date (English month names whatever the locale)."""
    return f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}"


def search_criteria(last_uid: int, days_back: int, today: datetime) -> Tuple[str, str]:
    """(IMAP UID SEARCH criteria, description for the log): after the UID position, else the date window."""
    if last_uid:
        return f'(UID {last_uid + 1}:*)', f"UID > {last_uid}"
    cutoff = imap_date((today - timedelta(days=days_back)).date())
    return f'(SINCE "{cutoff}")', f"SINCE {cutoff}"


def new_uids(search_data: Any, last_uid: int) -> List[int]:
    """UIDs above the position, ascending ("UID n:*" always returns the newest message, even when <= n)."""
    raw = (search_data[0] if search_data else b"") or b""
    return [u for u in sorted({int(u) for u in raw.split() if u.isdigit()}) if u > last_uid]


def fetched_bytes(status: str, msg_data: Any) -> Optional[bytes]:
    """The raw message of a ``UID FETCH (BODY.PEEK[])`` answer, None when there is none."""
    if status != 'OK' or not msg_data or not isinstance(msg_data[0], tuple):
        return None
    raw = msg_data[0][1]
    return raw if isinstance(raw, bytes) else None


# ── One message ───────────────────────────────────────────────────────────────
def decode_subject(msg: Any) -> str:
    subject = ""
    for part, encoding in decode_header(msg["Subject"] or ""):
        if isinstance(part, bytes):
            subject += part.decode(encoding or "utf-8", errors="ignore")
        else:
            subject += part
    return subject


def _decoded(part: email.message.Message) -> Optional[str]:
    payload = part.get_payload(decode=True)
    return payload.decode(errors="ignore") if isinstance(payload, bytes) else None


def plain_body(msg: email.message.Message) -> str:
    """The first text/plain part (or the whole payload of a single-part message), decoded."""
    if not msg.is_multipart():
        return _decoded(msg) or ""
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            text = _decoded(part)
            if text is not None:
                return text
    return ""


def synthetic_message_id(from_: Optional[str], date_str: Optional[str], subject: Optional[str],
                         to_: Optional[str]) -> str:
    """Stable id for a message with no Message-ID header, so it is stored once (B11)."""
    digest = hashlib.sha256(
        "\x1f".join([(from_ or "").strip().lower(), (to_ or "").strip().lower(),
                     (date_str or "").strip(), (subject or "").strip()]).encode("utf-8", "ignore")
    ).hexdigest()[:40]
    return f"<no-message-id-{digest}@imap-sync.invalid>"


def parse_date(date_str: Optional[str], default: datetime) -> datetime:
    """The Date header as naive UTC; ``default`` when missing or unparseable."""
    if not date_str:
        return default
    try:
        sent_at = parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return default
    if sent_at.tzinfo:
        sent_at = sent_at.astimezone(timezone.utc).replace(tzinfo=None)
    return sent_at


def parse_message(raw: bytes, now: datetime, latest_text: Callable[[str], str]) -> Optional[ParsedMessage]:
    """The fields the sync stores, or None when From or To is missing. ``latest_text`` cuts a body down
    to its newest human-readable reply block (app/utils/email_utils.extract_latest_message_text)."""
    msg = email.message_from_bytes(raw)
    from_ = msg.get("From") or ""
    to_ = msg.get("To") or ""
    sender = parseaddr(from_)[1].lower().strip()
    receiver = parseaddr(to_)[1].lower().strip()
    if not sender or not receiver:
        return None
    subject = decode_subject(msg)
    date_str = msg.get("Date")
    raw_body = plain_body(msg)
    return ParsedMessage(
        subject=subject, from_header=from_, to_header=to_, sender_email=sender, receiver_email=receiver,
        raw_body=raw_body, body=latest_text(raw_body),
        provider_message_id=(msg.get("Message-ID") or "").strip() or synthetic_message_id(
            from_, date_str, subject, to_),
        sent_at=parse_date(date_str, now), in_reply_to=msg.get("In-Reply-To"),
        references=msg.get("References"), auto_submitted=msg.get("Auto-Submitted"),
    )


def header_summary(raw: bytes) -> dict:
    """From, To, Subject (80 characters), Date and Message-ID of a message (the IMAP connection test)."""
    msg = email.message_from_bytes(raw)
    return {
        "from": parseaddr(msg.get("From", ""))[1],
        "to": parseaddr(msg.get("To", ""))[1],
        "subject": decode_subject(msg)[:80],
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
    }


def ooo_label(auto_submitted: Optional[str]) -> ReplyClassification:
    """The label of a reply the sync already recognised as an out-of-office auto-reply."""
    if inbox_reply_rules.is_auto_reply_header({"Auto-Submitted": auto_submitted}):
        return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 1.0, LabelSource.HEADER, "Auto-Submitted header")
    return ReplyClassification(ReplyLabel.OUT_OF_OFFICE, 0.8, LabelSource.RULES, "Out-of-office wording")
