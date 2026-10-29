"""
A campaign's step emails (ADR 0009, chunk A2): the pre-scheduled EmailMessage rows, their states,
mailbox bindings and subject-variant assignments, plus the manual engagement events.

Part of the campaign aggregate (``CampaignsUnitOfWork.messages``). Every helper takes a campaign (or a
step / template of one) the caller already scoped to the tenant. Nothing here commits; ``insert``
uses savepoints so a duplicate step email (one-email-per-step key) is skipped, not fatal.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.campaigns import REMOVED_BY_USER
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.repositories.reports_campaign_metrics import sent_message_filters, unique_prospects_expr

logger = logging.getLogger(__name__)

PENDING = ("QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN")


def _outbound() -> Any:
    return func.coalesce(EmailMessage.direction, "OUTBOUND") != "INBOUND"


class CampaignMessagesRepository:
    def __init__(self, session: Session) -> None:
        self._s = session

    def flush(self) -> None:
        self._s.flush()

    def add(self, entity: object) -> None:
        self._s.add(entity)

    # ── step emails of a campaign ──────────────────────────────────────────────────────────────

    def get(self, campaign_id: str, message_id: str) -> Optional[EmailMessage]:
        return self._s.execute(select(EmailMessage).where(
            EmailMessage.message_id == message_id, EmailMessage.campaign_id == campaign_id)).scalars().first()

    def by_ids(self, message_ids: Iterable[str]) -> list[EmailMessage]:
        ids = list(set(message_ids))
        if not ids:
            return []
        return list(self._s.execute(select(EmailMessage).where(EmailMessage.message_id.in_(ids))).scalars())

    def unsent(self, campaign_id: str, statuses: Sequence[str] = PENDING) -> list[EmailMessage]:
        """Not-yet-sent emails in these states, per contact in planned order."""
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(statuses)),
        ).order_by(EmailMessage.prospect_id.asc(), EmailMessage.scheduled_at.asc())).scalars())

    def lock_unsent(self, campaign_id: str) -> list[EmailMessage]:
        """Row-lock the campaign's unsent step emails (QUEUED / SCHEDULED / PAUSED_BY_CAMPAIGN) for the rest of
        the unit of work and refresh them: the sender cannot claim one meanwhile, and one it claimed just
        before is seen as SENDING (and left alone)."""
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(PENDING)),
        ).with_for_update().execution_options(populate_existing=True)).scalars())

    def step_email(self, campaign_id: str, prospect_id: str, sequence_id: str) -> Optional[EmailMessage]:
        """A contact's email of one step (the live one: a cancelled duplicate only when there is no other)."""
        rows = list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id == prospect_id,
            EmailMessage.sequence_id == sequence_id, _outbound())).scalars())
        rows.sort(key=lambda m: (m.status == "CANCELLED", m.sent_at is None))
        return rows[0] if rows else None

    def sent_newest_first(self, campaign_id: str) -> list[EmailMessage]:
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sent_at.isnot(None)
        ).order_by(EmailMessage.sent_at.desc())).scalars())

    def step_mails(self, campaign_id: str) -> list[EmailMessage]:
        """Every outbound step email of the campaign (any state)."""
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sequence_id.isnot(None), _outbound(),
        )).scalars())

    def existing_step_keys(self, campaign_id: str) -> set[tuple[str, str]]:
        """(prospect_id, sequence_id) of the step emails that exist. One cancelled because the user removed the
        contact does not count: a contact re-enrolled after removal (never emailed, S-21) gets its steps again."""
        rows = self._s.execute(select(EmailMessage.prospect_id, EmailMessage.sequence_id, EmailMessage.status,
                                      EmailMessage.failure_reason).where(EmailMessage.campaign_id == campaign_id)).all()
        return {(pid, sid) for pid, sid, status, reason in rows
                if not (status == "CANCELLED" and reason == REMOVED_BY_USER)}

    def bound_inboxes(self, campaign_id: str, inbox_ids: Sequence[str]) -> list[tuple[str, str]]:
        """(prospect_id, inbox_id) of step emails bound to these mailboxes, sent ones (newest) first."""
        if not inbox_ids:
            return []
        return [(pid, iid) for pid, iid in self._s.execute(
            select(EmailMessage.prospect_id, EmailMessage.inbox_id).where(
                EmailMessage.campaign_id == campaign_id, EmailMessage.inbox_id.in_(list(inbox_ids)),
            ).order_by(EmailMessage.sent_at.is_(None), EmailMessage.sent_at.desc())).all()]

    def insert(self, messages: Sequence[EmailMessage]) -> int:
        """
        Insert pre-scheduled step emails, tolerating the one-email-per-step key (item 19): a concurrent
        pre-schedule (double launch, enrolment during launch) may already have written some of them. All
        rows go in one savepoint; if the key refuses one, each row is retried in its own savepoint and the
        duplicates are skipped. Returns how many were added.
        """
        if not messages:
            self._s.flush()
            return 0
        try:
            with self._s.begin_nested():
                self._s.add_all(list(messages))
        except IntegrityError:
            logger.warning("[PRESCHEDULE] Some steps already have their email; inserting one by one")
        else:
            return len(messages)
        created = 0
        for msg in messages:
            try:
                with self._s.begin_nested():
                    self._s.add(msg)
                created += 1
            except IntegrityError:
                logger.info("[PRESCHEDULE] Skipped duplicate step email for prospect %s, step %s",
                            msg.prospect_id, msg.sequence_id)
        return created

    def set_status(self, campaign_id: str, from_statuses: Sequence[str], to_status: str) -> int:
        return int(self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.status.in_(list(from_statuses)),
        ).update({"status": to_status}, synchronize_session=False))

    def cancel_contact(self, campaign_id: str, prospect_id: str, reason: str) -> int:
        return int(self._s.query(EmailMessage).filter(
            EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id == prospect_id,
            EmailMessage.status.in_(list(PENDING)),
        ).update({"status": "CANCELLED", "failure_reason": reason}, synchronize_session=False))

    def unsent_of_template(self, template_id: str) -> list[EmailMessage]:
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.template_id == template_id, EmailMessage.status.in_(list(PENDING)))).scalars())

    def unsent_of_step(self, sequence_id: str) -> list[EmailMessage]:
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.status.in_(list(PENDING)),
            EmailMessage.sent_at.is_(None), _outbound())).scalars())

    def step_day(self, campaign_id: str, sequence_ids: Sequence[str], day: date) -> list[EmailMessage]:
        return list(self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sequence_id.in_(list(sequence_ids)),
            EmailMessage.status.in_(list(PENDING)), func.date(EmailMessage.scheduled_at) == day)).scalars())

    def sent_or_sending_of_step(self, sequence_id: str) -> int:
        """Emails of a step that went out or are being sent (then the step can't be deleted)."""
        return int(self._s.execute(select(func.count(EmailMessage.message_id)).where(
            EmailMessage.sequence_id == sequence_id,
            (EmailMessage.sent_at.isnot(None)) | (EmailMessage.status == "SENDING"))).scalar() or 0)

    def delete_step_emails(self, sequence_id: str) -> int:
        """Delete a step's (unsent) emails and their events; returns how many emails."""
        ids = list(self._s.execute(select(EmailMessage.message_id).where(
            EmailMessage.sequence_id == sequence_id)).scalars())
        if not ids:
            return 0
        self._s.query(EmailEvent).filter(EmailEvent.message_id.in_(ids)).delete(synchronize_session=False)
        return int(self._s.query(EmailMessage).filter(EmailMessage.message_id.in_(ids)).delete(
            synchronize_session=False))

    def planned_per_day(self, campaign_id: str) -> dict[date, int]:
        """Unsent step emails per (UTC) day they are planned for."""
        day = func.date(EmailMessage.scheduled_at)
        rows = self._s.execute(select(day, func.count(EmailMessage.message_id)).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.sent_at.is_(None),
            EmailMessage.status.in_(list(PENDING)), EmailMessage.scheduled_at.isnot(None),
        ).group_by(day)).all()
        out: dict[date, int] = {}
        for d, n in rows:
            if d is None:
                continue
            out[d if isinstance(d, date) else date.fromisoformat(str(d))] = int(n)
        return out

    # ── manual engagement (mark replied / opened) ──────────────────────────────────────────────

    def latest_sent(self, campaign_id: str, prospect_id: str) -> Optional[EmailMessage]:
        return self._s.execute(select(EmailMessage).where(
            EmailMessage.campaign_id == campaign_id, EmailMessage.prospect_id == prospect_id,
            EmailMessage.status == "SENT").order_by(EmailMessage.sent_at.desc())).scalars().first()

    def reply_event(self, message_id: str) -> Optional[EmailEvent]:
        return self._s.execute(select(EmailEvent).where(
            EmailEvent.message_id == message_id, EmailEvent.event_type == EmailEvent.EVENT_REPLY)).scalars().first()

    def add_event(self, message_id: str, event_type: int, when: datetime, metadata: dict[str, Any]) -> EmailEvent:
        event = EmailEvent(message_id=message_id, event_type=event_type, event_time=when, event_metadata=metadata)
        self._s.add(event)
        return event

    # ── subject variants (BR-SF-18) ────────────────────────────────────────────────────────────

    def unsent_step_emails(self, sequence_id: str) -> list[EmailMessage]:
        """A step's outbound emails that have not left yet (SENDING excluded)."""
        return self.unsent_of_step(sequence_id)

    def variant_sent_counts(self, sequence_id: str) -> dict[str, int]:
        rows = self._s.execute(select(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.isnot(None),
            *sent_message_filters()).group_by(EmailMessage.subject_variant)).all()
        return {label: int(n) for label, n in rows}

    def variant_assignment_counts(self, sequence_id: str) -> dict[str, int]:
        """Emails per variant of a step, whatever their state except CANCELLED."""
        rows = self._s.execute(select(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.isnot(None),
            EmailMessage.status != "CANCELLED").group_by(EmailMessage.subject_variant)).all()
        return {label: int(n) for label, n in rows}

    def variant_scheduled_counts(self, sequence_id: str) -> dict[str, int]:
        rows = self._s.execute(select(EmailMessage.subject_variant, func.count(EmailMessage.message_id)).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.isnot(None),
            EmailMessage.status.in_(list(PENDING)), EmailMessage.sent_at.is_(None),
        ).group_by(EmailMessage.subject_variant)).all()
        return {label: int(n) for label, n in rows}

    def variant_unique_contacts(self, sequence_id: str, event_type: int) -> dict[str, int]:
        rows = self._s.execute(select(EmailMessage.subject_variant, unique_prospects_expr()).join(
            EmailEvent, EmailEvent.message_id == EmailMessage.message_id
        ).where(EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.isnot(None),
                EmailEvent.event_type == event_type).group_by(EmailMessage.subject_variant)).all()
        return {label: int(n) for label, n in rows}

    def variant_messages_with_event(self, sequence_id: str, event_type: int) -> dict[str, int]:
        """Distinct emails per variant with at least one event of the type (replied, bounced)."""
        rows = self._s.execute(select(
            EmailMessage.subject_variant, func.count(func.distinct(EmailEvent.message_id))
        ).join(EmailEvent, EmailEvent.message_id == EmailMessage.message_id).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.isnot(None),
            EmailEvent.event_type == event_type).group_by(EmailMessage.subject_variant)).all()
        return {label: int(n) for label, n in rows}

    def variant_replied(self, sequence_id: str) -> dict[str, int]:
        return self.variant_messages_with_event(sequence_id, EmailEvent.EVENT_REPLY)

    def variant_bounced(self, sequence_id: str) -> dict[str, int]:
        """Recipient bounces per variant (EVENT_BOUNCE; sender-side bounces are not the subject's fault and,
        as in the campaign analytics, do not reduce delivered)."""
        return self.variant_messages_with_event(sequence_id, EmailEvent.EVENT_BOUNCE)

    def unassigned_sent(self, sequence_id: str) -> int:
        return int(self._s.execute(select(func.count(EmailMessage.message_id)).where(
            EmailMessage.sequence_id == sequence_id, EmailMessage.subject_variant.is_(None),
            *sent_message_filters())).scalar() or 0)

    def step_page(self, sequence_id: str, label: Optional[str], limit: int,
                  offset: int) -> tuple[int, list[tuple[EmailMessage, Optional[str], Optional[str]]]]:
        """(total, [(email, first_name, last_name)]) of a step's outbound emails, sent history first."""
        q = select(EmailMessage, Prospect.first_name, Prospect.last_name).join(
            Prospect, Prospect.prospect_id == EmailMessage.prospect_id
        ).where(EmailMessage.sequence_id == sequence_id, _outbound())
        if label:
            q = q.where(EmailMessage.subject_variant == label)
        total = int(self._s.execute(select(func.count()).select_from(q.subquery())).scalar() or 0)
        rows = self._s.execute(q.order_by(EmailMessage.sent_at.is_(None), EmailMessage.sent_at.desc(),
                                          EmailMessage.scheduled_at.asc(), EmailMessage.message_id)
                               .offset(offset).limit(limit)).all()
        return total, [(m, first, last) for m, first, last in rows]
