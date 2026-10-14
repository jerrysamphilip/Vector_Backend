# app/services/contact_query_service.py
"""
Read side of contact management (BRD v2.0, section 5.2): visibility, filtered and paged
lists, board view, facets, the record page, property history, the unified timeline,
duplicate detection, the Recently deleted list and export.

Users with manage_prospects see every contact; everyone else sees their own and their
team's (BR-SH-02). Every query here goes through ``visible_contacts``/``get_contact``.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.models.account import Account
from app.models.campaign import Campaign, CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.user import User
from app.services import contact_companies, crm, mentions
from app.services.contact_field_service import definitions_in_order, label_map
from app.services.contact_service import (
    MERGE_CHOOSABLE,
    can_access_contact,
    can_manage_contacts,
    phone_digits,
    scope,
    visible_user_ids,
)

SORT_COLUMNS = {
    "name": (Prospect.first_name, Prospect.last_name),
    "email": (Prospect.email,),
    "company": (Prospect.company_name,),
    "lifecycle_stage": (Prospect.lifecycle_stage,),
    "lead_status": (Prospect.lead_status,),
    "created_at": (Prospect.created_at,),
    "updated_at": (Prospect.updated_at,),
    "score": (Prospect.score,),  # BR-CM-43
}

EVENT_LABELS = {
    EmailEvent.EVENT_OPEN: "EMAIL_OPENED",
    EmailEvent.EVENT_CLICK: "EMAIL_CLICKED",
    EmailEvent.EVENT_BOUNCE: "EMAIL_BOUNCED",
    EmailEvent.EVENT_UNSUBSCRIBE: "UNSUBSCRIBED",
}

# Columns available to tables and exports (key -> label)
COLUMNS = {
    "full_name": "Name", "email": "Email", "phone": "Phone", "mobile_phone": "Mobile",
    "designation": "Job title", "company_name": "Company", "owner_name": "Owner",
    "lifecycle_stage": "Lifecycle stage", "lead_status": "Lead status", "lead_source": "Lead source",
    "tags": "Tags", "industry": "Industry", "poc_city": "City", "poc_state": "State",
    "poc_country": "Country", "timezone": "Time zone", "consent_status": "Email subscription",
    "legal_basis": "Legal basis", "linkedin_url": "LinkedIn", "last_activity_at": "Last activity",
    "last_contacted_at": "Last contacted", "created_at": "Create date", "updated_at": "Last updated",
    "score": "Score",  # BR-CM-43: fit + engagement score with its grade
}

EXPORT_ROLES = ("SUPER_ADMIN", "ADMIN", "MANAGER")
EXPORT_ROW_LIMIT = 100_000
EXPORT_CHUNK = 1000

_HISTORY_LABELS = {"account_id": "Company", "company_association": "Associated company",
                   "owner_id": "Owner", "emp_band": "Employees",
                   "deleted": "Deleted", "created": "Created",
                   "score_grade": "Score grade"}

_DUPLICATE_REASONS = {"email": "Same email address", "name": "Same name and company",
                      "initial": "Similar name at the same company", "phone": "Same phone number"}

_PHONE_QUERY = re.compile(r"[\d\s+().\-/]+")


@dataclass
class ContactFilters:
    """Filters shared by the list, board and export views (all optional)."""
    q: Optional[str] = None
    owner: Optional[str] = None
    account_id: Optional[str] = None
    tag: Optional[str] = None
    list_id: Optional[str] = None
    campaign_id: Optional[str] = None
    consent_status: Optional[str] = None
    email_valid: Optional[bool] = None
    has_phone: Optional[bool] = None
    country: Optional[str] = None
    industry: Optional[str] = None
    lifecycle_stage: Optional[str] = None
    lead_status: Optional[str] = None
    filters: Optional[str] = None
    grade: Optional[str] = None
    score_min: Optional[int] = None
    score_max: Optional[int] = None


@dataclass
class ExportFile:
    content: bytes
    media_type: str
    filename: str


# ── Small helpers ──────────────────────────────────────────────────

def user_name(u: Optional[User]) -> Optional[str]:
    return f"{u.first_name} {u.last_name}".strip() if u else None


def can_export(user: User) -> bool:
    """Role right (BR-CM-31); a user with customised permissions also needs export_data."""
    custom = user.custom_permissions
    return user.role in EXPORT_ROLES and (custom is None or "export_data" in custom)


def parse_grades(grade: str) -> List[str]:
    """'a, B,x' -> ['A', 'B']; unknown grades are dropped."""
    return [g for g in (x.strip().upper() for x in grade.split(",")) if g in ("A", "B", "C", "D")]


def search_condition(q: str) -> ColumnElement:
    """Match name, email, company, title or phone (digits only, formatting ignored).

    The digits-only phone match applies only when the query looks like a phone number (digits and
    phone punctuation, e.g. "737 555 0188", "+1 (737) 555-0188"). Otherwise a mixed query such as
    "zorblax66034" would also match every record whose text merely contains "66034"."""
    term = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions = [Prospect.search_text.like(f"%{term}%")]
    digits = phone_digits(q)
    if len(digits) >= 4 and digits != term and _PHONE_QUERY.fullmatch(q.strip()):
        conditions.append(Prospect.search_text.like(f"%{digits}%"))
    return or_(*conditions)


# ── Visibility ─────────────────────────────────────────────────────

def visible_contacts(db: Session, user: User) -> Query:
    """Contacts this user may see (not deleted; own team's only without manage_prospects)."""
    query = db.query(Prospect).filter(Prospect.tenant_id == user.tenant_id, Prospect.deleted_at.is_(None))
    # Own records plus those of everyone below in the sales hierarchy (BR-SH-02)
    return scope(query, db, user, Prospect.owner_id)


def get_contact(db: Session, user: User, prospect_id: str) -> Prospect:
    """A live contact the user may see, else NotFoundError (no leak of other teams' records)."""
    prospect = db.query(Prospect).filter(
        Prospect.prospect_id == prospect_id,
        Prospect.tenant_id == user.tenant_id,
        Prospect.deleted_at.is_(None),
    ).first()
    if not prospect or not can_access_contact(user, prospect):
        raise NotFoundError("Contact not found")
    return prospect


def filtered_query(db: Session, user: User, f: ContactFilters) -> Query:
    query = visible_contacts(db, user)
    # Owner filters narrow within what the user may already see
    if f.owner == "me":
        query = query.filter(Prospect.owner_id == user.user_id)
    elif f.owner == "unassigned":
        query = query.filter(Prospect.owner_id.is_(None))
    elif f.owner and f.owner != "team":
        query = query.filter(Prospect.owner_id == f.owner)

    if f.q and f.q.strip():
        query = query.filter(search_condition(f.q))

    if f.account_id:
        # Primary company or any other associated company (CM-014)
        query = query.filter(contact_companies.company_condition(db, f.account_id))
    if f.tag:
        query = query.filter(func.json_contains(Prospect.tags, func.json_quote(f.tag)) == 1)
    if f.list_id:
        plist = db.query(ProspectList).filter(ProspectList.list_id == f.list_id,
                                              ProspectList.tenant_id == user.tenant_id).first()
        if not plist:
            raise NotFoundError("List not found")
        query = query.filter(Prospect.prospect_id.in_(crm.list_member_ids(db, plist, user)))
    if f.campaign_id:
        query = query.filter(Prospect.prospect_id.in_(
            db.query(CampaignProspect.prospect_id).filter(CampaignProspect.campaign_id == f.campaign_id)))
    if f.consent_status:
        query = query.filter(Prospect.consent_status == f.consent_status)
    if f.email_valid is not None:
        query = query.filter(Prospect.is_valid_email == f.email_valid)
    if f.has_phone is True:
        query = query.filter(or_(func.coalesce(Prospect.phone, "") != "", func.coalesce(Prospect.mobile_phone, "") != ""))
    elif f.has_phone is False:
        query = query.filter(func.coalesce(Prospect.phone, "") == "", func.coalesce(Prospect.mobile_phone, "") == "")
    if f.country:
        query = query.filter(Prospect.poc_country == f.country)
    if f.industry:
        query = query.filter(Prospect.industry == f.industry)
    if f.lifecycle_stage:
        query = query.filter(Prospect.lifecycle_stage == f.lifecycle_stage)
    if f.lead_status:
        query = query.filter(Prospect.lead_status == f.lead_status)
    # Contact score (BR-CM-43): grade(s) "A" or "A,B", and a 0-100 range
    if f.grade:
        grades = parse_grades(f.grade)
        if not grades:
            raise BadRequestError("grade must be A, B, C or D (comma-separated)")
        query = query.filter(Prospect.score_grade.in_(grades))
    if f.score_min is not None:
        query = query.filter(Prospect.score >= f.score_min)
    if f.score_max is not None:
        query = query.filter(Prospect.score <= f.score_max)
    if f.filters:
        try:
            clause = crm.filter_clause(db, crm.parse_filter_param(f.filters), user)
        except crm.FilterError as exc:
            raise BadRequestError(str(exc)) from exc
        if clause is not None:
            query = query.filter(clause)
    return query


# ── Serialisation ──────────────────────────────────────────────────

def _bump(store: Dict[str, datetime], pid: str, ts: Optional[datetime]) -> None:
    if ts and (not store.get(pid) or ts > store[pid]):
        store[pid] = ts


def _last_touch(db: Session, ids: List[str]) -> Tuple[Dict[str, datetime], Dict[str, datetime]]:
    """(last activity, last contacted) per contact, from logged activities and sent emails."""
    last_activity: Dict[str, datetime] = {}
    last_contacted: Dict[str, datetime] = {}
    if not ids:
        return last_activity, last_contacted
    for pid, kind, ts in db.query(ContactActivity.prospect_id, ContactActivity.activity_type,
                                  func.max(ContactActivity.occurred_at)).filter(
            ContactActivity.prospect_id.in_(ids)).group_by(ContactActivity.prospect_id, ContactActivity.activity_type):
        _bump(last_activity, pid, ts)
        if kind in ("CALL", "EMAIL", "MEETING"):
            _bump(last_contacted, pid, ts)
    for pid, ts in db.query(EmailMessage.prospect_id, func.max(EmailMessage.sent_at)).filter(
            EmailMessage.prospect_id.in_(ids), EmailMessage.sent_at.isnot(None),
            EmailMessage.direction == "OUTBOUND").group_by(EmailMessage.prospect_id):
        _bump(last_activity, pid, ts)
        _bump(last_contacted, pid, ts)
    return last_activity, last_contacted


def summaries(db: Session, prospects: List[Prospect]) -> List[dict]:
    """Serialise contacts for tables, batch-loading owners, companies and activity dates."""
    ids = [p.prospect_id for p in prospects]
    owner_ids = {p.owner_id for p in prospects if p.owner_id}
    account_ids = {p.account_id for p in prospects if p.account_id}
    owners = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(owner_ids))} if owner_ids else {}
    accounts = {a.account_id: (a.name, a.domain) for a in db.query(Account.account_id, Account.name, Account.domain).filter(
        Account.account_id.in_(account_ids), Account.deleted_at.is_(None))} if account_ids else {}
    last_activity, last_contacted = _last_touch(db, ids)

    return [{
        "prospect_id": p.prospect_id,
        "first_name": p.first_name,
        "last_name": p.last_name,
        "full_name": p.full_name,
        "email": p.email,
        "phone": p.phone,
        "mobile_phone": p.mobile_phone,
        "designation": p.designation,
        "company_name": p.company_name,
        "account_id": p.account_id,
        "account_name": accounts.get(p.account_id, (None, None))[0],
        "account_domain": accounts.get(p.account_id, (None, None))[1],
        "owner_id": p.owner_id,
        "owner_name": user_name(owners.get(p.owner_id)),
        "tags": p.tags or [],
        "industry": p.industry,
        "emp_band": p.emp_band,
        "poc_city": p.poc_city,
        "poc_state": p.poc_state,
        "poc_country": p.poc_country,
        "timezone": p.timezone,
        "linkedin_url": p.linkedin_url,
        "lifecycle_stage": p.lifecycle_stage,
        "lead_status": p.lead_status,
        "lead_source": p.lead_source,
        "legal_basis": p.legal_basis,
        "consent_status": p.consent_status,
        "is_valid_email": p.is_valid_email,
        "custom_fields": p.custom_fields or {},
        "quality_flags": crm.quality_flags(p),
        # Contact score (BR-CM-43); None until first scored
        "score": p.score,
        "fit_score": p.fit_score,
        "engagement_score": p.engagement_score,
        "score_grade": p.score_grade,
        "scored_at": p.scored_at,
        "last_activity_at": last_activity.get(p.prospect_id),
        "last_contacted_at": last_contacted.get(p.prospect_id),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
    } for p in prospects]


