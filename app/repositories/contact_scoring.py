"""
Contact scoring repository (ADR 0009, chunk A7; BR-CM-43, CM-083).

The SQL behind app/services/contact_scoring.py: the workspace's scoring model row, the contact
properties and activity a score is computed from, storing scores (without touching updated_at) and
the grade history, and the worker's batch selections. Never commits.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import date, datetime
from typing import Any, Optional, Protocol

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.contact_activity import ContactActivity
from app.models.contact_scoring import ContactScoringModel
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.repositories.base import TenantId

__all__ = ["ScoringRepository", "Session", "SqlAlchemyScoringRepository", "scoring_repo"]

PROSPECT_COLUMNS = (
    Prospect.prospect_id, Prospect.tenant_id, Prospect.email, Prospect.email_type, Prospect.designation,
    Prospect.industry, Prospect.emp_band, Prospect.poc_country, Prospect.lifecycle_stage, Prospect.lead_status,
    Prospect.lead_source, Prospect.tags, Prospect.phone, Prospect.mobile_phone, Prospect.account_id,
    Prospect.score, Prospect.fit_score, Prospect.engagement_score, Prospect.score_grade,
)
EVENT_SIGNALS = {EmailEvent.EVENT_OPEN: "email_opened", EmailEvent.EVENT_CLICK: "email_clicked",
                 EmailEvent.EVENT_POSITIVE_REPLY: "positive_reply"}
ACTIVITY_SIGNALS = {"MEETING": "meeting", "CALL": "call"}


class ScoringRepository(Protocol):
    def model_row(self, tenant_id: TenantId) -> Optional[ContactScoringModel]: ...
    def seed_model_row(self, row: ContactScoringModel) -> bool: ...
    def contact_stats(self, tenant_id: TenantId) -> tuple[int, int, dict[str, int]]: ...
    def count_live(self, tenant_id: TenantId) -> int: ...
    def contact_rows(self, tenant_id: TenantId, ids: Collection[str]) -> list[Any]: ...
    def account_rows(self, ids: Collection[str]) -> dict[str, Any]: ...
    def first_email_events(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, str, datetime]]: ...
    def inbound_replies(self, ids: Collection[str], since: datetime, chars: int,
                        cap: int) -> Iterator[tuple[str, Optional[datetime], Optional[str], Optional[str]]]: ...
    def logged_activities(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, str, date, int]]: ...
    def completed_tasks(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, date, int]]: ...
    def latest_grades(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Optional[str]]: ...
    def store_score(self, prospect_id: str, result: dict[str, Any], now: datetime) -> None: ...
    def add(self, entity: Any) -> Any: ...
    def live_ids_after(self, tenant_id: TenantId, last_id: str, limit: int) -> list[str]: ...
    def recompute_requested(self, tenant_id: Optional[str], limit: int) -> list[str]: ...
    def stale_contacts(self, cutoff: datetime, tenant_id: Optional[str], cap: int) -> list[tuple[str, str]]: ...


class SqlAlchemyScoringRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def model_row(self, tenant_id: TenantId) -> Optional[ContactScoringModel]:
        return self._session.get(ContactScoringModel, tenant_id)

    def seed_model_row(self, row: ContactScoringModel) -> bool:
        """Insert the default model and commit it (it is shared reference data); False when another
        request seeded it first (the session is rolled back)."""
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            self._session.rollback()
            return False
        return True

    def _live(self, tenant_id: TenantId) -> Any:
        return self._session.query(func.count(Prospect.prospect_id)).filter(
            Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None))

    def count_live(self, tenant_id: TenantId) -> int:
        return int(self._live(tenant_id).scalar() or 0)

    def contact_stats(self, tenant_id: TenantId) -> tuple[int, int, dict[str, int]]:
        total = self.count_live(tenant_id)
        scored = int(self._live(tenant_id).filter(Prospect.score.isnot(None)).scalar() or 0)
        grades = dict(self._session.query(Prospect.score_grade, func.count(Prospect.prospect_id)).filter(
            Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None), Prospect.score_grade.isnot(None)
        ).group_by(Prospect.score_grade).all())
        return total, scored, grades

    def contact_rows(self, tenant_id: TenantId, ids: Collection[str]) -> list[Any]:
        if not ids:
            return []
        return self._session.query(*PROSPECT_COLUMNS).filter(
            Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(list(ids)), Prospect.deleted_at.is_(None)).all()

    def account_rows(self, ids: Collection[str]) -> dict[str, Any]:
        if not ids:
            return {}
        return {a.account_id: a for a in self._session.query(
            Account.account_id, Account.emp_band, Account.annual_revenue, Account.industry, Account.country
        ).filter(Account.account_id.in_(list(ids)))}

    def first_email_events(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, str, datetime]]:
        """(contact, signal, first time) for each e-mail's first open / click / positive reply."""
        rows = self._session.query(EmailMessage.prospect_id, EmailEvent.event_type, func.min(EmailEvent.event_time)).join(
            EmailMessage, EmailMessage.message_id == EmailEvent.message_id).filter(
            EmailMessage.prospect_id.in_(list(ids)), EmailEvent.event_type.in_(list(EVENT_SIGNALS)),
            EmailEvent.event_time >= since).group_by(EmailMessage.prospect_id, EmailEvent.message_id, EmailEvent.event_type)
        for pid, etype, at in rows:
            if at is not None:
                yield pid, EVENT_SIGNALS[etype], at

    def inbound_replies(self, ids: Collection[str], since: datetime, chars: int,
                        cap: int) -> Iterator[tuple[str, Optional[datetime], Optional[str], Optional[str]]]:
        rows = self._session.query(
            EmailMessage.prospect_id, EmailMessage.sent_at, func.substr(EmailMessage.subject, 1, chars),
            func.substr(EmailMessage.body_text, 1, chars)).filter(
            EmailMessage.prospect_id.in_(list(ids)), EmailMessage.direction == "INBOUND",
            EmailMessage.sent_at >= since).limit(cap)
        for pid, sent_at, subject, body in rows:
            yield pid, sent_at, subject, body

    def logged_activities(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, str, date, int]]:
        """(contact, signal, day, count) for meetings and calls logged against the contacts."""
        rows = self._session.query(ContactActivity.prospect_id, ContactActivity.activity_type,
                                   func.date(ContactActivity.occurred_at), func.count()).filter(
            ContactActivity.prospect_id.in_(list(ids)), ContactActivity.activity_type.in_(list(ACTIVITY_SIGNALS)),
            ContactActivity.occurred_at >= since).group_by(
            ContactActivity.prospect_id, ContactActivity.activity_type, func.date(ContactActivity.occurred_at))
        for pid, kind, day, n in rows:
            yield pid, ACTIVITY_SIGNALS[kind], day, n

    def completed_tasks(self, ids: Collection[str], since: datetime) -> Iterator[tuple[str, date, int]]:
        rows = self._session.query(CrmTask.prospect_id, func.date(CrmTask.completed_at), func.count()).filter(
            CrmTask.prospect_id.in_(list(ids)), CrmTask.status == "DONE", CrmTask.completed_at >= since).group_by(
            CrmTask.prospect_id, func.date(CrmTask.completed_at))
        for pid, day, n in rows:
            yield pid, day, n

    def latest_grades(self, tenant_id: TenantId, ids: Collection[str]) -> dict[str, Optional[str]]:
        """The new_value of each contact's most recent score_grade history row."""
        latest: dict[str, Optional[str]] = {}
        if not ids:
            return latest
        rows = self._session.query(PropertyChange.object_id, PropertyChange.new_value).filter(
            PropertyChange.tenant_id == tenant_id, PropertyChange.object_type == "CONTACT",
            PropertyChange.field == "score_grade", PropertyChange.object_id.in_(list(ids))
        ).order_by(PropertyChange.changed_at, PropertyChange.change_id)
        for pid, value in rows:
            latest[pid] = value  # ordered oldest first, so the last one wins
        return latest

    def store_score(self, prospect_id: str, result: dict[str, Any], now: datetime) -> None:
        # Core UPDATE that sets updated_at to itself: a score is not an edit of the contact
        table = Prospect.__table__
        self._session.execute(table.update().where(table.c.prospect_id == prospect_id).values(
            score=result["score"], fit_score=result["fit"], engagement_score=result["engagement"],
            score_grade=result["grade"], scored_at=now, updated_at=table.c.updated_at))

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def live_ids_after(self, tenant_id: TenantId, last_id: str, limit: int) -> list[str]:
        return [r[0] for r in self._session.query(Prospect.prospect_id).filter(
            Prospect.tenant_id == tenant_id, Prospect.deleted_at.is_(None), Prospect.prospect_id > last_id
        ).order_by(Prospect.prospect_id).limit(limit)]

    def recompute_requested(self, tenant_id: Optional[str], limit: int) -> list[str]:
        query = self._session.query(ContactScoringModel.tenant_id).filter(
            ContactScoringModel.recompute_requested_at.isnot(None))
        if tenant_id:
            query = query.filter(ContactScoringModel.tenant_id == tenant_id)
        return [r[0] for r in query.limit(limit)]

    def stale_contacts(self, cutoff: datetime, tenant_id: Optional[str], cap: int) -> list[tuple[str, str]]:
        query = self._session.query(Prospect.tenant_id, Prospect.prospect_id).filter(
            Prospect.deleted_at.is_(None),
            or_(Prospect.scored_at.is_(None), and_(Prospect.scored_at.isnot(None), Prospect.scored_at < cutoff)))
        if tenant_id:
            query = query.filter(Prospect.tenant_id == tenant_id)
        return [(t, p) for t, p in query.limit(cap).all()]


def scoring_repo(session: Session) -> SqlAlchemyScoringRepository:
    return SqlAlchemyScoringRepository(session)
