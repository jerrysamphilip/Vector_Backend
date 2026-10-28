"""
Unified inbox and reply classification value objects (ADR 0009, S-09 / S-10).

Pure values: no I/O, no SQLAlchemy. Routers build ``ConversationQuery`` from query parameters; the
conversation service and ``app/repositories/inbox.py`` take it unchanged. ``ReplyClassification``
is what the classifier (rules or LLM) returns and what is stored on the inbound email_messages row.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from app.domain.paging import Paging


class ReplyLabel(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    OUT_OF_OFFICE = "OUT_OF_OFFICE"
    UNSUBSCRIBE = "UNSUBSCRIBE"

    @classmethod
    def parse(cls, value: object) -> Optional[ReplyLabel]:
        """A label from free text ("positive", "out of office", "OOO", ...); None when unknown."""
        if isinstance(value, ReplyLabel):
            return value
        key = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        key = {"OOO": "OUT_OF_OFFICE", "OUTOFOFFICE": "OUT_OF_OFFICE", "UNSUB": "UNSUBSCRIBE"}.get(key, key)
        try:
            return cls(key)
        except ValueError:
            return None


class LabelSource(str, Enum):
    HEADER = "HEADER"   # RFC 3834 Auto-Submitted (out of office)
    RULES = "RULES"     # rule-based classifier (no key, over budget, LLM failed, or a plain opt-out)
    LLM = "LLM"         # LLM through app/services/llm_budget.py
    SYSTEM = "SYSTEM"   # not a reply: a system notice (the SES bounce note), never classified


# An UNSUBSCRIBE at or above this confidence suppresses the contact; below it the reply is only
# flagged for review (wrongly blocking a real lead is silent and permanent).
UNSUBSCRIBE_AUTO_CONFIDENCE = 0.85
# Classification attempts per reply (LLM errors are retried by the reply_classification job; the
# last attempt falls back to the rules so every reply ends up labelled).
MAX_CLASSIFY_ATTEMPTS = 3
# Replies older than this are not (re)classified by the job.
CLASSIFY_LOOKBACK_DAYS = 30


# System notices (UAT defect 2): the "Delivery Failed" note the SES webhook writes into a thread when a sent
# email hard-bounces. It is an INBOUND row (it shows in the thread) but not a reply from the contact: its
# sender is this address and its reply_label_source is SYSTEM (so the reply classifier skips it).
SYSTEM_NOTICE_SENDER = "MAILER-DAEMON@amazonses.com"
BOUNCE_NOTICE_SUBJECT_PREFIX = "Delivery Failed: "
BOUNCE_NOTICE_BODY_PREFIX = "Email delivery failed ("


def is_system_notice(message: Any) -> bool:
    """True for a system notice in a thread (not a reply). ``message`` has ``direction``, ``from_email``,
    ``reply_label_source``, ``subject`` and ``body_text``. Notes written before the marker existed (sender =
    the contact) are recognised by the note's fixed wording."""
    if (getattr(message, "direction", None) or "OUTBOUND") != "INBOUND":
        return False
    if getattr(message, "reply_label_source", None) == LabelSource.SYSTEM.value:
        return True
    if (getattr(message, "from_email", None) or "").lower() == SYSTEM_NOTICE_SENDER.lower():
        return True
    return ((getattr(message, "subject", None) or "").startswith(BOUNCE_NOTICE_SUBJECT_PREFIX)
            and (getattr(message, "body_text", None) or "").startswith(BOUNCE_NOTICE_BODY_PREFIX))


@dataclass(frozen=True, slots=True)
class ReplyClassification:
    label: ReplyLabel
    confidence: float
    source: LabelSource
    reason: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")

    @property
    def suppresses(self) -> bool:
        """An opt-out confident enough to suppress the contact without a person looking at it."""
        return self.label is ReplyLabel.UNSUBSCRIBE and self.confidence >= UNSUBSCRIBE_AUTO_CONFIDENCE

    @property
    def needs_review(self) -> bool:
        """A possible opt-out that is not certain enough to act on: flagged for a person."""
        return self.label is ReplyLabel.UNSUBSCRIBE and not self.suppresses


MAX_SEARCH_CHARS = 200


@dataclass(frozen=True, slots=True)
class ConversationQuery:
    """Unified inbox listing: optional search (contact name, email, subject), campaign filter, paging."""

    paging: Paging = Paging(1, 20)
    search: Optional[str] = None
    campaign_id: Optional[str] = None
    label: Optional[ReplyLabel] = None

    def __post_init__(self) -> None:
        term = (self.search or "").strip()
        if len(term) > MAX_SEARCH_CHARS:
            raise ValueError(f"search must be at most {MAX_SEARCH_CHARS} characters")
        object.__setattr__(self, "search", term or None)
        object.__setattr__(self, "campaign_id", (self.campaign_id or "").strip() or None)


@dataclass(frozen=True, slots=True)
class OwnerScope:
    """Contacts whose conversations a user may see: ``owner_ids`` None = everyone's (BR-SH-02)."""

    owner_ids: Optional[frozenset[str]] = None
    include_unowned: bool = True

    @property
    def unrestricted(self) -> bool:
        return self.owner_ids is None

    def allows(self, owner_id: Optional[str]) -> bool:
        if self.owner_ids is None:
            return True
        if owner_id is None:
            return self.include_unowned
        return owner_id in self.owner_ids
