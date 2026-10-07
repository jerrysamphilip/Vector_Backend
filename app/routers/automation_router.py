
# app/routers/automation_router.py
"""
API Router for managing Automation Rules.
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import require_role
from app.models.automation_rule import AutomationRule
from app.models.campaign import Campaign
from app.models.user import User
from app.schemas.automation_schema import AutomationRuleCreate, AutomationRuleResponse, AutomationRuleUpdate

router = APIRouter(prefix="/automation-rules", tags=["Automation Rules"])

@router.post("/", response_model=AutomationRuleResponse, status_code=status.HTTP_201_CREATED)
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

@router.put("/{rule_id}", response_model=AutomationRuleResponse)
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
    for field, value in update_data.items():
        setattr(rule, field, value)
    
    db.commit()
    db.refresh(rule)
    return rule

@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
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