def activity_dict(a: ContactActivity, authors: Dict[str, User], mentioned: Optional[List[dict]] = None) -> dict:
    return {
        "mentions": mentioned or [],
        "activity_id": a.activity_id,
        "prospect_id": a.prospect_id,
        "activity_type": a.activity_type,
        "subject": a.subject,
        "body": a.body,
        "outcome": a.outcome,
        "duration_minutes": a.duration_minutes,
        "occurred_at": a.occurred_at,
        "created_by": a.created_by,
        "created_by_name": user_name(authors.get(a.created_by)),
        "created_at": a.created_at,
    }


def _account_card(db: Session, account_id: Optional[str]) -> Optional[dict]:
    account = db.query(Account).filter(Account.account_id == account_id,
                                       Account.deleted_at.is_(None)).first() if account_id else None
    if not account:
        return None
    return {
        "account_id": account.account_id, "name": account.name, "domain": account.domain,
        "website": account.website, "industry": account.industry, "emp_band": account.emp_band,
        "phone": account.phone, "city": account.city, "country": account.country,
        "lifecycle_stage": account.lifecycle_stage,
        "contact_count": db.query(func.count(Prospect.prospect_id)).filter(
            Prospect.account_id == account.account_id, Prospect.deleted_at.is_(None)).scalar(),
    }


