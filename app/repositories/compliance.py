"""Compliance check results repository (ADR 0009): stored template compliance checks. Never commits."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.compliance_check import ComplianceCheck

__all__ = ["Session", "SqlAlchemyComplianceRepository"]


class SqlAlchemyComplianceRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, check: ComplianceCheck) -> ComplianceCheck:
        self._session.add(check)
        return check

    def refresh(self, check: ComplianceCheck) -> None:
        self._session.refresh(check)
