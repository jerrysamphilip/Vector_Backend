# app/routers/automation_router.py
"""
API Router for managing Automation Rules.

Layered (ADR 0009, chunk A7): each handler makes one call to AutomationRulesService
(app/services/automation_rule_service.py), injected through ``rules_service``.
"""

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, status

from app.core.auth import require_permission, require_role
from app.core.database import get_db
from app.schemas.automation_schema import AutomationRuleCreate, AutomationRuleResponse, AutomationRuleUpdate
from app.services.automation_rule_service import AutomationRulesService

router = APIRouter(prefix="/automation-rules", tags=["Automation Rules"])

# Changing a campaign's automation needs the same permission as changing the campaign (F038)
MANAGE_CAMPAIGNS = [Depends(require_permission("manage_campaigns"))]


def rules_service(db=Depends(get_db)) -> AutomationRulesService:
    return AutomationRulesService.for_session(db)


Rules = Depends(rules_service)


@router.post("/", response_model=AutomationRuleResponse, status_code=status.HTTP_201_CREATED,
             dependencies=MANAGE_CAMPAIGNS)
def create_rule(
    rule_in: AutomationRuleCreate,
    service: AutomationRulesService = Rules,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Any:
    """Create a new automation rule."""
    return service.create(current_user, rule_in)


@router.get("/", response_model=List[AutomationRuleResponse])
def list_rules(
    campaign_id: Optional[str] = None,
    service: AutomationRulesService = Rules,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
) -> Any:
    """List rules. specific campaign filter optional."""
    return service.list(current_user, campaign_id)


@router.put("/{rule_id}", response_model=AutomationRuleResponse, dependencies=MANAGE_CAMPAIGNS)
def update_rule(
    rule_id: str,
    rule_in: AutomationRuleUpdate,
    service: AutomationRulesService = Rules,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
) -> Any:
    """Update an automation rule."""
    return service.update(current_user, rule_id, rule_in)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=MANAGE_CAMPAIGNS)
def delete_rule(
    rule_id: str,
    service: AutomationRulesService = Rules,
    current_user: Any = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
) -> None:
    """Delete an automation rule."""
    service.delete(current_user, rule_id)