def _contact_lists(db: Session, user: User, p: Prospect) -> List[dict]:
    static = [{
        "list_id": lst.list_id, "list_name": lst.list_name, "list_type": "STATIC", "added_at": m.added_at,
        "notes": m.notes,
    } for m, lst in db.query(ProspectListMember, ProspectList).join(
        ProspectList, ProspectList.list_id == ProspectListMember.list_id
    ).filter(ProspectListMember.prospect_id == p.prospect_id).order_by(ProspectListMember.added_at.desc())]
    active = []
    for plist in db.query(ProspectList).filter(ProspectList.tenant_id == p.tenant_id, ProspectList.list_type == "ACTIVE"):
        try:
            if crm.list_member_ids(db, plist, user).filter(Prospect.prospect_id == p.prospect_id).first():
                active.append({"list_id": plist.list_id, "list_name": plist.list_name, "list_type": "ACTIVE"})
        except crm.FilterError:
            continue  # an active list with a broken filter matches nobody
    return static + active


def _contact_stats(db: Session, p: Prospect) -> dict:
    sent = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
        EmailMessage.prospect_id == p.prospect_id, EmailMessage.direction == "OUTBOUND",
        EmailMessage.sent_at.isnot(None)).one()
    received = db.query(func.count(EmailMessage.message_id), func.max(EmailMessage.sent_at)).filter(
        EmailMessage.prospect_id == p.prospect_id, EmailMessage.direction == "INBOUND").one()
    opens = db.query(func.count(func.distinct(EmailEvent.message_id))).join(
        EmailMessage, EmailMessage.message_id == EmailEvent.message_id
    ).filter(EmailMessage.prospect_id == p.prospect_id, EmailEvent.event_type == EmailEvent.EVENT_OPEN).scalar()
    activity_counts = dict(db.query(ContactActivity.activity_type, func.count(ContactActivity.activity_id)).filter(
        ContactActivity.prospect_id == p.prospect_id).group_by(ContactActivity.activity_type))
    open_tasks = db.query(func.count(CrmTask.task_id)).filter(
        CrmTask.prospect_id == p.prospect_id, CrmTask.status == "OPEN").scalar()
    return {
        "emails_sent": sent[0], "last_emailed_at": sent[1],
        "replies": received[0], "last_reply_at": received[1],
        "emails_opened": opens or 0,
        "notes": activity_counts.get("NOTE", 0),
        "calls": activity_counts.get("CALL", 0),
        "meetings": activity_counts.get("MEETING", 0),
        "logged_emails": activity_counts.get("EMAIL", 0),
        "open_tasks": open_tasks or 0,
    }


