"""
Automation rules repository and unit of work (ADR 0009, chunk A7).

Rules are scoped to a workspace through their campaign (``automation_rules`` has no tenant column).
The engine's reads and writes (rules for an event, the run ledger, the queued e-mail) are here too.
Never commits; the service owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.automation_rule import AutomationRule
from app.models.automation_rule_run import AutomationRuleRun
from app.models.campaign import Campaign, CampaignProspect
from app.models.email_message import EmailMessage
from app.models.email_template import EmailTemplate
from app.models.prospect import Prospect
from app.repositories.base import SqlAlchemyUnitOfWork, TenantId

__all__ = [
    "AutomationAccess", "AutomationRepository", "AutomationUnitOfWork", "Session", "SqlAlchemyAutomationRepository",
    "SqlAlchemyAutomationUnitOfWork", "automation_uow", "borrow",
]


class AutomationRepository(Protocol):
    def campaign(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]: ...
    def rules(self, tenant_id: TenantId, campaign_id: Optional[str]) -> list[AutomationRule]: ...
    def rule(self, tenant_id: TenantId, rule_id: str) -> Optional[AutomationRule]: ...
    def template_in_tenant(self, template_id: str, tenant_id: Optional[str]) -> bool: ...
    def add(self, entity: Any) -> Any: ...
    def delete(self, entity: Any) -> None: ...
    def flush(self) -> None: ...
    def refresh(self, entity: Any) -> None: ...
    # engine
    def active_rules(self, campaign_id: str, trigger: str) -> list[AutomationRule]: ...
    def campaign_tenant(self, campaign_id: str) -> Optional[str]: ...
    def prospect(self, tenant_id: Optional[str], prospect_id: str) -> Optional[Prospect]: ...
    def rule_mail_exists(self, campaign_id: str, prospect_id: str, template_id: str,
                         ignore_statuses: Sequence[str]) -> bool: ...
    def claim_run(self, rule_id: str, prospect_id: str, tenant_id: Optional[str]) -> bool: ...
    def enrollment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]: ...


class SqlAlchemyAutomationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def campaign(self, tenant_id: TenantId, campaign_id: str) -> Optional[Campaign]:
        return self._session.query(Campaign).filter(Campaign.campaign_id == campaign_id,
                                                    Campaign.tenant_id == tenant_id).first()

    def rules(self, tenant_id: TenantId, campaign_id: Optional[str]) -> list[AutomationRule]:
        query = self._session.query(AutomationRule).join(Campaign).filter(Campaign.tenant_id == tenant_id)
        if campaign_id:
            query = query.filter(AutomationRule.campaign_id == campaign_id)
        return query.all()

    def rule(self, tenant_id: TenantId, rule_id: str) -> Optional[AutomationRule]:
        return self._session.query(AutomationRule).join(Campaign).filter(
            AutomationRule.rule_id == rule_id, Campaign.tenant_id == tenant_id).first()

    def template_in_tenant(self, template_id: str, tenant_id: Optional[str]) -> bool:
        """True when the template belongs to a campaign of this workspace (F058)."""
        if not template_id or not tenant_id:
            return False
        return self._session.query(EmailTemplate.template_id).join(
            Campaign, Campaign.campaign_id == EmailTemplate.campaign_id
        ).filter(EmailTemplate.template_id == template_id, Campaign.tenant_id == tenant_id).first() is not None

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity

    def delete(self, entity: Any) -> None:
        self._session.delete(entity)

    def flush(self) -> None:
        self._session.flush()

    def refresh(self, entity: Any) -> None:
        self._session.refresh(entity)

    # engine ------------------------------------------------------------------
    def active_rules(self, campaign_id: str, trigger: str) -> list[AutomationRule]:
        return self._session.query(AutomationRule).filter(
            AutomationRule.campaign_id == campaign_id,
            AutomationRule.trigger_type == trigger,
            AutomationRule.is_active.is_(True),
        ).all()

    def campaign_tenant(self, campaign_id: str) -> Optional[str]:
        return self._session.query(Campaign.tenant_id).filter(Campaign.campaign_id == campaign_id).scalar()

    def prospect(self, tenant_id: Optional[str], prospect_id: str) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def rule_mail_exists(self, campaign_id: str, prospect_id: str, template_id: str,
                         ignore_statuses: Sequence[str]) -> bool:
        return self._session.query(EmailMessage.message_id).filter(
            EmailMessage.campaign_id == campaign_id,
            EmailMessage.prospect_id == prospect_id,
            EmailMessage.template_id == template_id,
            EmailMessage.sequence_id.is_(None),
            EmailMessage.status.notin_(list(ignore_statuses)),
        ).first() is not None

    def claim_run(self, rule_id: str, prospect_id: str, tenant_id: Optional[str]) -> bool:
        """Record that the rule acted on the contact; False when it already had (19b). The unique key
        decides, also between two events processed at the same time. The insert runs in a savepoint,
        so a duplicate only undoes the run row, not the caller's transaction."""
        try:
            with self._session.begin_nested():
                self._session.add(AutomationRuleRun(rule_id=rule_id, prospect_id=prospect_id, tenant_id=tenant_id))
        except IntegrityError:
            return False
        return True

    def enrollment(self, campaign_id: str, prospect_id: str) -> Optional[CampaignProspect]:
        return self._session.query(CampaignProspect).filter(CampaignProspect.campaign_id == campaign_id,
                                                            CampaignProspect.prospect_id == prospect_id).first()


class AutomationAccess(Protocol):
    @property
    def rules_repo(self) -> AutomationRepository: ...

    @property
    def session(self) -> Session: ...

    def savepoint(self) -> AbstractContextManager[Any]: ...


@dataclass(frozen=True)
class _Borrowed:
    session: Session

    @property
    def rules_repo(self) -> SqlAlchemyAutomationRepository:
        return SqlAlchemyAutomationRepository(self.session)

    def savepoint(self) -> AbstractContextManager[Any]:
        """A nested transaction: an exception inside it undoes only its own changes."""
        return self.session.begin_nested()


def borrow(session: Session) -> AutomationAccess:
    """Repository access on a caller's session; the caller owns the transaction."""
    return _Borrowed(session)


class AutomationUnitOfWork(Protocol):
    @property
    def rules_repo(self) -> AutomationRepository: ...

    @property
    def session(self) -> Session: ...

    def __enter__(self) -> AutomationUnitOfWork: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class SqlAlchemyAutomationUnitOfWork(SqlAlchemyUnitOfWork):
    @classmethod
    def for_session(cls, session: Session) -> SqlAlchemyAutomationUnitOfWork:
        uow = cls(lambda: session)
        uow._owns_session = False
        return uow

    def __enter__(self) -> SqlAlchemyAutomationUnitOfWork:
        super().__enter__()
        return self

    @property
    def rules_repo(self) -> SqlAlchemyAutomationRepository:
        return SqlAlchemyAutomationRepository(self.session)


def automation_uow(session: Session) -> SqlAlchemyAutomationUnitOfWork:
    return SqlAlchemyAutomationUnitOfWork.for_session(session)
