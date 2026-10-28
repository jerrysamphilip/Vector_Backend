"""
Mailbox-provider feedback repository (ADR 0009): ingestion runs and the reputation / feedback rows
read from Postmaster Tools, SNDS and JMRP. Runs are per tenant (None = platform-wide). Never commits.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.provider_reputation import ExternalIngestionRun

__all__ = ["Session", "SqlAlchemyProviderIngestionRepository"]


class SqlAlchemyProviderIngestionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def latest_run(self, provider: str, tenant_id: Optional[str]) -> Optional[ExternalIngestionRun]:
        return (
            self._session.query(ExternalIngestionRun)
            .filter(ExternalIngestionRun.provider == provider, ExternalIngestionRun.tenant_id == tenant_id)
            .order_by(ExternalIngestionRun.started_at.desc())
            .first()
        )
