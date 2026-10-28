# app/services/contact_query_service.py
"""
Read side of contact management (BRD v2.0, section 5.2): visibility, filtered and paged
lists, board view, facets, the record page, property history, the unified timeline,
duplicate detection, the Recently deleted list, export and global search.

Users with manage_prospects see every contact; everyone else sees their own and their
team's (BR-SH-02). Every read goes through the caller's ``VisibilityScope`` / ``get_contact``.

``ContactQueryService`` serves the routes: one method per endpoint, each in its own unit of work.
The module-level helpers taking ``db`` keep their signatures for other aggregates (accounts, scoring).
SQL lives in app/repositories/contacts.py.
"""

import csv
import io
import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from app.core.clock import utcnow
from app.core.errors import BadRequestError, NotFoundError, PermissionDenied
from app.domain.contacts import (
    ContactCriteria,
    ContactFilter,
    FilterError,
    owner_criteria,
    parse_grades,
    search_terms,
)
from app.domain.paging import Paging, Sort
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask, PropertyChange
from app.models.email_message import EmailEvent
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contacts import (
    ContactRepository,
    ContactsAccess,
    ContactsUnitOfWork,
    Session,
    borrow,
    contacts_uow,
)
from app.repositories.contacts import search_condition as search_condition
from app.services import contact_companies, crm, mentions
from app.services import duplicate_scan as duplicate_scan  # noqa: PLC0414  (explicit submodule import for mypy)
from app.services.contact_field_service import definitions_in_order, label_map
from app.services.export_log import record_export
from app.services.contact_service import (
    MERGE_CHOOSABLE,
    can_access_contact,
    can_manage_contacts,
    phone_digits,
    require_manager,
    visibility_scope,
    visible_user_ids,
)

__all__ = ["ContactFilters", "ContactQueryService", "parse_grades", "search_condition"]

# The filters shared by the list, board and export views (app/domain/contacts.py)
ContactFilters = ContactFilter

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

TOUCH_KINDS = ("CALL", "EMAIL", "MEETING")  # logged activities that count as contacting the person


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


# ── Visibility and criteria ────────────────────────────────────────

def find_contact(ctx: ContactsAccess, user: User, prospect_id: str) -> Prospect:
    """A live contact the user may see, else NotFoundError (no leak of other teams' records)."""
    prospect = ctx.contacts.get_live(user.tenant_id, prospect_id)
    if not prospect or not can_access_contact(user, prospect):
        raise NotFoundError("Contact not found")
    return prospect


def get_contact(db: Session, user: User, prospect_id: str) -> Prospect:
    """``find_contact`` on a caller's session."""
    return find_contact(borrow(db), user, prospect_id)


def contact_criteria(ctx: ContactsAccess, user: User, f: ContactFilter) -> ContactCriteria:
    """Check and resolve the request's filters for this user (404 unknown list, 400 bad grade / filter)."""
    owner_id, unassigned = owner_criteria(f.owner, user.user_id)
    in_list = None
    if f.list_id:
        plist = ctx.contacts.list_in_tenant(user.tenant_id, f.list_id)
        if not plist:
            raise NotFoundError("List not found")
        in_list = crm.list_scope(ctx.session, plist, user)
    grades = None
    if f.grade:
        grades = tuple(parse_grades(f.grade))
        if not grades:
            raise BadRequestError("grade must be A, B, C or D (comma-separated)")
    tree = None
    if f.filters:
        try:
            tree = crm.parse_filter(ctx.session, crm.parse_filter_param(f.filters), user)
        except FilterError as exc:
            raise BadRequestError(str(exc)) from exc
    return ContactCriteria(
        tenant_id=user.tenant_id, visibility=visibility_scope(ctx.session, user), owner_id=owner_id,
        unassigned=unassigned, search=search_terms(f.q) if f.searching and f.q else (), account_id=f.account_id,
        tag=f.tag, in_list=in_list, campaign_id=f.campaign_id, consent_status=f.consent_status,
        email_valid=f.email_valid, has_phone=f.has_phone, country=f.country, industry=f.industry,
        lifecycle_stage=f.lifecycle_stage, lead_status=f.lead_status, grades=grades, score_min=f.score_min,
        score_max=f.score_max, tree=tree)


