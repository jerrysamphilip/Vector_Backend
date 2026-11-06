"""
Data-access request (DSAR) export of one contact: GET /contacts/{id}/data-export.

Everything the workspace holds about one person, as one JSON file: the contact record and its custom
properties, company links, lists and campaigns (``detail_of``), activities and notes, tasks, leads,
deals with their proposals / quote lines and deal notes, every
email sent to or received from them (subject, body, dates, statuses) and its tracking events, consent
and suppression (DEC-14), and the property history (lifecycle stage included). Erasure is the other
half of the request (POST /contacts/purge, D-12).

Who: the export rules of BR-CM-31 (export_contacts) for an admin, and only a contact the admin can see
(BR-SH-02). Each export leaves a row in the export log (C-07), which never holds the exported data.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, List

from app.core.clock import iso_utc, utcnow
from app.core.errors import PermissionDenied
from app.domain.inbox import is_system_notice
from app.models.crm import CrmTask
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contacts import ContactRepository, ContactsAccess, ContactsUnitOfWork, Session, contacts_uow
from app.services import mentions
from app.services.contact_field_service import definitions_in_order
from app.services.contact_query_service import (
    EVENT_LABELS,
    ExportFile,
    activity_dict,
    can_export,
    detail_of,
    find_contact,
    history_rows,
)
from app.services.export_log import EXPORT_LOG_ROLES, record_export

__all__ = ["ContactDataExportService", "personal_data"]

FORMAT_VERSION = 1
ALL = 1_000_000  # the repository reads take a limit; a person's history is never cut short
NOT_ALLOWED = "Only admins who may export contacts can export a person's data"

TaskSerializer = Callable[[Session, CrmTask], dict]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _record(p: Prospect) -> Dict[str, Any]:
    return {a.key: getattr(p, a.key) for a in Prospect.__mapper__.column_attrs if a.key != "custom_fields"}


def _custom_fields(repo: ContactRepository, p: Prospect) -> List[dict]:
    values = p.custom_fields or {}
    known = [{"key": d.field_key, "label": d.label, "value": values[d.field_key]}
             for d in definitions_in_order(repo, p.tenant_id) if d.field_key in values]
    labelled = {f["key"] for f in known}
    return known + [{"key": k, "label": None, "value": v} for k, v in values.items() if k not in labelled]


def _activities(ctx: ContactsAccess, p: Prospect) -> List[dict]:
    repo = ctx.contacts
    activities = repo.activities_of(p.prospect_id, None, ALL)
    authors = repo.users_by_ids({a.created_by for a in activities if a.created_by})
    mentioned = mentions.for_activities(ctx.session, [a.activity_id for a in activities if a.activity_type == "NOTE"])
    return [activity_dict(a, authors, mentioned.get(a.activity_id)) for a in activities]


def _emails(repo: ContactRepository, p: Prospect) -> List[dict]:
    return [{
        "message_id": m.message_id, "direction": m.direction, "subject": m.subject, "body": m.body_text,
        "from_email": m.from_email, "to_email": m.to_email, "status": m.status, "final_status": m.final_status,
        "failure_reason": m.failure_reason, "scheduled_at": m.scheduled_at, "sent_at": m.sent_at,
        "delivered_at": m.delivered_at, "campaign_id": m.campaign_id, "campaign_name": campaign_name,
    } for m, campaign_name in repo.emails_of(p.prospect_id, ALL) if not is_system_notice(m)]


def _email_events(repo: ContactRepository, p: Prospect) -> List[dict]:
    return [{"message_id": e.message_id, "subject": subject, "event": EVENT_LABELS[e.event_type], "at": e.event_time}
            for e, subject in repo.email_events_of(p.prospect_id, list(EVENT_LABELS), ALL)]


def _columns(obj: Any, skip: tuple = ("row_version",)) -> Dict[str, Any]:
    return {a.key: getattr(obj, a.key) for a in type(obj).__mapper__.column_attrs if a.key not in skip}


def _leads(repo: ContactRepository, p: Prospect) -> List[dict]:
    """Every lead on the person: stage, source, dates, disqualify reason, qualification notes."""
    return [_columns(lead) for lead in repo.leads_of(p.tenant_id, p.prospect_id)]


def _deals(ctx: ContactsAccess, p: Prospect, lead_ids: List[str], exported: set) -> List[dict]:
    """Deals with the person (or from their leads) with stage name, proposals / quote lines and notes.
    ``exported``: activity ids already in the export (not repeated under the deal)."""
    repo = ctx.contacts
    deals = repo.deals_of(p.tenant_id, p.prospect_id, lead_ids)
    ids = [o.opportunity_id for o, _ in deals]
    proposals = repo.proposals_of(p.tenant_id, ids)
    lines: Dict[str, List[dict]] = {}
    for line in repo.proposal_lines_of(p.tenant_id, [x.proposal_id for x in proposals]):
        lines.setdefault(line.proposal_id, []).append(_columns(line, ()))
    by_deal: Dict[str, List[dict]] = {}
    for x in proposals:
        by_deal.setdefault(x.opportunity_id, []).append({**_columns(x), "lines": lines.get(x.proposal_id, [])})
    activities = [a for a in repo.deal_activities(p.tenant_id, ids) if a.activity_id not in exported]
    authors = repo.users_by_ids({a.created_by for a in activities if a.created_by})
    notes: Dict[str, List[dict]] = {}
    for a in activities:
        notes.setdefault(a.opportunity_id or "", []).append(activity_dict(a, authors))
    return [{**_columns(o), "stage_name": stage, "proposals": by_deal.get(o.opportunity_id, []),
             "activities": notes.get(o.opportunity_id, [])} for o, stage in deals]


def _consent(p: Prospect, detail: dict) -> dict:
    return {"consent_status": p.consent_status, "consent_source": p.consent_source,
            "consent_timestamp": p.consent_timestamp, "legal_basis": p.legal_basis,
            "suppression": detail.get("suppression")}


def personal_data(ctx: ContactsAccess, user: User, p: Prospect, task_serializer: TaskSerializer) -> dict:
    """The whole export for one visible contact (no permission check: see ContactDataExportService)."""
    repo = ctx.contacts
    detail = detail_of(ctx, user, p)
    history = history_rows(repo, user, repo.changes(p.prospect_id, None, None, ALL))
    activities = _activities(ctx, p)
    leads = _leads(repo, p)
    deals = _deals(ctx, p, [lead["lead_id"] for lead in leads], {a["activity_id"] for a in activities})
    return {
        "export": {"format_version": FORMAT_VERSION, "generated_at": utcnow(), "generated_by": user.user_id,
                   "tenant_id": user.tenant_id, "prospect_id": p.prospect_id},
        "contact": _record(p),
        "custom_fields": _custom_fields(repo, p),
        "companies": detail.get("companies") or [],
        "lists": detail.get("lists") or [],
        "campaigns": detail.get("campaigns") or [],
        "activities": [a for a in activities if a["activity_type"] != "NOTE"],
        "notes": [a for a in activities if a["activity_type"] == "NOTE"],
        "tasks": [task_serializer(ctx.session, t) for t in repo.tasks_of(p.prospect_id, None, ALL)],
        "emails": _emails(repo, p),
        "email_events": _email_events(repo, p),
        "leads": leads,
        "deals": deals,
        "consent": _consent(p, detail),
        "lifecycle_history": [h for h in history if h["field"] == "lifecycle_stage"],
        "property_history": history,
    }


def may_export_personal_data(user: User) -> bool:
    return user.role in EXPORT_LOG_ROLES and can_export(user)


class ContactDataExportService:
    """One DSAR export per call: checks, builds the file and logs it in one unit of work."""

    def __init__(self, uow: ContactsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "ContactDataExportService":
        return cls(contacts_uow(db))

    def export(self, user: User, prospect_id: str, task_serializer: TaskSerializer) -> ExportFile:
        if not may_export_personal_data(user):
            raise PermissionDenied(NOT_ALLOWED)
        with self.uow as uow:
            p = find_contact(uow, user, prospect_id)  # 404 outside the admin's visibility
            pid = p.prospect_id
            data = personal_data(uow, user, p, task_serializer)
            record_export(uow, user.tenant_id, user.user_id, "CONTACT",
                          f"Personal data export (data-access request) of contact {pid}", 1, "json")
            uow.commit()
        content = json.dumps(data, default=_json_default, ensure_ascii=False, indent=2).encode("utf-8")
        stamp = utcnow().strftime("%Y%m%d-%H%M")
        return ExportFile(content, "application/json", f"contact-data-{pid[:8]}-{stamp}.json")
