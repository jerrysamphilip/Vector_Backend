"""
Sales administration (BRD v2.0 5.4 and 5.12): the sales hierarchy (BR-SH-01/02), sales settings
(BR-SF-02/03/05/12/17), workflow rules (BR-SF-13), in-app notifications (BR-SF-07), revenue targets
(BR-SF-08) and the email template library (BR-SF-10).

``SalesAdminService`` is what app/routers/sales_admin_router.py (and the hierarchy routes of
app/routers/sales_router.py) call: one method per route, one unit of work per call. SQL lives in
app/repositories/sales_admin.py.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID
from typing import Any, Dict, List, Optional

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain.sales import ADMIN_ROLES, SALES_ROLES, hierarchy_error
from app.models.sales_extra import MessageTemplate, SalesTarget, WorkflowRule
from app.services import workflow
from app.services.contact_service import SALES_LEVELS, clean_str
from app.services.notifications import as_dict as notification_dict
from app.services.quote_approval import parse_threshold
from app.services.sales import SalesRules, SalesServiceBase, as_float, fiscal_label, fiscal_year_of, money, settings
from app.services.sales_settings import DEFAULTS, amounts_allowed, load_settings, store_settings

MERGE_FIELDS = [
    ("{{first_name}}", "First name"), ("{{last_name}}", "Last name"), ("{{full_name}}", "Full name"),
    ("{{company_name}}", "Company"), ("{{designation}}", "Job title"), ("{{email}}", "Email"),
    ("{{industry}}", "Industry"), ("{{your_name}}", "Your name"), ("{{calendar_link}}", "Calendar link"),
]
SAMPLE_CONTACT = dict(first_name="Alex", last_name="Morgan", company_name="Acme Corp",
                      designation="Head of Operations", email="alex@acme.example", industry="Software",
                      linkedin_url="", poc_city="", poc_state="", poc_country="")
TEMPLATE_LIMIT = 500
QUARTERS = (1, 2, 3, 4)


# ── Pure rules ───────────────────────────────────────────────

def _full_name(u: Any) -> str:
    return f"{u.first_name} {u.last_name}".strip()


def settings_errors(changes: dict) -> Optional[str]:
    """Validation of a settings change that needs no database (None = fine)."""
    la = changes.get("lead_assignment")
    if la is not None and la.get("mode") not in (None, "off", "round_robin", "region"):
        return "mode must be off, round_robin or region"
    return None


def assignment_user_ids(changes: dict) -> set:
    la = changes.get("lead_assignment")
    if la is None:
        return set()
    return set(la.get("users") or []) | {r.get("user_id") for r in la.get("regions") or []}


def days_error(changes: dict) -> Optional[str]:
    for key in ("stale_deal_days", "default_recycle_days"):
        if key in changes and (not isinstance(changes[key], int) or changes[key] < 1):
            return f"{key} must be a positive number of days"
    return None


def rule_dict(r: Any) -> dict:
    return {"rule_id": r.rule_id, "name": r.name, "object_type": r.object_type, "field": r.field,
            "operator": r.operator, "value": r.value, "actions": r.actions, "active": r.active,
            "run_count": r.run_count, "last_run_at": r.last_run_at, "created_at": r.created_at}


def rule_condition_error(data: dict) -> Optional[str]:
    """What the rule watches: object, field, operator and value (BR-SF-13)."""
    obj = data.get("object_type")
    if obj not in workflow.WATCH_FIELDS:
        return "object_type must be LEAD, DEAL or CONTACT"
    if data.get("field") not in workflow.WATCH_FIELDS[obj]:
        return f"field must be one of {list(workflow.WATCH_FIELDS[obj])}"
    if data.get("operator") not in workflow.OPERATORS:
        return f"operator must be one of {list(workflow.OPERATORS)}"
    if data["operator"] != "changes" and not clean_str(data.get("value")):
        return "Give the value to compare with"
    if data["operator"] == "greater_than":
        try:
            float(data["value"])
        except (TypeError, ValueError):
            return "'greater than' needs a number"
    return None


def action_error(obj: str, action: dict) -> Optional[str]:
    if action.get("type") not in workflow.ACTION_TYPES:
        return f"Action type must be one of {list(workflow.ACTION_TYPES)}"
    if action["type"] == "create_task" and not clean_str(action.get("title")):
        return "A task action needs a title"
    if action["type"] == "update_field" and action.get("field") not in workflow.SETTABLE_FIELDS[obj]:
        return f"Can set only {list(workflow.SETTABLE_FIELDS[obj])} on this object"
    return None


def action_recipients(action: dict) -> List[Optional[str]]:
    """Named users an action addresses (owner / manager / actor are resolved when the rule runs)."""
    if action["type"] == "create_task":
        who = [action.get("assign_to")]
    elif action["type"] == "notify":
        who = action.get("to") or []
    else:
        who = []
    return [w for w in who if w and w not in ("owner", "manager", "actor")]


def template_dict(t: Any, user: Any, names: Optional[dict] = None) -> dict:
    return {"template_id": t.template_id, "name": t.name, "category": t.category, "subject": t.subject, "body": t.body,
            "shared": t.shared, "owner_id": t.owner_id, "owner_name": (names or {}).get(t.owner_id),
            "usage_count": t.usage_count, "updated_at": t.updated_at,
            "can_edit": t.owner_id == user.user_id or user.role in ADMIN_ROLES}


def _hierarchy_user(u: Any, reports: Dict[str, int]) -> dict:
    return {"user_id": u.user_id, "name": _full_name(u), "email": u.email, "role": u.role, "status": u.status,
            "sales_level": u.sales_level, "level_label": SALES_LEVELS.get(u.sales_level),
            "manager_id": u.manager_id, "direct_reports": reports.get(u.user_id, 0)}


class SalesAdminService(SalesServiceBase):

    # ── hierarchy (BR-SH-01/02) ─────────────────────────────
    def hierarchy(self, user: Any) -> dict:
        with self.work() as rules:
            users = rules.repos.lookups.hierarchy_users(user.tenant_id)
            reports: Dict[str, int] = {}
            for u in users:
                if u.manager_id:
                    reports[u.manager_id] = reports.get(u.manager_id, 0) + 1
            visible = rules.collab.visible_user_ids(user)
            if visible is not None and user.role not in ADMIN_ROLES:
                # Admins and workspace-wide viewers see everyone; others only themselves and their team (BR-SH-02)
                users = [u for u in users if u.user_id in visible]
            return {"levels": [{"level": k, "label": v} for k, v in SALES_LEVELS.items()],
                    "can_edit": user.role in ADMIN_ROLES,
                    "my_team": sorted(visible) if visible is not None else None,
                    "users": [_hierarchy_user(u, reports) for u in users]}

    def set_hierarchy(self, user: Any, user_id: UUID | str, level: Optional[int], manager_id: Optional[str]) -> dict:
        """Set a user's sales level and manager (admins). The manager must sit at a higher level."""
        user_id = str(user_id)
        with self.work() as rules:
            target = rules.repos.lookups.user(user.tenant_id, user_id)
            if not target:
                raise NotFoundError("User not found")
            if level is not None and level not in SALES_LEVELS:
                raise BadRequestError("Sales level must be 1, 2, 3 or 4")
            if level is None or level == 1:
                manager_id = None if level is None else manager_id
            self._check_manager(rules, user, target, level, manager_id)
            # Direct reports must stay below the user
            clash = rules.repos.lookups.reports_with_level(target.user_id)
            if level is None and clash:
                raise BadRequestError("Reassign this user's team before removing them from the hierarchy")
            if level and any(c.sales_level <= level for c in clash):
                raise BadRequestError("Some of this user's team would no longer be below them; reassign them first")
            target.sales_level, target.manager_id = level, manager_id
            self.commit()
            return {"user_id": target.user_id, "sales_level": target.sales_level, "manager_id": target.manager_id}

    @staticmethod
    def _check_manager(rules: SalesRules, user: Any, target: Any, level: Optional[int],
                       manager_id: Optional[str]) -> None:
        manager_level, in_team = None, False
        if manager_id:
            manager = rules.repos.lookups.user(user.tenant_id, manager_id)
            if not manager or manager.user_id == target.user_id:
                raise BadRequestError("Choose a manager from your workspace")
            manager_level = manager.sales_level
            if manager_level and not (level and manager_level >= level):
                in_team = manager.user_id in rules.collab.team_user_ids(target)
        error = hierarchy_error(level, manager_level, bool(manager_id), in_team)
        if error:
            raise BadRequestError(error)

    # ── settings ────────────────────────────────────────────
    def read_settings(self, user: Any) -> dict:
        with self.work() as rules:
            s = rules.settings(user.tenant_id)
            s["can_edit"] = user.role in ADMIN_ROLES
            return s

    def write_settings(self, user: Any, payload: Dict[str, Any]) -> dict:
        with self.work() as rules:
            changes = {k: v for k, v in payload.items() if k in DEFAULTS}
            error = settings_errors(changes)
            if error:
                raise BadRequestError(error)
            ids = assignment_user_ids(changes)
            if ids and rules.repos.admin.count_users(user.tenant_id, ids) != len(ids):
                raise BadRequestError("Assignment users must belong to your workspace")
            error = days_error(changes)
            if error:
                raise BadRequestError(error)
            if "discount_approval_threshold" in changes:  # BR-SF-17
                changes["discount_approval_threshold"] = parse_threshold(changes["discount_approval_threshold"])
            result = store_settings(rules.repos.settings, user.tenant_id, changes)
            self.commit()
            return result

    # ── workflow rules (BR-SF-13) ───────────────────────────
    @staticmethod
    def workflow_meta() -> dict:
        return {"watch_fields": workflow.WATCH_FIELDS, "settable_fields": workflow.SETTABLE_FIELDS,
                "operators": workflow.OPERATORS, "action_types": list(workflow.ACTION_TYPES)}

    @staticmethod
    def _validate_rule(rules: SalesRules, user: Any, data: dict) -> None:
        error = rule_condition_error(data)
        if error:
            raise BadRequestError(error)
        actions = data.get("actions") or []
        if not actions:
            raise BadRequestError("Add at least one action")
        for a in actions:
            error = action_error(data["object_type"], a)
            if error:
                raise BadRequestError(error)
            for who in action_recipients(a):
                if not rules.repos.admin.user_exists(user.tenant_id, who):
                    raise BadRequestError("Recipients must be owner, manager or a workspace user")

    def list_rules(self, user: Any) -> List[dict]:
        with self.work() as rules:
            return [rule_dict(r) for r in rules.repos.admin.rules(user.tenant_id)]

    def create_rule(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            if not clean_str(data.get("name")):
                raise BadRequestError("Give the rule a name")
            data.setdefault("operator", "changes")
            self._validate_rule(rules, user, data)
            rule = WorkflowRule(tenant_id=user.tenant_id, name=clean_str(data["name"]), object_type=data["object_type"],
                                field=data["field"], operator=data["operator"], value=clean_str(data.get("value")),
                                actions=data["actions"], active=data.get("active", True), created_by=user.user_id)
            rules.repos.admin.add(rule)
            self.commit()
            return rule_dict(rule)

    def update_rule(self, user: Any, rule_id: UUID | str, changes: dict) -> dict:
        rule_id = str(rule_id)
        with self.work() as rules:
            rule = rules.repos.admin.rule(user.tenant_id, rule_id)
            if not rule:
                raise NotFoundError("Rule not found")
            data = {**rule_dict(rule), **changes}
            self._validate_rule(rules, user, data)
            for f in ("object_type", "field", "operator", "actions", "active"):
                setattr(rule, f, data[f])
            rule.name = clean_str(data["name"]) or rule.name
            rule.value = clean_str(data.get("value"))
            self.commit()
            return rule_dict(rule)

    def delete_rule(self, user: Any, rule_id: UUID | str) -> dict:
        rule_id = str(rule_id)
        with self.work() as rules:
            n = rules.repos.admin.delete_rule(user.tenant_id, rule_id)
            self.commit()
            if not n:
                raise NotFoundError("Rule not found")
            return {"status": "deleted", "rule_id": rule_id}

    # ── notifications (BR-SF-07) ────────────────────────────
    def notifications(self, user: Any, unread_only: bool, limit: int) -> dict:
        with self.work() as rules:
            unread = rules.repos.admin.unread_count(user.user_id)
            rows = rules.repos.admin.notifications(user.user_id, unread_only, limit)
            return {"items": [notification_dict(n) for n in rows], "unread": unread,
                    "email_enabled": bool(user.notify_email)}

    def mark_read(self, user: Any, ids: Optional[List[str]]) -> dict:
        with self.work() as rules:
            n = rules.repos.admin.mark_read(user.user_id, ids, utcnow())
            self.commit()
            return {"marked": n}

    def set_email_preference(self, user: Any, email: bool) -> dict:
        with self.work():
            user.notify_email = email
            self.commit()
            return {"email_enabled": email}

    # ── targets (BR-SF-08) ──────────────────────────────────
    @staticmethod
    def _can_set_target(rules: SalesRules, user: Any, target_user_id: str) -> bool:
        if user.role in ADMIN_ROLES or user.sales_level == 1:
            return True
        return target_user_id != user.user_id and target_user_id in rules.collab.team_user_ids(user)

    def targets(self, user: Any, fy: Optional[int]) -> dict:
        with self.work() as rules:
            fy = fy or fiscal_year_of(date.today())
            visible = rules.collab.visible_user_ids(user)
            users = rules.repos.admin.target_users(user.tenant_id, SALES_ROLES, visible)
            rows = {(t.user_id, t.quarter): as_float(t.amount) for t in rules.repos.admin.targets(user.tenant_id, fy)}
            entries: List[Dict[str, Any]] = [{
                "user_id": u.user_id, "name": _full_name(u), "sales_level": u.sales_level,
                "manager_id": u.manager_id, "can_edit": self._can_set_target(rules, user, u.user_id),
                "quarters": [rows.get((u.user_id, q)) for q in QUARTERS],
                "total": round(sum(rows.get((u.user_id, q)) or 0 for q in QUARTERS), 2),
            } for u in users]
            if not amounts_allowed(rules.repos.settings, user):
                for entry in entries:
                    entry["quarters"], entry["total"], entry["can_edit"] = [None] * 4, None, False
            return {"fy": fy, "label": fiscal_label(fy), "start_month": settings.FISCAL_YEAR_START_MONTH,
                    "users": entries}

    def set_target(self, user: Any, user_id: str, fy: int, quarter: int, raw_amount: Any) -> dict:
        with self.work() as rules:
            if quarter not in QUARTERS:
                raise BadRequestError("quarter must be 1 to 4")
            if not rules.repos.admin.user_exists(user.tenant_id, user_id):
                raise NotFoundError("User not found")
            if not self._can_set_target(rules, user, user_id) or not amounts_allowed(rules.repos.settings, user):
                raise PermissionDenied("You can set targets only for people in your team")
            amount = money(raw_amount) or Decimal("0")
            row = rules.repos.admin.target(user.tenant_id, user_id, fy, quarter)
            if row:
                row.amount, row.set_by = amount, user.user_id
            else:
                rules.repos.admin.add(SalesTarget(tenant_id=user.tenant_id, user_id=user_id, fy=fy, quarter=quarter,
                                                  amount=amount, set_by=user.user_id))
            self.commit()
            return {"user_id": user_id, "fy": fy, "quarter": quarter, "amount": float(amount)}

    # ── template library (BR-SF-10) ─────────────────────────
    @staticmethod
    def _template(rules: SalesRules, user: Any, template_id: str, edit: bool = False) -> Any:
        t = rules.repos.admin.template(user.tenant_id, template_id)
        if not t or not (t.shared or t.owner_id == user.user_id):
            raise NotFoundError("Template not found")
        if edit and not template_dict(t, user)["can_edit"]:
            raise PermissionDenied("Only the template's owner or an admin can change it")
        return t

    def templates(self, user: Any, search: Optional[str], category: Optional[str]) -> dict:
        with self.work() as rules:
            rows = rules.repos.admin.templates(user.tenant_id, user.user_id, category, search, TEMPLATE_LIMIT)
            names = rules.user_names([t.owner_id for t in rows])
            cats = rules.repos.admin.template_categories(user.tenant_id, user.user_id)
            return {"items": [template_dict(t, user, names) for t in rows], "categories": cats,
                    "merge_fields": [{"token": k, "label": v} for k, v in MERGE_FIELDS]}

    def create_template(self, user: Any, data: dict) -> dict:
        with self.work() as rules:
            for f in ("name", "subject", "body"):
                if not clean_str(data.get(f)):
                    raise BadRequestError(f"The template needs a {f}")
            t = MessageTemplate(tenant_id=user.tenant_id, name=clean_str(data["name"]),
                                category=clean_str(data.get("category")), subject=data["subject"].strip(),
                                body=data["body"], shared=data.get("shared", True), owner_id=user.user_id)
            rules.repos.admin.add(t)
            self.commit()
            return template_dict(t, user)

    def update_template(self, user: Any, template_id: UUID | str, data: dict) -> dict:
        template_id = str(template_id)
        with self.work() as rules:
            t = self._template(rules, user, template_id, edit=True)
            for f in ("name", "subject", "body"):
                if f in data:
                    if not clean_str(data[f]):
                        raise BadRequestError(f"The template needs a {f}")
                    setattr(t, f, data[f] if f == "body" else data[f].strip())
            if "category" in data:
                t.category = clean_str(data["category"])
            if "shared" in data:
                t.shared = bool(data["shared"])
            self.commit()
            return template_dict(t, user)

    def delete_template(self, user: Any, template_id: UUID | str) -> dict:
        template_id = str(template_id)
        with self.work() as rules:
            t = self._template(rules, user, template_id, edit=True)
            rules.repos.admin.delete(t)
            self.commit()
            return {"status": "deleted", "template_id": template_id}

    def render_template(self, user: Any, template_id: UUID | str, prospect_id: Optional[str], count_use: bool) -> dict:
        """The template with merge fields filled in for one contact (or sample values)."""
        template_id = str(template_id)
        with self.work() as rules:
            t = self._template(rules, user, template_id)
            prospect = None
            if prospect_id:
                prospect = rules.repos.lookups.tenant_prospect(user.tenant_id, prospect_id)
                if not prospect or not rules.collab.can_access_contact(user, prospect):
                    raise NotFoundError("Contact not found")
            sample = prospect or SimpleNamespace(**SAMPLE_CONTACT)
            sender = _full_name(user)
            subject = rules.collab.fill_merge_fields(t.subject, sample, sender)
            body = rules.collab.fill_merge_fields(t.body, sample, sender)
            if count_use:
                t.usage_count = (t.usage_count or 0) + 1
                self.commit()
            return {"subject": subject, "body": body, "sample": prospect is None}


__all__ = ["MERGE_FIELDS", "SalesAdminService", "load_settings"]