# ── Serialisation ──────────────────────────────────────────────────

def _bump(store: Dict[str, datetime], pid: str, ts: Optional[datetime]) -> None:
    if ts and (not store.get(pid) or ts > store[pid]):
        store[pid] = ts


def last_touches(repo: ContactRepository, ids: List[str]) -> Tuple[Dict[str, datetime], Dict[str, datetime]]:
    """(last activity, last contacted) per contact, from logged activities and sent emails."""
    last_activity: Dict[str, datetime] = {}
    last_contacted: Dict[str, datetime] = {}
    if not ids:
        return last_activity, last_contacted
    for pid, kind, ts in repo.activity_touches(ids):
        _bump(last_activity, pid, ts)
        if kind in TOUCH_KINDS:
            _bump(last_contacted, pid, ts)
    for pid, ts in repo.outbound_sends(ids):
        _bump(last_activity, pid, ts)
        _bump(last_contacted, pid, ts)
    return last_activity, last_contacted


def contact_summaries(repo: ContactRepository, prospects: Sequence[Prospect]) -> List[dict]:
    """Serialise contacts for tables, batch-loading owners, companies and activity dates."""
    ids = [p.prospect_id for p in prospects]
    owners = repo.users_by_ids({p.owner_id for p in prospects if p.owner_id})
    accounts = repo.live_account_names({p.account_id for p in prospects if p.account_id})
    last_activity, last_contacted = last_touches(repo, ids)
    return [_summary(p, owners, accounts, last_activity, last_contacted) for p in prospects]


def _summary(p: Prospect, owners: Dict[str, User], accounts: Dict[str, Tuple[str, Optional[str]]],
             last_activity: Dict[str, datetime], last_contacted: Dict[str, datetime]) -> dict:
    return {
        "prospect_id": p.prospect_id,
        "row_version": p.row_version,  # send back as expected_version when editing (T-3)
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
    }


def summaries(db: Session, prospects: List[Prospect]) -> List[dict]:
    """``contact_summaries`` on a caller's session (accounts use it for a company's contacts)."""
    return contact_summaries(borrow(db).contacts, prospects)


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


def _account_card(repo: ContactRepository, p: Prospect) -> Optional[dict]:
    found = repo.account_card(p.tenant_id, p.account_id)
    if not found:
        return None
    account, contact_count = found
    return {
        "account_id": account.account_id, "name": account.name, "domain": account.domain,
        "website": account.website, "industry": account.industry, "emp_band": account.emp_band,
        "phone": account.phone, "city": account.city, "country": account.country,
        "lifecycle_stage": account.lifecycle_stage, "contact_count": contact_count,
    }


def _contact_lists(ctx: ContactsAccess, user: User, p: Prospect) -> List[dict]:
    repo = ctx.contacts
    static = [{
        "list_id": lst.list_id, "list_name": lst.list_name, "list_type": "STATIC", "added_at": m.added_at,
        "notes": m.notes,
    } for m, lst in repo.static_memberships(p.prospect_id, newest_first=True)]
    active = []
    for plist in repo.active_lists(p.tenant_id):
        try:
            if repo.in_list(crm.list_scope(ctx.session, plist, user), p.prospect_id):
                active.append({"list_id": plist.list_id, "list_name": plist.list_name, "list_type": "ACTIVE"})
        except FilterError:
            continue  # an active list with a broken filter matches nobody
    return static + active