def contact_detail(db: Session, user: User, p: Prospect) -> dict:
    """The record page: summary plus company, lists, campaigns, persona and engagement stats."""
    detail = summaries(db, [p])[0]
    detail.update({
        "email_type": p.email_type,
        "email_provider": p.email_provider,
        "consent_source": p.consent_source,
        "consent_timestamp": p.consent_timestamp,
    })
    detail["account"] = _account_card(db, p.account_id)
    detail["companies"] = contact_companies.links(db, p)
    detail["lists"] = _contact_lists(db, user, p)
    detail["campaigns"] = [{
        "campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "campaign_status": c.status,
        "status": e.status, "current_step": e.current_step, "stopped_reason": e.stopped_reason,
        "enrolled_at": e.enrolled_at,
    } for e, c in db.query(CampaignProspect, Campaign).join(
        Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
    ).filter(CampaignProspect.prospect_id == p.prospect_id).order_by(CampaignProspect.enrolled_at.desc())]

    persona = db.query(ProspectPersona).filter(ProspectPersona.prospect_id == p.prospect_id).first()
    detail["persona"] = {"persona_type": persona.persona_type, "confidence_score": persona.confidence_score} if persona else None
    detail["stats"] = _contact_stats(db, p)
    detail["can_edit_owner"] = can_manage_contacts(user)
    detail["can_delete"] = can_manage_contacts(user) and user.role != "AGENT"
    detail["can_override_lifecycle"] = crm.can_override_lifecycle(user)
    return detail


def companies_response(db: Session, prospect: Prospect) -> dict:
    return {"prospect_id": prospect.prospect_id, "items": contact_companies.links(db, prospect)}


# ── Collection views ───────────────────────────────────────────────

def contact_meta(user: User) -> dict:
    """Picklist values and table columns for the UI."""
    return {
        "lifecycle_stages": [{"value": k, "label": v} for k, v in crm.LIFECYCLE_STAGES.items()],
        "lead_statuses": [{"value": k, "label": v} for k, v in crm.LEAD_STATUSES.items()],
        "legal_bases": [{"value": k, "label": v} for k, v in crm.LEGAL_BASES.items()],
        "columns": [{"key": k, "label": v} for k, v in COLUMNS.items()],
        "can_manage": can_manage_contacts(user),
        "can_export": can_manage_contacts(user) and user.role in EXPORT_ROLES,
        "can_override_lifecycle": crm.can_override_lifecycle(user),
        "restore_window_days": crm.RESTORE_WINDOW_DAYS,
    }


