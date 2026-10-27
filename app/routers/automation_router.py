
# app/routers/automation_router.py
"""
API Router for managing Automation Rules.
"""

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_permission, require_role
from app.models.automation_rule import ActionType, AutomationRule
from app.models.campaign import Campaign
from app.services.automation_rule_service import template_in_tenant
from app.models.user import User
from app.schemas.automation_schema import AutomationRuleCreate, AutomationRuleResponse, AutomationRuleUpdate

router = APIRouter(prefix="/automation-rules", tags=["Automation Rules"])

# Changing a campaign's automation needs the same permission as changing the campaign (F038)
MANAGE_CAMPAIGNS = [Depends(require_permission("manage_campaigns"))]


def _check_action_template(db: Session, action_type: str, action_config: Optional[Dict[str, Any]],
                           tenant_id: str) -> None:
    """
    The template a rule sends must be one of the caller's workspace (F058): another workspace's
    template is 404, exactly like one that does not exist. SEND_EMAIL without a template is 400.
    """
    template_id = (action_config or {}).get("template_id")
    if not template_id:
        if action_type == ActionType.SEND_EMAIL.value:
            raise HTTPException(status_code=400, detail="A SEND_EMAIL rule needs action_config.template_id")
        return
    if not isinstance(template_id, str) or not template_in_tenant(db, template_id, tenant_id):
        raise HTTPException(status_code=404, detail="Template not found")


@router.post("/", response_model=AutomationRuleResponse, status_code=status.HTTP_201_CREATED,
             dependencies=MANAGE_CAMPAIGNS)
def create_rule(
    rule_in: AutomationRuleCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Create a new automation rule."""
    campaign = db.query(Campaign).filter(
        Campaign.campaign_id == rule_in.campaign_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    _check_action_template(db, rule_in.action_type.value, rule_in.action_config, current_user.tenant_id)

    rule = AutomationRule(**rule_in.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule

@router.get("/", response_model=List[AutomationRuleResponse])
def list_rules(
    campaign_id: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT", "AGENT")),
):
    """List rules. specific campaign filter optional."""
    query = db.query(AutomationRule).join(Campaign).filter(
        Campaign.tenant_id == current_user.tenant_id
    )
    if campaign_id:
        query = query.filter(AutomationRule.campaign_id == campaign_id)
    
    return query.all()

@router.put("/{rule_id}", response_model=AutomationRuleResponse, dependencies=MANAGE_CAMPAIGNS)
def update_rule(
    rule_id: str,
    rule_in: AutomationRuleUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")),
):
    """Update an automation rule."""
    rule = db.query(AutomationRule).join(Campaign).filter(
        AutomationRule.rule_id == rule_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    update_data = rule_in.model_dump(exclude_unset=True)
    if "action_config" in update_data:
        _check_action_template(db, rule.action_type, update_data["action_config"], current_user.tenant_id)
    for field, value in update_data.items():
        setattr(rule, field, value)
    
    db.commit()
    db.refresh(rule)
    return rule

@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=MANAGE_CAMPAIGNS)
def delete_rule(
    rule_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role("SUPER_ADMIN", "ADMIN", "MANAGER")),
):
    """Delete an automation rule."""
    rule = db.query(AutomationRule).join(Campaign).filter(
        AutomationRule.rule_id == rule_id,
        Campaign.tenant_id == current_user.tenant_id,
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    db.delete(rule)
    db.commit()
