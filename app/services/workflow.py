"""
Workflow rules (BRD v2.0 BR-SF-13): when a field changes on a lead, deal or
contact, create a task, send an alert, or set a field.

The engine runs from crm.record_changes(), which every edit already goes
through to write property history, so rules see UI, bulk, import and system
changes alike. Field updates made by a rule can trigger further rules, up to
MAX_DEPTH levels, so two rules can never loop forever.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MAX_DEPTH = 3

# Fields a rule can watch, and fields it may set, per object
WATCH_FIELDS = {
    "LEAD": {"created": "Lead created", "stage": "Stage", "owner_id": "Owner", "source": "Source",
             "next_step": "Next step"},
    "DEAL": {"created": "Opportunity created", "stage_id": "Stage", "amount": "Amount", "close_date": "Close date",
             "client_type": "Client type", "owner_id": "Owner", "forecast_category": "Forecast category"},
    "CONTACT": {"created": "Contact created", "lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status",
                "consent_status": "Email subscription", "owner_id": "Owner"},
}
SETTABLE_FIELDS = {
    "LEAD": {"source": "Source", "next_step": "Next step"},
    "DEAL": {"next_step": "Next step", "client_type": "Client type", "forecast_category": "Forecast category"},
    "CONTACT": {"lead_status": "Lead status", "lifecycle_stage": "Lifecycle stage", "lead_source": "Lead source"},
}
OPERATORS = {"changes": "changes", "equals": "changes to", "greater_than": "becomes greater than"}
ACTION_TYPES = ("create_task", "notify", "update_field")


def _load(db: Session, object_type: str, object_id: str):
    from app.models.prospect import Prospect
    from app.models.sales import Lead, Opportunity
    model = {"LEAD": Lead, "DEAL": Opportunity, "CONTACT": Prospect}[object_type]
    key = {"LEAD": Lead.lead_id, "DEAL": Opportunity.opportunity_id, "CONTACT": Prospect.prospect_id}[object_type]
    return db.query(model).filter(key == object_id).first()


def _describe(db: Session, object_type: str, obj) -> Tuple[str, str, Optional[str], Optional[str], Optional[str]]:
    """(name, link, prospect_id, opportunity_id, account_id)"""
    from app.models.prospect import Prospect
    if object_type == "DEAL":
        return obj.name, f"/app/deals/{obj.opportunity_id}", obj.prospect_id, obj.opportunity_id, obj.account_id
    if object_type == "LEAD":
        p = db.query(Prospect).filter(Prospect.prospect_id == obj.prospect_id).first()
        name = (p.full_name or p.email) if p else "Lead"
        return name, f"/app/leads/{obj.lead_id}", obj.prospect_id, None, obj.account_id
    return obj.full_name or obj.email, f"/app/contacts/{obj.prospect_id}", obj.prospect_id, None, obj.account_id


def _matches(rule, old, new) -> bool:
    from app.services.crm import _fmt
    if rule.operator == "changes":
        return _fmt(old) != _fmt(new)
    if rule.operator == "equals":
        return (_fmt(new) or "").strip().lower() == (rule.value or "").strip().lower() and \
            (_fmt(old) or "").strip().lower() != (rule.value or "").strip().lower()
    if rule.operator == "greater_than":
        try:
            threshold = float(rule.value)
            return float(new) > threshold and (old in (None, "") or float(old) <= threshold)
        except (TypeError, ValueError):
            return False
    return False


def _recipient(db: Session, who: str, owner_id: Optional[str], actor_id: Optional[str]) -> Optional[str]:
    from app.services.notifications import manager_of
    if who == "owner":
        return owner_id
    if who == "manager":
        return manager_of(db, owner_id)
    if who == "actor":
        return actor_id
    return who or None  # a user id


def _render(text: str, name: str, rule_name: str) -> str:
    return (text or "").replace("{name}", name or "").replace("{rule}", rule_name)


def on_changes(db: Session, tenant_id: str, object_type: str, object_id: str,
               changes: Dict[str, Tuple[object, object]], actor_id: Optional[str]) -> int:
    """Run the workspace's active rules for these field changes. Caller commits."""
    if object_type not in WATCH_FIELDS or not changes:
        return 0
    depth = db.info.get("workflow_depth", 0)
    if depth >= MAX_DEPTH:
        return 0
    from app.models.sales_extra import WorkflowRule
    cache = db.info.setdefault("workflow_rules", {})
    key = (tenant_id, object_type)
    if key not in cache:
        cache[key] = db.query(WorkflowRule).filter(WorkflowRule.tenant_id == tenant_id,
                                                   WorkflowRule.object_type == object_type,
                                                   WorkflowRule.active.is_(True)).all()
    rules = [r for r in cache[key] if r.field in changes and _matches(r, *changes[r.field])]
    if not rules:
        return 0
    obj = _load(db, object_type, object_id)
    if obj is None:
        return 0
    name, link, prospect_id, opportunity_id, account_id = _describe(db, object_type, obj)
    owner_id = getattr(obj, "owner_id", None)
    db.info["workflow_depth"] = depth + 1
    try:
        for rule in rules:
            try:
                _run(db, tenant_id, rule, object_type, obj, name, link, owner_id, actor_id,
                     prospect_id, opportunity_id, account_id)
                rule.run_count = (rule.run_count or 0) + 1
                rule.last_run_at = datetime.utcnow()
            except Exception as exc:  # a broken rule must not break the user's edit
                logger.warning(f"[Workflow] Rule {rule.rule_id} failed: {exc}")
    finally:
        db.info["workflow_depth"] = depth
    return len(rules)