def _contact_stats(repo: ContactRepository, p: Prospect) -> dict:
    s = repo.stats(p.prospect_id)
    return {
        "emails_sent": s.emails_sent, "last_emailed_at": s.last_emailed_at,
        "replies": s.replies, "last_reply_at": s.last_reply_at,
        "emails_opened": s.emails_opened,
        "notes": s.activity_counts.get("NOTE", 0),
        "calls": s.activity_counts.get("CALL", 0),
        "meetings": s.activity_counts.get("MEETING", 0),
        "logged_emails": s.activity_counts.get("EMAIL", 0),
        "open_tasks": s.open_tasks,
    }


def detail_of(ctx: ContactsAccess, user: User, p: Prospect) -> dict:
    """The record page: summary plus company, lists, campaigns, persona and engagement stats."""
    repo = ctx.contacts
    detail = contact_summaries(repo, [p])[0]
    detail.update({
        "email_type": p.email_type,
        "email_provider": p.email_provider,
        "consent_source": p.consent_source,
        "consent_timestamp": p.consent_timestamp,
    })
    detail["account"] = _account_card(repo, p)
    detail["companies"] = contact_companies.links(repo, p)
    detail["lists"] = _contact_lists(ctx, user, p)
    detail["campaigns"] = [{
        "campaign_id": c.campaign_id, "campaign_name": c.campaign_name, "campaign_status": c.status,
        "status": e.status, "current_step": e.current_step, "stopped_reason": e.stopped_reason,
        "enrolled_at": e.enrolled_at,
    } for e, c in repo.enrollments(p.prospect_id, newest_first=True)]
    persona = repo.persona(p.prospect_id)
    detail["persona"] = {"persona_type": persona.persona_type, "confidence_score": persona.confidence_score} if persona else None
    detail["stats"] = _contact_stats(repo, p)
    detail["can_edit_owner"] = can_manage_contacts(user)
    detail["can_delete"] = can_manage_contacts(user) and user.role != "AGENT"
    detail["can_override_lifecycle"] = crm.can_override_lifecycle(user)
    return detail


def contact_detail(db: Session, user: User, p: Prospect) -> dict:
    """``detail_of`` on a caller's session."""
    return detail_of(borrow(db), user, p)


def companies_of(repo: ContactRepository, prospect: Prospect) -> dict:
    return {"prospect_id": prospect.prospect_id, "items": contact_companies.links(repo, prospect)}


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


def tag_counts(tag_lists: Iterable[Optional[Sequence[str]]]) -> List[dict]:
    """Tags counted case-insensitively (first spelling kept), most used first then by name."""
    counts: Dict[str, list] = {}
    for tags in tag_lists:
        for tag in tags or []:
            entry = counts.setdefault(tag.lower(), [tag, 0])
            entry[1] += 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.values(), key=lambda x: (-x[1], x[0].lower()))]


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


def export_summary(f: ContactFilter, keys: Sequence[str], list_name: Optional[str] = None) -> str:
    """What was exported, for the export log (C-07): the filters that were set and the columns."""
    parts = [f"list={list_name}" if name == "list_id" and list_name else f"{name}={value}"
             for name, value in ((fld.name, getattr(f, fld.name)) for fld in dataclass_fields(f))
             if value not in (None, "")]
    return "; ".join(parts or ["all contacts"]) + f" | columns: {', '.join(keys)}"


def check_export_size(total: int) -> None:
    """Refuse an export larger than EXPORT_ROW_LIMIT instead of silently cutting it (C-07)."""
    if total > EXPORT_ROW_LIMIT:
        raise BadRequestError(
            f"This view has {total:,} contacts, more than the {EXPORT_ROW_LIMIT:,} an export can hold. "
            "Narrow it with a filter (owner, list, lifecycle stage, ...) and export it in parts.")