def list_page(db: Session, user: User, f: ContactFilters, sort_by: str, sort_order: str,
              page: int, page_size: int) -> dict:
    query = filtered_query(db, user, f)
    direction: Callable[[Any], Any] = (lambda c: c.asc()) if sort_order == "asc" else (lambda c: c.desc())
    order = [direction(c) for c in SORT_COLUMNS[sort_by]] + [direction(Prospect.prospect_id)]
    if f.q and f.q.strip():
        # A search usually matches few rows: scan once, counting with a window function, and
        # don't let MySQL walk the date index looking for matches (contact search < 1 s at 100k)
        paged = query.add_columns(func.count().over().label("total")).with_hint(
            Prospect, "IGNORE INDEX FOR ORDER BY (ix_prospects_tenant_live_created, ix_prospects_tenant_live_updated)",
            "mysql").order_by(*order).offset((page - 1) * page_size).limit(page_size).all()
        rows = [r[0] for r in paged]
        total = paged[0][1] if paged else query.with_entities(func.count(Prospect.prospect_id)).order_by(None).scalar()
    else:
        # COUNT on the id (not .count(), which wraps every column in a subquery); the tiebreaker
        # sorts the same way as the sort column, so MySQL walks the index instead of sorting
        total = query.with_entities(func.count(Prospect.prospect_id)).order_by(None).scalar()
        rows = query.order_by(*order).offset((page - 1) * page_size).limit(page_size).all()
    return {"items": summaries(db, rows), "total": total, "page": page, "page_size": page_size}


def board(db: Session, user: User, group_by: str, per_column: int, f: ContactFilters) -> dict:
    """Board view: contacts grouped by lifecycle stage or lead status (BR-CM-20)."""
    values = crm.LIFECYCLE_STAGES if group_by == "lifecycle_stage" else crm.LEAD_STATUSES
    column = getattr(Prospect, group_by)
    base = filtered_query(db, user, f)
    counts = dict(base.with_entities(column, func.count(Prospect.prospect_id)).group_by(column).all())
    lanes, all_rows = [], []
    for value, label in list(values.items()) + [(None, "Not set")]:
        rows = base.filter(column == value if value else column.is_(None)).order_by(
            Prospect.updated_at.desc()).limit(per_column).all() if counts.get(value) else []
        if value is None and not rows:
            continue
        lanes.append((value, label, rows))
        all_rows.extend(rows)
    by_id = {s["prospect_id"]: s for s in summaries(db, all_rows)}
    return {"group_by": group_by, "lanes": [
        {"value": v, "label": lbl, "total": counts.get(v, 0), "items": [by_id[r.prospect_id] for r in rows]}
        for v, lbl, rows in lanes]}


def tag_counts(tag_lists: Iterable[Optional[Sequence[str]]]) -> List[dict]:
    """Tags counted case-insensitively (first spelling kept), most used first then by name."""
    counts: Dict[str, list] = {}
    for tags in tag_lists:
        for tag in tags or []:
            entry = counts.setdefault(tag.lower(), [tag, 0])
            entry[1] += 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.values(), key=lambda x: (-x[1], x[0].lower()))]


