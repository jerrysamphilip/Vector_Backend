# app/routers/sales_admin_router.py
"""
Phase 2 "Should" features (BRD v2.0 5.12):
  /sales/settings        lead assignment, reasons, stale days, amount visibility (BR-SF-02, 03, 05, 12)
  /workflow/rules        field-change rules: task / alert / set field (BR-SF-13)
  /notifications         in-app notifications and email preference (BR-SF-07)
  /sales/targets         revenue targets per rep per quarter (BR-SF-08)
  /template-library      email template library with merge fields (BR-SF-10)

Thin routes (ADR 0009): each calls one SalesAdminService method (app/services/sales_admin.py).
"""
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.sales import search_term
from app.schemas.sales_schema import PrefRequest, PreviewRequest, ReadRequest, RuleWrite, TargetWrite, TemplateWrite
from app.services.sales import entity_id
from app.services.sales_admin import MERGE_FIELDS, SalesAdminService

router = APIRouter(tags=["Sales settings"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
admin_user = require_role("SUPER_ADMIN", "ADMIN")

__all__ = ["MERGE_FIELDS", "router"]


def admin_service(db=Depends(get_db)) -> SalesAdminService:
    return SalesAdminService.for_session(db)


# ── Settings ─────────────────────────────────────────────────

@router.get("/sales/settings")
def read_settings(service: SalesAdminService = Depends(admin_service), current_user: Any = Depends(tenant_user)):
    return service.read_settings(current_user)


@router.put("/sales/settings")
def write_settings(payload: Dict[str, Any], service: SalesAdminService = Depends(admin_service),
                   current_user: Any = Depends(admin_user)):
    return service.write_settings(current_user, payload)


# ── Workflow rules (BR-SF-13) ────────────────────────────────

@router.get("/workflow/meta")
def workflow_meta(current_user: Any = Depends(tenant_user)):
    return SalesAdminService.workflow_meta()


@router.get("/workflow/rules")
def list_rules(service: SalesAdminService = Depends(admin_service), current_user: Any = Depends(admin_user)):
    return service.list_rules(current_user)


@router.post("/workflow/rules", status_code=201)
def create_rule(payload: RuleWrite, service: SalesAdminService = Depends(admin_service),
                current_user: Any = Depends(admin_user)):
    return service.create_rule(current_user, payload.model_dump(exclude_unset=True))


@router.patch("/workflow/rules/{rule_id}")
def update_rule(rule_id: str, payload: RuleWrite, service: SalesAdminService = Depends(admin_service),
                current_user: Any = Depends(admin_user)):
    return service.update_rule(current_user, entity_id(rule_id, "Rule not found"), payload.model_dump(exclude_unset=True))


@router.delete("/workflow/rules/{rule_id}")
def delete_rule(rule_id: str, service: SalesAdminService = Depends(admin_service),
                current_user: Any = Depends(admin_user)):
    return service.delete_rule(current_user, entity_id(rule_id, "Rule not found"))


# ── Notifications (BR-SF-07) ─────────────────────────────────

@router.get("/notifications")
def list_notifications(unread_only: bool = False, limit: int = Query(30, ge=1, le=100),
                       service: SalesAdminService = Depends(admin_service), current_user: Any = Depends(tenant_user)):
    return service.notifications(current_user, unread_only, limit)


@router.post("/notifications/read")
def mark_read(payload: ReadRequest, service: SalesAdminService = Depends(admin_service),
              current_user: Any = Depends(tenant_user)):
    return service.mark_read(current_user, payload.ids)


@router.put("/notifications/preferences")
def notification_prefs(payload: PrefRequest, service: SalesAdminService = Depends(admin_service),
                       current_user: Any = Depends(tenant_user)):
    return service.set_email_preference(current_user, payload.email)


# ── Targets (BR-SF-08) ───────────────────────────────────────

@router.get("/sales/targets")
def list_targets(fy: Optional[int] = None, service: SalesAdminService = Depends(admin_service),
                 current_user: Any = Depends(tenant_user)):
    return service.targets(current_user, fy)


@router.put("/sales/targets")
def set_target(payload: TargetWrite, service: SalesAdminService = Depends(admin_service),
               current_user: Any = Depends(tenant_user)):
    return service.set_target(current_user, payload.user_id, payload.fy, payload.quarter, payload.amount)


# ── Template library (BR-SF-10) ──────────────────────────────

@router.get("/template-library")
def list_templates(q: Optional[str] = None, category: Optional[str] = None,
                   service: SalesAdminService = Depends(admin_service), current_user: Any = Depends(tenant_user)):
    return service.templates(current_user, search_term(q), category)


@router.post("/template-library", status_code=201)
def create_template(payload: TemplateWrite, service: SalesAdminService = Depends(admin_service),
                    current_user: Any = Depends(tenant_user)):
    return service.create_template(current_user, payload.model_dump(exclude_unset=True))


@router.patch("/template-library/{template_id}")
def update_template(template_id: str, payload: TemplateWrite, service: SalesAdminService = Depends(admin_service),
                    current_user: Any = Depends(tenant_user)):
    return service.update_template(current_user, entity_id(template_id, "Template not found"),
                                   payload.model_dump(exclude_unset=True))


@router.delete("/template-library/{template_id}")
def delete_template(template_id: str, service: SalesAdminService = Depends(admin_service),
                    current_user: Any = Depends(tenant_user)):
    return service.delete_template(current_user, entity_id(template_id, "Template not found"))


@router.post("/template-library/{template_id}/render")
def render_template(template_id: str, payload: PreviewRequest, service: SalesAdminService = Depends(admin_service),
                    current_user: Any = Depends(tenant_user)):
    """The template with merge fields filled in for one contact (or sample values)."""
    return service.render_template(current_user, entity_id(template_id, "Template not found"), payload.prospect_id,
                                   payload.count_use)