def export_contacts(ctx: ContactsAccess, user: User, fmt: str, columns: Optional[str], f: ContactFilter,
                    object_type: str = "CONTACT", list_name: Optional[str] = None) -> ExportFile:
    """A view as CSV / XLSX. Admins and managers only. Logged in export_logs (C-07); the caller commits."""
    if not can_export(user):
        raise PermissionDenied("Only admins and managers can export contacts")
    keys = export_columns(columns)
    repo = ctx.contacts
    field_defs = definitions_in_order(repo, user.tenant_id)
    criteria = contact_criteria(ctx, user, f)
    check_export_size(repo.search(criteria, Sort("created_at"), Paging(1, 1)).total)

    header = [COLUMNS[k] for k in keys] + [d.label for d in field_defs]
    rows = []
    for offset in range(0, EXPORT_ROW_LIMIT, EXPORT_CHUNK):
        chunk = repo.export_chunk(criteria, offset, EXPORT_CHUNK)
        if not chunk:
            break
        for s in contact_summaries(repo, chunk):
            values = [export_cell(k, s.get(k)) for k in keys]
            for d in field_defs:
                v = s["custom_fields"].get(d.field_key)
                values.append("; ".join(v) if isinstance(v, list) else v)
            rows.append(crm.csv_safe_row(values))  # no formula injection in Excel / Sheets
    record_export(ctx, user.tenant_id, user.user_id, object_type, export_summary(f, keys, list_name), len(rows), fmt)

    stamp = utcnow().strftime("%Y%m%d-%H%M")
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


def mentionable_users(ctx: ContactsAccess, user: User, q: Optional[str], limit: int,
                      prospect_id: Optional[str], opportunity_id: Optional[str]) -> List[dict]:
    """Colleagues who can be @mentioned (BR-CM-16); scoped to who can see the record when given."""
    db = ctx.session
    if prospect_id:
        prospect = find_contact(ctx, user, prospect_id)
        return mentions.mentionable_users(db, user, q, limit, prospect.owner_id, True)
    if opportunity_id:
        from app.services import sales as sales_svc
        opp = sales_svc.get_opp(db, user, opportunity_id)
        return mentions.mentionable_users(db, user, q, limit, opp.owner_id, True)
    return mentions.mentionable_users(db, user, q, limit)


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


def history_rows(repo: ContactRepository, user: User, rows: List[PropertyChange]) -> List[dict]:
    defs = label_map(repo, user.tenant_id)
    user_ids = {r.changed_by for r in rows} | {r.old_value for r in rows if r.field == "owner_id"} | \
               {r.new_value for r in rows if r.field == "owner_id"}
    users = {uid: user_name(u) for uid, u in repo.users_by_ids({i for i in user_ids if i}).items()}
    company_ids = {v for r in rows if r.field in ("account_id", "company_association")
                   for v in (r.old_value, r.new_value) if v}
    companies = repo.account_names(company_ids)
    return [{
        "change_id": r.change_id, "field": r.field, "label": history_label(r.field, defs),
        "old_value": history_value(r.field, r.old_value, users, companies),
        "new_value": history_value(r.field, r.new_value, users, companies),
        "source": r.source, "changed_by": r.changed_by, "changed_by_name": users.get(r.changed_by),
        "changed_at": r.changed_at,
    } for r in rows]


def history_items(db: Session, user: User, rows: List[PropertyChange]) -> List[dict]:
    """``history_rows`` on a caller's session (accounts show company history the same way)."""
    return history_rows(borrow(db).contacts, user, rows)


# ── Timeline (BR-CM-14) ────────────────────────────────────────────

def parse_timeline_types(types: Optional[str]) -> Optional[Set[str]]:
    """'note, call' -> {'NOTE', 'CALL'}; None means every kind."""
    return {t.strip().upper() for t in types.split(",")} if types else None


def _email_items(repo: ContactRepository, prospect: Prospect, limit: int) -> List[dict]:
    items = []
    for m, campaign_name in repo.emails_of(prospect.prospect_id, limit):
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
    opened = set()
    for e, subject in repo.email_events_of(prospect.prospect_id, list(EVENT_LABELS), limit):
        # One "opened" entry per email (the first open), not one per pixel load
        if e.event_type == EmailEvent.EVENT_OPEN:
            if e.message_id in opened:
                continue
            opened.add(e.message_id)
        items.append({"kind": EVENT_LABELS[e.event_type], "at": e.event_time,
                      "email": {"message_id": e.message_id, "subject": subject}})
    return items