def facets(db: Session, user: User) -> dict:
    """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
    base = visible_contacts(db, user)

    def distinct(column: Any) -> List[Any]:
        return sorted(v for (v,) in base.with_entities(column).filter(column.isnot(None), column != "").distinct())

    lists = db.query(ProspectList.list_id, ProspectList.list_name, ProspectList.list_type).filter(
        ProspectList.tenant_id == user.tenant_id).order_by(ProspectList.uploaded_at.desc()).all()
    campaigns = db.query(Campaign.campaign_id, Campaign.campaign_name, Campaign.status).filter(
        Campaign.tenant_id == user.tenant_id).order_by(Campaign.created_at.desc()).all()

    return {
        "tags": tag_counts(t for (t,) in base.with_entities(Prospect.tags).filter(Prospect.tags.isnot(None))),
        "countries": distinct(Prospect.poc_country),
        "industries": distinct(Prospect.industry),
        "lead_sources": distinct(Prospect.lead_source),
        "lists": [{"list_id": i, "list_name": n, "list_type": t} for i, n, t in lists],
        "campaigns": [{"campaign_id": i, "campaign_name": n, "status": s} for i, n, s in campaigns],
    }


def owners(db: Session, user: User) -> List[dict]:
    """Active users in the workspace, for owner pickers (hierarchy users: their own team)."""
    users = db.query(User).filter(
        User.tenant_id == user.tenant_id, User.status == "ACTIVE"
    ).order_by(User.first_name, User.last_name).all()
    visible = visible_user_ids(db, user)
    if visible is not None:  # BR-SH-02
        users = [u for u in users if u.user_id in visible]
    return [{"user_id": u.user_id, "name": user_name(u), "email": u.email, "role": u.role} for u in users]


def mentionable_users(db: Session, user: User, q: Optional[str], limit: int,
                      prospect_id: Optional[str], opportunity_id: Optional[str]) -> List[dict]:
    """Colleagues who can be @mentioned (BR-CM-16); scoped to who can see the record when given."""
    if prospect_id:
        prospect = get_contact(db, user, prospect_id)
        return mentions.mentionable_users(db, user, q, limit, prospect.owner_id, True)
    if opportunity_id:
        from app.services import sales as sales_svc
        opp = sales_svc.get_opp(db, user, opportunity_id)
        return mentions.mentionable_users(db, user, q, limit, opp.owner_id, True)
    return mentions.mentionable_users(db, user, q, limit)


# ── Export (BR-CM-31) ──────────────────────────────────────────────

def export_cell(key: str, value: Any) -> Any:
    """A summary value as shown in an export cell."""
    if key == "tags":
        return "; ".join(value or [])
    if key == "lifecycle_stage":
        return crm.LIFECYCLE_STAGES.get(value, value)
    if key == "lead_status":
        return crm.LEAD_STATUSES.get(value, value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return value


def export_columns(columns: Optional[str]) -> List[str]:
    """Requested known column keys in order; all columns when none are valid."""
    return [c for c in (columns or "").split(",") if c in COLUMNS] or list(COLUMNS)


def export_contacts(db: Session, user: User, fmt: str, columns: Optional[str], f: ContactFilters) -> ExportFile:
    """A view as CSV / XLSX. Admins and managers only."""
    if not can_export(user):
        raise PermissionDenied("Only admins and managers can export contacts")
    keys = export_columns(columns)
    field_defs = definitions_in_order(db, user.tenant_id)
    query = filtered_query(db, user, f).order_by(Prospect.created_at.desc())

    header = [COLUMNS[k] for k in keys] + [d.label for d in field_defs]
    rows = []
    for offset in range(0, EXPORT_ROW_LIMIT, EXPORT_CHUNK):
        chunk = query.offset(offset).limit(EXPORT_CHUNK).all()
        if not chunk:
            break
        for s in summaries(db, chunk):
            values = [export_cell(k, s.get(k)) for k in keys]
            for d in field_defs:
                v = s["custom_fields"].get(d.field_key)
                values.append("; ".join(v) if isinstance(v, list) else v)
            rows.append(crm.csv_safe_row(values))  # no formula injection in Excel / Sheets

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M")
    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Contacts"
        ws.append(crm.csv_safe_row(header))
        for r in rows:
            ws.append(r)
        buffer = io.BytesIO()
        wb.save(buffer)
        return ExportFile(buffer.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          f"contacts-{stamp}.xlsx")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(crm.csv_safe_row(header))
    writer.writerows(rows)
    return ExportFile(out.getvalue().encode("utf-8-sig"), "text/csv", f"contacts-{stamp}.csv")


# ── Recently deleted (BR-CM-35) ────────────────────────────────────

def deleted_page(db: Session, user: User, q: Optional[str], page: int, page_size: int) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS)
    query = db.query(Prospect).filter(
        Prospect.tenant_id == user.tenant_id, Prospect.deleted_at.isnot(None),
        Prospect.deleted_at > cutoff, Prospect.merged_into_id.is_(None))
    query = scope(query, db, user, Prospect.owner_id)  # own team's contacts only (BR-SH-02)
    if q and q.strip():
        term = f"%{q.strip()}%"
        query = query.filter(or_(Prospect.email.ilike(term), Prospect.first_name.ilike(term),
                                 Prospect.last_name.ilike(term), Prospect.company_name.ilike(term)))
    total = query.count()
    rows = query.order_by(Prospect.deleted_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    deleters = {u.user_id: u for u in db.query(User).filter(User.user_id.in_({p.deleted_by for p in rows if p.deleted_by}))}
    items = []
    for s, p in zip(summaries(db, rows), rows, strict=True):
        s.update(deleted_at=p.deleted_at, deleted_by_name=user_name(deleters.get(p.deleted_by)),
                 purge_at=p.deleted_at + timedelta(days=crm.RESTORE_WINDOW_DAYS))
        items.append(s)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ── Duplicates (BR-CM-32) ──────────────────────────────────────────

def normalize_name(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def email_key(email: Optional[str]) -> str:
    """Same mailbox apart from case, +tags and (for personal domains) dots in the local part."""
    local, _, domain = (email or "").lower().partition("@")
    local = local.split("+")[0]
    if crm.is_personal_domain(domain):
        local = local.replace(".", "")
    return f"{local}@{domain}"


def duplicate_keys(row: Any) -> List[tuple]:
    """Bucket keys a contact row falls into: email, name+company, initial+company, phone."""
    keys: List[tuple] = [("email", email_key(row.email))]
    first, last = normalize_name(row.first_name), normalize_name(row.last_name)
    company = row.account_id or normalize_name(row.company_name)
    if first and last and company:
        keys.append(("name", first, last, company))
        if len(first) > 1:  # "Rob" / "Robert" and initials at the same company
            keys.append(("initial", first[0], last, company))
    for phone in {phone_digits(row.phone), phone_digits(row.mobile_phone)}:
        if len(phone) >= 7:
            keys.append(("phone", phone[-10:]))
    return keys


def duplicate_groups(rows: Iterable[Any], limit: int) -> List[dict]:
    """Group rows sharing any bucket key (transitively by first match); [{ids, reasons}]."""
    buckets: Dict[tuple, Set[str]] = {}
    for r in rows:
        for key in duplicate_keys(r):
            buckets.setdefault(key, set()).add(r.prospect_id)

    groups: List[dict] = []
    seen: Dict[str, int] = {}
    for key, ids in buckets.items():
        if len(ids) < 2:
            continue
        reason = _DUPLICATE_REASONS[key[0]]
        hit = next((seen[i] for i in ids if i in seen), None)
        if hit is None:
            groups.append({"ids": set(ids), "reasons": {reason}})
            hit = len(groups) - 1
        else:
            groups[hit]["ids"] |= ids
            groups[hit]["reasons"].add(reason)
        for i in groups[hit]["ids"]:
            seen[i] = hit
    return groups[:limit]


def find_duplicates(db: Session, user: User, limit: int) -> dict:
    """Groups of likely duplicates: same email (Gmail-style), similar name at a company, same phone."""
    rows = visible_contacts(db, user).with_entities(
        Prospect.prospect_id, Prospect.first_name, Prospect.last_name, Prospect.company_name,
        Prospect.account_id, Prospect.phone, Prospect.mobile_phone, Prospect.email,
    ).all()
    groups = duplicate_groups(rows, limit)
    all_ids = {i for g in groups for i in g["ids"]}
    by_id = {p.prospect_id: p for p in db.query(Prospect).filter(Prospect.prospect_id.in_(all_ids))} if all_ids else {}
    by_summary = {s["prospect_id"]: s for s in summaries(db, list(by_id.values()))}
    return {
        "groups": [{
            "reasons": sorted(g["reasons"]),
            "contacts": sorted((by_summary[i] for i in g["ids"] if i in by_summary),
                               key=lambda s: s["created_at"] or datetime.min),
        } for g in groups],
        "total_groups": len(groups),
        "mergeable_properties": list(MERGE_CHOOSABLE),
    }


# ── Property history (BR-CM-11) ────────────────────────────────────

def history_label(field: str, defs: Dict[str, str]) -> str:
    if field.startswith("custom."):
        return defs.get(field[len("custom."):], field[len("custom."):])
    return COLUMNS.get(field, _HISTORY_LABELS.get(field, field.replace("_", " ").capitalize()))


def history_value(field: str, value: Optional[str], users: Dict[str, Optional[str]],
                  companies: Dict[str, str]) -> Optional[str]:
    if value is None:
        return None
    if field == "owner_id":
        return users.get(value, value)
    if field in ("account_id", "company_association"):
        return companies.get(value, value)
    if field == "lifecycle_stage":
        return crm.LIFECYCLE_STAGES.get(value, value)
    if field == "lead_status":
        return crm.LEAD_STATUSES.get(value, value)
    if field == "legal_basis":
        return crm.LEGAL_BASES.get(value, value)
    return value


def history_items(db: Session, user: User, rows: List[PropertyChange]) -> List[dict]:
    defs = label_map(db, user.tenant_id)
    user_ids = {r.changed_by for r in rows} | {r.old_value for r in rows if r.field == "owner_id"} | \
               {r.new_value for r in rows if r.field == "owner_id"}
    users = {u.user_id: user_name(u) for u in db.query(User).filter(User.user_id.in_({i for i in user_ids if i}))}
    company_ids = {v for r in rows if r.field in ("account_id", "company_association")
                   for v in (r.old_value, r.new_value) if v}
    companies = {a.account_id: a.name for a in db.query(Account).filter(Account.account_id.in_(company_ids))} if company_ids else {}
    return [{
        "change_id": r.change_id, "field": r.field, "label": history_label(r.field, defs),
        "old_value": history_value(r.field, r.old_value, users, companies),
        "new_value": history_value(r.field, r.new_value, users, companies),
        "source": r.source, "changed_by": r.changed_by, "changed_by_name": users.get(r.changed_by),
        "changed_at": r.changed_at,
    } for r in rows]


def _changes_query(db: Session, prospect: Prospect) -> Query:
    return db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                           PropertyChange.object_id == prospect.prospect_id)


def property_history(db: Session, user: User, prospect_id: str, field: Optional[str]) -> dict:
    prospect = get_contact(db, user, prospect_id)
    query = _changes_query(db, prospect)
    if field:
        query = query.filter(PropertyChange.field == field)
    return {"items": history_items(db, user, query.order_by(PropertyChange.changed_at.desc()).limit(1000).all())}


# ── Timeline (BR-CM-14) ────────────────────────────────────────────

def parse_timeline_types(types: Optional[str]) -> Optional[Set[str]]:
    """'note, call' -> {'NOTE', 'CALL'}; None means every kind."""
    return {t.strip().upper() for t in types.split(",")} if types else None


def _email_items(db: Session, prospect: Prospect, limit: int) -> List[dict]:
    items = []
    messages = db.query(EmailMessage, Campaign.campaign_name).outerjoin(
        Campaign, Campaign.campaign_id == EmailMessage.campaign_id
    ).filter(
        EmailMessage.prospect_id == prospect.prospect_id,
        or_(EmailMessage.sent_at.isnot(None), EmailMessage.direction == "INBOUND"),
    ).order_by(func.coalesce(EmailMessage.sent_at, EmailMessage.scheduled_at).desc()).limit(limit).all()
    for m, campaign_name in messages:
        inbound = m.direction == "INBOUND"
        items.append({
            "kind": "EMAIL_RECEIVED" if inbound else "EMAIL_SENT",
            "at": m.sent_at or m.scheduled_at,
            "email": {
                "message_id": m.message_id, "subject": m.subject, "subject_variant": m.subject_variant,
                "snippet": (m.body_text or "")[:400], "status": m.status, "final_status": m.final_status,
                "failure_reason": m.failure_reason,
                "from_email": m.from_email, "to_email": m.to_email,
                "campaign_id": m.campaign_id, "campaign_name": campaign_name,
                "conversation_id": m.conversation_id,
            },
        })
    events = db.query(EmailEvent, EmailMessage.subject).join(
        EmailMessage, EmailMessage.message_id == EmailEvent.message_id
    ).filter(
        EmailMessage.prospect_id == prospect.prospect_id,
        EmailEvent.event_type.in_(list(EVENT_LABELS)),
    ).order_by(EmailEvent.event_time.asc()).limit(limit).all()
    opened = set()
    for e, subject in events:
        # One "opened" entry per email (the first open), not one per pixel load
        if e.event_type == EmailEvent.EVENT_OPEN:
            if e.message_id in opened:
                continue
            opened.add(e.message_id)
        items.append({"kind": EVENT_LABELS[e.event_type], "at": e.event_time,
                      "email": {"message_id": e.message_id, "subject": subject}})
    return items


def timeline(db: Session, user: User, prospect_id: str, types: Optional[str], user_id: Optional[str],
             limit: int, task_serializer: Callable[[Session, CrmTask], dict]) -> dict:
    """Everything that happened with this contact, newest first. ``task_serializer`` renders a
    task the way the tasks API does (passed in so this service does not depend on a router)."""
    prospect = get_contact(db, user, prospect_id)
    wanted = parse_timeline_types(types)

    def want(kind: str) -> bool:
        return wanted is None or kind in wanted

    items: List[dict] = []

    activity_query = db.query(ContactActivity).filter(ContactActivity.prospect_id == prospect.prospect_id)
    if user_id:
        activity_query = activity_query.filter(ContactActivity.created_by == user_id)
    activities = activity_query.order_by(ContactActivity.occurred_at.desc()).limit(limit).all()
    author_ids = {a.created_by for a in activities if a.created_by}
    authors = {u.user_id: u for u in db.query(User).filter(User.user_id.in_(author_ids))} if author_ids else {}
    mentioned = mentions.for_activities(db, [a.activity_id for a in activities if a.activity_type == "NOTE"])
    for a in activities:
        kind = "EMAIL_LOGGED" if a.activity_type == "EMAIL" else a.activity_type
        if want(a.activity_type):
            items.append({"kind": kind, "at": a.occurred_at,
                          "activity": activity_dict(a, authors, mentioned.get(a.activity_id)),
                          "user_id": a.created_by,
                          "can_edit": a.created_by == user.user_id or can_manage_contacts(user)})

    if want("TASK"):
        task_query = db.query(CrmTask).filter(CrmTask.prospect_id == prospect.prospect_id)
        if user_id:
            task_query = task_query.filter(CrmTask.created_by == user_id)
        for t in task_query.order_by(CrmTask.created_at.desc()).limit(limit):
            items.append({"kind": "TASK", "at": t.completed_at or t.created_at, "task": task_serializer(db, t),
                          "user_id": t.created_by})

    if want("PROPERTY"):
        change_query = _changes_query(db, prospect)
        if user_id:
            change_query = change_query.filter(PropertyChange.changed_by == user_id)
        for c in history_items(db, user, change_query.order_by(PropertyChange.changed_at.desc()).limit(limit).all()):
            items.append({"kind": "PROPERTY_CHANGE", "at": c["changed_at"], "change": c, "user_id": c["changed_by"]})

    if want("EMAIL") and not user_id:
        items.extend(_email_items(db, prospect, limit))

    if (want("CAMPAIGN") or want("EMAIL")) and not user_id:
        for e, c in db.query(CampaignProspect, Campaign).join(
                Campaign, Campaign.campaign_id == CampaignProspect.campaign_id
        ).filter(CampaignProspect.prospect_id == prospect.prospect_id):
            if want("CAMPAIGN"):
                items.append({"kind": "CAMPAIGN_ENROLLED", "at": e.enrolled_at,
                              "campaign": {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name,
                                           "status": e.status}})

    if (want("LIST") or want("NOTE")) and not user_id:
        for m, lst in db.query(ProspectListMember, ProspectList).join(
                ProspectList, ProspectList.list_id == ProspectListMember.list_id
        ).filter(ProspectListMember.prospect_id == prospect.prospect_id):
            if want("LIST"):
                items.append({"kind": "ADDED_TO_LIST", "at": m.added_at,
                              "list": {"list_id": lst.list_id, "list_name": lst.list_name}})
            if want("NOTE") and m.notes:
                # Notes written on an upload list before contacts had their own notes
                items.append({"kind": "LIST_NOTE", "at": m.added_at,
                              "list": {"list_id": lst.list_id, "list_name": lst.list_name}, "body": m.notes})

    items.sort(key=lambda i: i["at"] or datetime.min, reverse=True)
    return {"items": items[:limit]}