def _run(db, tenant_id, rule, object_type, obj, name, link, owner_id, actor_id, prospect_id, opportunity_id, account_id):
    from app.models.crm import CrmTask
    from app.services import crm
    from app.services.notifications import notify
    for action in rule.actions or []:
        kind = action.get("type")
        if kind == "create_task":
            assignee = _recipient(db, action.get("assign_to") or "owner", owner_id, actor_id)
            days = int(action.get("due_in_days") or 0)
            db.add(CrmTask(tenant_id=tenant_id, title=_render(action.get("title") or rule.name, name, rule.name)[:255],
                           notes=f"Created by workflow rule \"{rule.name}\"", task_type=action.get("task_type") or "TODO",
                           priority=action.get("priority") or "MEDIUM", owner_id=assignee, created_by=actor_id,
                           due_at=datetime.utcnow() + timedelta(days=days) if days else None,
                           prospect_id=prospect_id, opportunity_id=opportunity_id, account_id=account_id))
            if assignee and assignee != actor_id:
                notify(db, tenant_id, [assignee], "TASK", f"New task: {_render(action.get('title') or rule.name, name, rule.name)}",
                       f"From rule \"{rule.name}\" on {name}.", link)
        elif kind == "notify":
            targets = action.get("to") or ["owner"]
            targets = targets if isinstance(targets, list) else [targets]
            users = [_recipient(db, t, owner_id, actor_id) for t in targets]
            message = _render(action.get("message") or f"{rule.name}: {name}", name, rule.name)
            notify(db, tenant_id, users, "WORKFLOW", message, f"Rule \"{rule.name}\" ran on {name}.", link)
        elif kind == "update_field":
            field = action.get("field")
            if field not in SETTABLE_FIELDS.get(object_type, {}):
                continue
            before = {field: getattr(obj, field, None)}
            value = action.get("value")
            setattr(obj, field, value)
            if object_type == "DEAL" and field == "forecast_category":
                obj.forecast_category_manual = True
            crm.record_changes(db, tenant_id, object_type, getattr(obj, {"LEAD": "lead_id", "DEAL": "opportunity_id",
                                                                       "CONTACT": "prospect_id"}[object_type]),
                               before, {field: value}, actor_id, "WORKFLOW")