TaskSerializer = Callable[[Session, CrmTask], dict]


class _Timeline:
    """One timeline request: the contact, the kinds wanted and the items collected."""

    def __init__(self, ctx: ContactsAccess, user: User, prospect: Prospect, types: Optional[str],
                 user_id: Optional[str], limit: int) -> None:
        self.ctx, self.repo, self.user, self.prospect = ctx, ctx.contacts, user, prospect
        self.wanted, self.user_id, self.limit = parse_timeline_types(types), user_id, limit
        self.items: List[dict] = []

    def want(self, kind: str) -> bool:
        return self.wanted is None or kind in self.wanted

    def activities(self) -> None:
        user, pid = self.user, self.prospect.prospect_id
        activities = self.repo.activities_of(pid, self.user_id, self.limit)
        authors = self.repo.users_by_ids({a.created_by for a in activities if a.created_by})
        mentioned = mentions.for_activities(self.ctx.session,
                                            [a.activity_id for a in activities if a.activity_type == "NOTE"])
        for a in activities:
            kind = "EMAIL_LOGGED" if a.activity_type == "EMAIL" else a.activity_type
            if self.want(a.activity_type):
                self.items.append({"kind": kind, "at": a.occurred_at,
                                   "activity": activity_dict(a, authors, mentioned.get(a.activity_id)),
                                   "user_id": a.created_by,
                                   "can_edit": a.created_by == user.user_id or can_manage_contacts(user)})

    def tasks(self, task_serializer: TaskSerializer) -> None:
        for t in self.repo.tasks_of(self.prospect.prospect_id, self.user_id, self.limit):
            self.items.append({"kind": "TASK", "at": t.completed_at or t.created_at,
                               "task": task_serializer(self.ctx.session, t), "user_id": t.created_by})

    def changes(self) -> None:
        rows = self.repo.changes(self.prospect.prospect_id, None, self.user_id, self.limit)
        for c in history_rows(self.repo, self.user, rows):
            self.items.append({"kind": "PROPERTY_CHANGE", "at": c["changed_at"], "change": c,
                               "user_id": c["changed_by"]})

    def campaigns(self) -> None:
        for e, c in self.repo.enrollments(self.prospect.prospect_id, newest_first=False):
            if self.want("CAMPAIGN"):
                self.items.append({"kind": "CAMPAIGN_ENROLLED", "at": e.enrolled_at,
                                   "campaign": {"campaign_id": c.campaign_id, "campaign_name": c.campaign_name,
                                                "status": e.status}})

    def lists(self) -> None:
        for m, lst in self.repo.static_memberships(self.prospect.prospect_id, newest_first=False):
            if self.want("LIST"):
                self.items.append({"kind": "ADDED_TO_LIST", "at": m.added_at,
                                   "list": {"list_id": lst.list_id, "list_name": lst.list_name}})
            if self.want("NOTE") and m.notes:
                # Notes written on an upload list before contacts had their own notes
                self.items.append({"kind": "LIST_NOTE", "at": m.added_at,
                                   "list": {"list_id": lst.list_id, "list_name": lst.list_name}, "body": m.notes})

    def build(self, task_serializer: TaskSerializer) -> dict:
        self.activities()
        if self.want("TASK"):
            self.tasks(task_serializer)
        if self.want("PROPERTY"):
            self.changes()
        everyone = not self.user_id
        if self.want("EMAIL") and everyone:
            self.items.extend(_email_items(self.repo, self.prospect, self.limit))
        if (self.want("CAMPAIGN") or self.want("EMAIL")) and everyone:
            self.campaigns()
        if (self.want("LIST") or self.want("NOTE")) and everyone:
            self.lists()
        self.items.sort(key=lambda i: i["at"] or datetime.min, reverse=True)
        return {"items": self.items[:self.limit]}


