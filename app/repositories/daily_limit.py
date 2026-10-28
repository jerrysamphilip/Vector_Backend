"""
Daily new-contact limit repository (ADR 0009; BR-OV-01): first-step sends and held first emails per
attributed user (the contact's owner, else the campaign's creator). User ids are global, so counts
per user need no tenant. Never commits.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Optional

from sqlalchemy import ColumnElement, func
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.email_message import EmailMessage
from app.models.email_sequence import EmailSequence
from app.models.prospect import Prospect
from app.models.user import User

__all__ = ["Session", "SqlAlchemyDailyLimitRepository", "daily_limit_repo"]


def _attributed_user() -> ColumnElement[str]:
    return func.coalesce(Prospect.owner_id, Campaign.created_by)


class SqlAlchemyDailyLimitRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def first_steps_sent(self, user_id: str, start: datetime, end: datetime) -> int:
        """Outbound first-step emails sent in [start, end) attributed to the user."""
        return self._session.query(func.count(EmailMessage.message_id)) \
            .join(EmailSequence, EmailSequence.sequence_id == EmailMessage.sequence_id) \
            .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id) \
            .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id) \
            .filter(EmailSequence.step_number == 1, EmailMessage.direction == "OUTBOUND",
                    EmailMessage.sent_at >= start, EmailMessage.sent_at < end,
                    _attributed_user() == user_id).scalar() or 0

    def held(self, user_id: str, code: str) -> int:
        """Queued first emails held back with ``code``, attributed to the user."""
        return self._session.query(func.count(EmailMessage.message_id)) \
            .join(Prospect, Prospect.prospect_id == EmailMessage.prospect_id) \
            .join(Campaign, Campaign.campaign_id == EmailMessage.campaign_id) \
            .filter(EmailMessage.status.in_(("QUEUED", "SCHEDULED")), EmailMessage.last_error_code == code,
                    _attributed_user() == user_id).scalar() or 0

    def step_number(self, sequence_id: str) -> Optional[int]:
        number: Optional[int] = self._session.query(EmailSequence.step_number).filter(
            EmailSequence.sequence_id == sequence_id).scalar()
        return number

    def campaign_creator(self, campaign_id: str) -> Optional[str]:
        creator: Optional[str] = self._session.query(Campaign.created_by).filter(
            Campaign.campaign_id == campaign_id).scalar()
        return creator

    def owners(self, prospect_ids: Sequence[str]) -> list[Optional[str]]:
        """The owner of each contact (one entry per contact found)."""
        if not prospect_ids:
            return []
        return [owner for (owner,) in self._session.query(Prospect.owner_id).filter(
            Prospect.prospect_id.in_(prospect_ids)).all()]

    def user_name_parts(self, user_id: str) -> tuple[Optional[str], ...]:
        row = self._session.query(User.first_name, User.last_name).filter(User.user_id == user_id).first()
        return tuple(row) if row else ()


def daily_limit_repo(session: Session) -> SqlAlchemyDailyLimitRepository:
    return SqlAlchemyDailyLimitRepository(session)
