"""
SQL of the sending queue (chunk A8, ADR 0009): the due batch, the atomic claim, the stuck-SENDING sweep, the
conditional SENT, per-step stops and the follow-up / resume re-timing.

The worker sends for every workspace, so these entry points are deliberately not tenant-scoped: a message is
reached by its id, and the due query excludes suspended / deleted workspaces (``BLOCKED_TENANT_STATUSES``).
Repositories stage and flush; the services commit at the sending protocol's commit points (claim, sent).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple

from sqlalchemy import Exists, and_, exists, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, object_session

from app.core.auth import BLOCKED_TENANT_STATUSES
from app.domain.sending import (
    DUE_STATUSES, FOLLOW_UP_STATUSES, PENDING_STEP_STATUSES, RESUME_STATUSES, DueRow, SendKeyConflict,
)
from app.models.campaign import Campaign
from app.models.email_message import EmailEvent, EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.sending_inbox import SendingInbox
from app.models.tenant import Tenant
from app.schemas.campaign_schema import CampaignStatus

__all__ = ["Session", "SendingQueueRepository", "SqlAlchemySendingQueueRepository", "earlier_step_pending",
           "session_or_none"]


def session_or_none(entity: Any) -> Optional[Session]:
    """The session a loaded entity belongs to (None when it is detached)."""
    return object_session(entity)


def earlier_step_pending(msg_entity: Any, step_entity: Any) -> Exists:
    """
    SQL condition: this prospect still has an earlier step of the same campaign that has not been sent
    yet (B01). A follow-up is only due once the step before it has actually gone out, whatever its
    scheduled_at says.
    """
    prev = aliased(EmailMessage)
    prev_step = aliased(EmailSequence)
    return exists().where(
        prev.campaign_id == msg_entity.campaign_id,
        prev.prospect_id == msg_entity.prospect_id,
        prev.message_id != msg_entity.message_id,
        prev.sent_at.is_(None),
        prev.status.in_(PENDING_STEP_STATUSES),
        prev_step.sequence_id == prev.sequence_id,
        prev_step.step_number < step_entity.step_number,
    )


class SendingQueueRepository(Protocol):
    def select_due(self, now: datetime, lane_cap: int, limit: int) -> List[DueRow]: ...

    def earlier_step_unsent(self, msg: EmailMessage) -> bool: ...

    def load_message(self, message_id: str) -> Optional[EmailMessage]: ...

    def duplicate_of(self, msg: EmailMessage) -> Optional[str]: ...

    def claim(self, message_id: str, key: Optional[str], now: datetime) -> int: ...

    def cancel_duplicate(self, message_id: str, reason: str) -> int: ...

    def date_undated_claims(self, now: datetime) -> int: ...

    def stuck_sending(self, cutoff: datetime) -> List[EmailMessage]: ...

    def provider_event_ids(self, message_ids: Sequence[str]) -> Set[str]: ...

    def mark_sent(self, msg: EmailMessage, values: Dict[str, Any]) -> bool: ...

    def stoppable_steps(self, campaign_id: str, prospect_id: str) -> List[Tuple[str, EmailSequence]]: ...

    def cancel(self, message_ids: Sequence[str], reason: str) -> int: ...

    def unsent_follow_ups(self, campaign_id: str, prospect_id: str, sequence_id: str) -> List[EmailMessage]: ...

    def pending_for_resume(self, campaign_id: str) -> List[EmailMessage]: ...

    def rebind_unsent(self, campaign_id: str, prospect_id: str, exclude_message_id: str,
                      inbox_id: str, from_email: Optional[str]) -> int: ...

    def record_sent_event(self, message_id: str, now: datetime, ses_message_id: Optional[str] = ...) -> None: ...

    def count_sent_to_inbox(self, inbox_id: str, now: datetime) -> None: ...


class SqlAlchemySendingQueueRepository:
    """``SendingQueueRepository`` over a SQLAlchemy session."""

    def __init__(self, session: Session) -> None:
        self._s = session

    @classmethod
    def for_session(cls, db: Session) -> "SqlAlchemySendingQueueRepository":
        return cls(db)

    # ── Due batch (B04) ─────────────────────────────────────────────────────

    def select_due(self, now: datetime, lane_cap: int, limit: int) -> List[DueRow]:
        """
        Due rows as (message_id, inbox_id, lane), fair across lanes.

        A lane is the sending inbox (or the campaign, for messages with no inbox). ROW_NUMBER() per lane
        caps each lane at ``lane_cap`` rows and the result is ordered round-robin (every lane's 1st row,
        then every lane's 2nd, ...), so a big backlog in one inbox or tenant cannot crowd out the rest.
        Only ACTIVE campaigns of active workspaces; follow-ups whose previous step is unsent are held (B01).
        """
        step = aliased(EmailSequence)
        lane = func.coalesce(EmailMessage.inbox_id, EmailMessage.campaign_id)
        rn = func.row_number().over(
            partition_by=lane, order_by=(EmailMessage.scheduled_at, EmailMessage.message_id)
        )
        inner = (
            self._s.query(
                EmailMessage.message_id.label("message_id"),
                EmailMessage.inbox_id.label("inbox_id"),
                lane.label("lane"),
                EmailMessage.scheduled_at.label("scheduled_at"),
                rn.label("rn"),
            )
            .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id)
            .join(Tenant, Tenant.tenant_id == Campaign.tenant_id)
            .outerjoin(step, step.sequence_id == EmailMessage.sequence_id)
            .filter(
                EmailMessage.status.in_(list(DUE_STATUSES)),
                EmailMessage.scheduled_at <= now,
                EmailMessage.sent_at.is_(None),  # never re-process an email that was already sent
                Campaign.status == CampaignStatus.ACTIVE.value,
                func.upper(func.coalesce(Tenant.status, "ACTIVE")).notin_(sorted(BLOCKED_TENANT_STATUSES)),
                or_(EmailMessage.next_retry_at.is_(None), EmailMessage.next_retry_at <= now),
                ~earlier_step_pending(EmailMessage, step),
            )
            .subquery()
        )
        rows = (
            self._s.query(inner.c.message_id, inner.c.inbox_id, inner.c.lane)
            .filter(inner.c.rn <= lane_cap)
            .order_by(inner.c.rn, inner.c.scheduled_at, inner.c.message_id)
            .limit(limit)
            .all()
        )
        return [DueRow(message_id, inbox_id, lane_key) for message_id, inbox_id, lane_key in rows]

    def earlier_step_unsent(self, msg: EmailMessage) -> bool:
        """Python twin of earlier_step_pending, for a single message."""
        if not (msg.campaign_id and msg.sequence_id):
            return False
        step = aliased(EmailSequence)
        return self._s.query(EmailMessage.message_id).join(
            step, step.sequence_id == msg.sequence_id
        ).filter(
            EmailMessage.message_id == msg.message_id,
            earlier_step_pending(EmailMessage, step),
        ).first() is not None

    def load_message(self, message_id: str) -> Optional[EmailMessage]:
        message: Optional[EmailMessage] = self._s.query(EmailMessage).filter(
            EmailMessage.message_id == message_id).first()
        return message

    # ── Claim (BR-DF-06) ────────────────────────────────────────────────────

    def duplicate_of(self, msg: EmailMessage) -> Optional[str]:
        """Another message of this campaign step to the same address that was sent or is being sent."""
        row = self._s.query(EmailMessage.message_id).filter(
            EmailMessage.campaign_id == msg.campaign_id,
            EmailMessage.sequence_id == msg.sequence_id,
            func.lower(EmailMessage.to_email) == msg.to_email.strip().lower(),
            EmailMessage.message_id != msg.message_id,
            or_(EmailMessage.sent_at.isnot(None), EmailMessage.status.in_(["SENDING", "SENT"])),
        ).first()
        return row[0] if row else None

    def claim(self, message_id: str, key: Optional[str], now: datetime) -> int:
        """QUEUED/SCHEDULED -> SENDING with the send_key and claim time; 0 when someone else has it.
        Raises SendKeyConflict when a concurrent worker holds the same send_key (the session must be
        rolled back)."""
        try:
            rows: int = self._s.query(EmailMessage).filter(
                EmailMessage.message_id == message_id,
                EmailMessage.status.in_(list(DUE_STATUSES)),
            ).update({"status": "SENDING", "send_key": key, "claimed_at": now}, synchronize_session=False)
        except IntegrityError as exc:
            raise SendKeyConflict(str(exc.orig)) from exc
        return rows

    def claim_for_send_now(self, message_id: str, now: datetime) -> int:
        """"Send now"'s claim: QUEUED/SCHEDULED -> SENDING, dated so the stuck sweep only reaps it once stale."""
        rows: int = self._s.query(EmailMessage).filter(
            EmailMessage.message_id == message_id,
            EmailMessage.status.in_(list(DUE_STATUSES)),
        ).update({"status": "SENDING", "claimed_at": now}, synchronize_session=False)
        return rows

    def cancel_duplicate(self, message_id: str, reason: str) -> int:
        rows: int = self._s.query(EmailMessage).filter(
            EmailMessage.message_id == message_id,
            EmailMessage.status.in_(list(DUE_STATUSES)),
        ).update({"status": "CANCELLED", "failure_reason": reason}, synchronize_session=False)
        return rows

    # ── Stuck-SENDING sweep (BR-DF-06, B08) ─────────────────────────────────

    def date_undated_claims(self, now: datetime) -> int:
        """Start the stuck-send clock for legacy SENDING rows that carry no claimed_at."""
        rows: int = self._s.query(EmailMessage).filter(
            EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
            EmailMessage.claimed_at.is_(None),
        ).update({"claimed_at": now}, synchronize_session=False)
        return rows

    def stuck_sending(self, cutoff: datetime) -> List[EmailMessage]:
        return self._s.query(EmailMessage).filter(
            EmailMessage.status == "SENDING", EmailMessage.sent_at.is_(None),
            EmailMessage.claimed_at < cutoff,
        ).all()

    def provider_event_ids(self, message_ids: Sequence[str]) -> Set[str]:
        """Messages SES has already reported something other than our own SENT event for."""
        return {
            mid for (mid,) in self._s.query(EmailEvent.message_id).filter(
                EmailEvent.message_id.in_(list(message_ids)),
                EmailEvent.event_type != EmailEvent.EVENT_SENT,
            ).distinct()
        }

    # ── Sent ────────────────────────────────────────────────────────────────

    def mark_sent(self, msg: EmailMessage, values: Dict[str, Any]) -> bool:
        """
        Conditional SENT (item 15a): status becomes SENT only while the row is still SENDING; otherwise
        (a bounce / complaint the webhook recorded mid-send) only the send itself is recorded. Refreshes
        ``msg``; True when the row became SENT.
        """
        rows = self._s.query(EmailMessage).filter(
            EmailMessage.message_id == msg.message_id, EmailMessage.status == "SENDING",
        ).update({**values, "status": "SENT"}, synchronize_session=False)
        if not rows:
            self._s.query(EmailMessage).filter(
                EmailMessage.message_id == msg.message_id, EmailMessage.sent_at.is_(None),
            ).update(values, synchronize_session=False)
        self._s.refresh(msg)
        return bool(rows)

    def record_sent_event(self, message_id: str, now: datetime, ses_message_id: Optional[str] = None,
                          *, with_metadata: bool = True) -> None:
        self._s.add(EmailEvent(
            message_id=message_id, event_type=EmailEvent.EVENT_SENT, event_time=now,
            event_metadata={"ses_message_id": ses_message_id} if with_metadata else None,
        ))

    def count_sent_to_inbox(self, inbox_id: str, now: datetime) -> None:
        """Daily counter + last send of the inbox, in SQL so parallel lanes can't lose a count."""
        self._s.query(SendingInbox).filter(SendingInbox.inbox_id == inbox_id).update({
            SendingInbox.emails_sent_today: func.coalesce(SendingInbox.emails_sent_today, 0) + 1,
            SendingInbox.last_sent_at: now,
        }, synchronize_session=False)

    # ── Per-step stops (BR-DF-05, item 20) ──────────────────────────────────

    def stoppable_steps(self, campaign_id: str, prospect_id: str,
                        statuses: Iterable[str] = FOLLOW_UP_STATUSES) -> List[Tuple[str, EmailSequence]]:
        """(message_id, step) of this contact's unsent outbound step mail in this campaign."""
        rows = self._s.query(EmailMessage.message_id, EmailSequence).join(
            EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id
        ).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.direction == "OUTBOUND",
            EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(statuses)),
        ).all()
        return [(message_id, step) for message_id, step in rows]

    def cancel(self, message_ids: Sequence[str], reason: str,
               statuses: Iterable[str] = FOLLOW_UP_STATUSES) -> int:
        rows: int = self._s.query(EmailMessage).filter(
            EmailMessage.message_id.in_(list(message_ids)), EmailMessage.status.in_(list(statuses)),
        ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: reason},
                 synchronize_session=False)
        return rows

    # ── Re-timing (B01, B02) ────────────────────────────────────────────────

    def unsent_follow_ups(self, campaign_id: str, prospect_id: str, sequence_id: str) -> List[EmailMessage]:
        return self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.sequence_id == sequence_id,
            EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(FOLLOW_UP_STATUSES)),
        ).all()

    def pending_for_resume(self, campaign_id: str) -> List[EmailMessage]:
        return self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status.in_(list(RESUME_STATUSES)),
            EmailMessage.sent_at.is_(None),
        ).all()

    def rebind_unsent(self, campaign_id: str, prospect_id: str, exclude_message_id: str,
                      inbox_id: str, from_email: Optional[str]) -> int:
        """Move this contact's other unsent mail of the campaign to ``inbox_id`` (S-17: a new contact moved off
        a capped mailbox keeps one sender for the whole sequence)."""
        return int(self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.message_id != exclude_message_id,
            EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(FOLLOW_UP_STATUSES)),
        ).update({EmailMessage.inbox_id: inbox_id, EmailMessage.from_email: from_email},
                 synchronize_session=False))

    # ── "Send now" batch (cursor over step number, message id) ──────────────

    def send_now_criteria(self, campaign_id: str, started_at: Optional[datetime],
                          after: Optional[Tuple[int, str]]) -> List[Any]:
        """Due criteria of one "send now" run: the campaign's QUEUED/SCHEDULED mail whose previous step went
        out (B01); with ``started_at``, not follow-ups unlocked by a send of this run; after the cursor."""
        criteria: List[Any] = [
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.status.in_(list(DUE_STATUSES)),
            ~earlier_step_pending(EmailMessage, EmailSequence),
        ]
        if started_at is not None:
            prev, prev_step = aliased(EmailMessage), aliased(EmailSequence)
            criteria.append(~exists().where(
                prev.campaign_id == EmailMessage.campaign_id, prev.prospect_id == EmailMessage.prospect_id,
                prev.message_id != EmailMessage.message_id, prev.sent_at >= started_at,
                prev_step.sequence_id == prev.sequence_id, prev_step.step_number < EmailSequence.step_number))
        if after is not None:
            criteria.append(or_(EmailSequence.step_number > after[0],
                                and_(EmailSequence.step_number == after[0], EmailMessage.message_id > after[1])))
        return criteria

    def send_now_batch(self, criteria: Sequence[Any], limit: int) -> List[Tuple[str, int]]:
        """(message_id, step_number) in sequence order, at most ``limit``."""
        rows = self._s.query(EmailMessage).join(EmailSequence).filter(*criteria).with_entities(
            EmailMessage.message_id, EmailSequence.step_number
        ).order_by(EmailSequence.step_number, EmailMessage.message_id).limit(limit).all()
        return [(mid, step) for mid, step in rows]

    def messages(self, message_ids: Sequence[str]) -> List[EmailMessage]:
        if not message_ids:
            return []
        return self._s.query(EmailMessage).filter(EmailMessage.message_id.in_(list(message_ids))).all()

    def count_due(self, criteria: Sequence[Any]) -> int:
        count: Optional[int] = self._s.query(func.count(EmailMessage.message_id)).join(
            EmailSequence).filter(*criteria).scalar()
        return count or 0