# ── The service the routes call ────────────────────────────────────

class ContactQueryService:
    """Contact reads for the API; each method is one unit of work (nothing is written)."""

    def __init__(self, uow: ContactsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "ContactQueryService":
        return cls(contacts_uow(db))

    def meta(self, user: User) -> dict:
        return contact_meta(user)

    def list_page(self, user: User, f: ContactFilter, sort: Sort, paging: Paging) -> dict:
        with self.uow as uow:
            page = uow.contacts.search(contact_criteria(uow, user, f), sort, paging)
            return {"items": contact_summaries(uow.contacts, page.items), "total": page.total,
                    "page": paging.page, "page_size": paging.page_size}

    def board(self, user: User, group_by: str, per_column: int, f: ContactFilter) -> dict:
        """Board view: contacts grouped by lifecycle stage or lead status (BR-CM-20)."""
        values = crm.LIFECYCLE_STAGES if group_by == "lifecycle_stage" else crm.LEAD_STATUSES
        with self.uow as uow:
            repo = uow.contacts
            criteria = contact_criteria(uow, user, f)
            counts = repo.count_by(criteria, group_by)
            lanes, all_rows = [], []
            for value, label in list(values.items()) + [(None, "Not set")]:
                rows = repo.lane(criteria, group_by, value, per_column) if counts.get(value) else []
                if value is None and not rows:
                    continue
                lanes.append((value, label, rows))
                all_rows.extend(rows)
            by_id = {s["prospect_id"]: s for s in contact_summaries(repo, all_rows)}
        return {"group_by": group_by, "lanes": [
            {"value": v, "label": lbl, "total": counts.get(v, 0), "items": [by_id[r.prospect_id] for r in rows]}
            for v, lbl, rows in lanes]}

    def facets(self, user: User) -> dict:
        """Values for the filter dropdowns: tags, countries, industries, lists and campaigns."""
        with self.uow as uow:
            repo, scope = uow.contacts, visibility_scope(uow.session, user)
            return {
                "tags": tag_counts(repo.tag_lists(user.tenant_id, scope)),
                "countries": repo.distinct_values(user.tenant_id, scope, "poc_country"),
                "industries": repo.distinct_values(user.tenant_id, scope, "industry"),
                "lead_sources": repo.distinct_values(user.tenant_id, scope, "lead_source"),
                "lists": [{"list_id": i, "list_name": n, "list_type": t} for i, n, t in repo.list_rows(user.tenant_id)],
                "campaigns": [{"campaign_id": i, "campaign_name": n, "status": s}
                              for i, n, s in repo.campaign_rows(user.tenant_id)],
            }

    def owners(self, user: User) -> List[dict]:
        """Active users in the workspace, for owner pickers (hierarchy users: their own team)."""
        with self.uow as uow:
            users = uow.contacts.active_users(user.tenant_id)
            visible = visible_user_ids(uow.session, user)
            if visible is not None:  # BR-SH-02
                users = [u for u in users if u.user_id in visible]
            return [{"user_id": u.user_id, "name": user_name(u), "email": u.email, "role": u.role} for u in users]

    def mentionable_users(self, user: User, q: Optional[str], limit: int, prospect_id: Optional[str],
                          opportunity_id: Optional[str]) -> List[dict]:
        with self.uow as uow:
            return mentionable_users(uow, user, q, limit, prospect_id, opportunity_id)

    def export(self, user: User, fmt: str, columns: Optional[str], f: ContactFilter) -> ExportFile:
        with self.uow as uow:
            export = export_contacts(uow, user, fmt, columns, f)
            uow.commit()  # the export-log row (C-07)
            return export

    def deleted_page(self, user: User, q: Optional[str], paging: Paging) -> dict:
        """Recently deleted (BR-CM-35): the user's team's contacts still inside the restore window."""
        require_manager(user)
        with self.uow as uow:
            repo = uow.contacts
            cutoff = utcnow() - timedelta(days=crm.RESTORE_WINDOW_DAYS)
            page = repo.deleted_page(user.tenant_id, visibility_scope(uow.session, user), cutoff, q, paging)
            deleters = repo.users_by_ids({p.deleted_by for p in page.items if p.deleted_by})
            items = []
            for s, p in zip(contact_summaries(repo, page.items), page.items, strict=True):
                s.update(deleted_at=p.deleted_at, deleted_by_name=user_name(deleters.get(p.deleted_by)),
                         purge_at=p.deleted_at + timedelta(days=crm.RESTORE_WINDOW_DAYS))
                items.append(s)
            return {"items": items, "total": page.total, "page": paging.page, "page_size": paging.page_size}

    def duplicates(self, user: User, limit: int) -> dict:
        """Stored OPEN duplicate pairs (C-08), strongest first, each as a group of two contacts with its
        reasons, score and confidence (the shape the Find duplicates modal reads). A workspace that was
        never scanned is scanned first."""
        require_manager(user)
        with self.uow as uow:
            repo, store = uow.contacts, uow.contacts.duplicates
            if not store.has_pairs(user.tenant_id):
                duplicate_scan.scan_tenant(store, user.tenant_id, utcnow())
                uow.commit()
            page = store.pair_page(user.tenant_id, visibility_scope(uow.session, user), "OPEN", None, Paging(1, limit))
            ids = {i for pair in page.items for i in (pair.contact_a_id, pair.contact_b_id)}
            by_summary = {s["prospect_id"]: s for s in contact_summaries(repo, repo.prospects_by_ids(user.tenant_id, ids))}
            groups = [duplicate_scan.pair_group(pair, by_summary) for pair in page.items]
        return {"groups": groups, "total_groups": page.total, "mergeable_properties": list(MERGE_CHOOSABLE)}

    def detail(self, user: User, prospect_id: str) -> dict:
        with self.uow as uow:
            return detail_of(uow, user, find_contact(uow, user, prospect_id))

    def companies(self, user: User, prospect_id: str) -> dict:
        """Every company the contact is associated with, with its label; the primary one first."""
        with self.uow as uow:
            return companies_of(uow.contacts, find_contact(uow, user, prospect_id))

    def property_history(self, user: User, prospect_id: str, field: Optional[str]) -> dict:
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            return {"items": history_rows(uow.contacts, user,
                                          uow.contacts.changes(prospect.prospect_id, field, None, 1000))}

    def timeline(self, user: User, prospect_id: str, types: Optional[str], user_id: Optional[str], limit: int,
                 task_serializer: TaskSerializer) -> dict:
        """Everything that happened with this contact, newest first. ``task_serializer`` renders a
        task the way the tasks API does."""
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            return _Timeline(uow, user, prospect, types, user_id, limit).build(task_serializer)

    def global_search(self, user: User, q: str, limit: int) -> dict:
        """Contacts by name, email, phone, company or domain, and companies by name or domain (BR-CM-21)."""
        with self.uow as uow:
            repo, scope = uow.contacts, visibility_scope(uow.session, user)
            contact_rows = repo.search_contacts(user.tenant_id, scope, q, limit)
            company_rows = repo.search_companies(user.tenant_id, scope, q.strip(), max(3, limit // 2))
            return {
                "contacts": [{"prospect_id": p.prospect_id, "full_name": p.full_name, "email": p.email,
                              "phone": p.phone, "company_name": p.company_name,
                              "lifecycle_stage": p.lifecycle_stage} for p in contact_rows],
                "companies": [{"account_id": a.account_id, "name": a.name, "domain": a.domain,
                               "industry": a.industry} for a in company_rows],
            }
