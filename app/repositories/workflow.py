"""
Workflow rules repository (ADR 0009; BR-SF-13): the workspace's active rules and the records they act
on. Every lookup takes the tenant. Never commits (rules run inside the edit's transaction).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.prospect import Prospect
from app.models.sales import Lead, Opportunity
from app.models.sales_extra import WorkflowRule
from app.repositories.base import TenantId

__all__ = ["Session", "SqlAlchemyWorkflowRepository", "workflow_repo"]

_MODELS: dict[str, Any] = {"LEAD": Lead, "DEAL": Opportunity, "CONTACT": Prospect}
_KEYS: dict[str, Any] = {"LEAD": Lead.lead_id, "DEAL": Opportunity.opportunity_id, "CONTACT": Prospect.prospect_id}


class SqlAlchemyWorkflowRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def active_rules(self, tenant_id: TenantId, object_type: str) -> list[WorkflowRule]:
        return self._session.query(WorkflowRule).filter(WorkflowRule.tenant_id == tenant_id,
                                                        WorkflowRule.object_type == object_type,
                                                        WorkflowRule.active.is_(True)).all()

    def record(self, tenant_id: TenantId, object_type: str, object_id: str) -> Any:
        """The lead / deal / contact a rule runs on (None when missing or another tenant's)."""
        model = _MODELS[object_type]
        return self._session.query(model).filter(_KEYS[object_type] == object_id,
                                                 model.tenant_id == tenant_id).first()

    def prospect(self, tenant_id: TenantId, prospect_id: Optional[str]) -> Optional[Prospect]:
        return self._session.query(Prospect).filter(Prospect.prospect_id == prospect_id,
                                                    Prospect.tenant_id == tenant_id).first()

    def add(self, entity: Any) -> Any:
        self._session.add(entity)
        return entity


def workflow_repo(session: Session) -> SqlAlchemyWorkflowRepository:
    return SqlAlchemyWorkflowRepository(session)
