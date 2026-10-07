# app/services/crm.py
"""
Shared Contact Management logic for Phase 1 (BRD v2.0, section 5.2):

- lifecycle stage / lead status / legal basis definitions (BR-CM-06/07/08/40)
- property change history (BR-CM-11)
- company resolution from the email domain, falling back to the company name for
  personal email providers (BR-CM-03/04)
- soft delete, restore within 90 days and purge (BR-CM-35)
- the AND/OR filter engine used by views, active lists and exports (BR-CM-18/22)
- data-quality flags (BR-CM-34)
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import Numeric, and_, cast, func, not_, or_
from sqlalchemy.orm import Session

from app.models.account import Account
from app.models.campaign import CampaignProspect
from app.models.crm import PropertyChange
from app.models.email_message import EmailMessage
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User

# ── Definitions (labels can be renamed here before go-live; codes are stored) ──

LIFECYCLE_STAGES = {
    "SUBSCRIBER": "Subscriber",
    "LEAD": "Lead",
    "MQL": "Marketing qualified lead",
    "SQL": "Sales qualified lead",
    "OPPORTUNITY": "Opportunity",
    "CUSTOMER": "Customer",
    "EVANGELIST": "Evangelist",
    "OTHER": "Other",
}
_LIFECYCLE_ORDER = [k for k in LIFECYCLE_STAGES if k != "OTHER"]

LEAD_STATUSES = {
    "NEW": "New",
    "OPEN": "Open",
    "IN_PROGRESS": "In progress",
    "ATTEMPTED_TO_CONTACT": "Attempted to contact",
    "CONNECTED": "Connected",
    "OPEN_DEAL": "Open deal",
    "BAD_TIMING": "Bad timing",
    "UNQUALIFIED": "Unqualified",
}

LEGAL_BASES = {
    "CONSENT": "Consent",
    "LEGITIMATE_INTEREST": "Legitimate interest",
    "CONTRACT": "Performance of a contract",
    "LEGAL_OBLIGATION": "Legal obligation",
    "NOT_APPLICABLE": "Not applicable",
}

RESTORE_WINDOW_DAYS = 90

# Personal email providers, matched on the provider label so regional variants
# (yahoo.co.in, outlook.fr, ...) count too. No company is created from these domains.
_FREEMAIL_LABELS = {
    "gmail", "googlemail", "yahoo", "ymail", "rocketmail", "hotmail", "outlook", "live", "msn",
    "aol", "icloud", "me", "mac", "protonmail", "proton", "pm", "zoho", "zohomail", "yandex",
    "mail", "gmx", "web", "rediffmail", "rediff", "qq", "163", "126", "sina", "naver",
    "hanmail", "tutanota", "fastmail", "hushmail", "inbox", "lycos", "comcast", "verizon",
}


def can_override_lifecycle(user: User) -> bool:
    """Admins may move a lifecycle stage backwards (BR-CM-08)."""
    return user.role in ("SUPER_ADMIN", "ADMIN")


def lifecycle_move_allowed(old: Optional[str], new: Optional[str], user: User) -> bool:
    if not old or not new or old == new or "OTHER" in (old, new) or can_override_lifecycle(user):
        return True
    return _LIFECYCLE_ORDER.index(new) >= _LIFECYCLE_ORDER.index(old)


# ── Property history ────────────────────────────────────────────

def _fmt(value):
    if value is None or value == "" or value == [] or value == {}:
        return None
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def snapshot(obj, fields: Iterable[str]) -> dict:
    return {f: getattr(obj, f) for f in fields}


def record_changes(db: Session, tenant_id: str, object_type: str, object_id: str,
                   before: dict, after: dict, user_id: Optional[str], source: str = "UI") -> int:
    """Write one history row per changed field. custom_fields / tags are diffed per key."""
    rows = 0
    changed = {}
    for field, old in before.items():
        new = after.get(field)
        if field == "custom_fields":
            old, new = old or {}, new or {}
            for key in sorted(set(old) | set(new)):
                if _fmt(old.get(key)) != _fmt(new.get(key)):
                    db.add(PropertyChange(tenant_id=tenant_id, object_type=object_type, object_id=object_id,
                                          field=f"custom.{key}", old_value=_fmt(old.get(key)),
                                          new_value=_fmt(new.get(key)), source=source, changed_by=user_id))
                    rows += 1
            continue
        if _fmt(old) != _fmt(new):
            db.add(PropertyChange(tenant_id=tenant_id, object_type=object_type, object_id=object_id,
                                  field=field, old_value=_fmt(old), new_value=_fmt(new),
                                  source=source, changed_by=user_id))
            changed[field] = (old, new)
            rows += 1
    # Workflow rules react to the same changes (BR-SF-13); imports and merges are excluded
    if changed and source not in ("IMPORT", "MERGE"):
        from app.services import workflow
        workflow.on_changes(db, tenant_id, object_type, object_id, changed, user_id)
    return rows


# ── Companies ───────────────────────────────────────────────────

def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def is_personal_domain(domain: Optional[str]) -> bool:
    if not domain:
        return True
    return domain.split(".")[0] in _FREEMAIL_LABELS


def clean_domain(value: Optional[str]) -> Optional[str]:
    """'https://www.Acme.com/about' -> 'acme.com'."""
    if not value:
        return None
    value = value.strip().lower()
    value = re.sub(r"^[a-z]+://", "", value).split("/")[0].split("?")[0]
    value = value[4:] if value.startswith("www.") else value
    return value or None


def _name_from_domain(domain: str) -> str:
    label = domain.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part) or domain


def resolve_company(db: Session, tenant_id: str, email: Optional[str] = None,
                    company_name: Optional[str] = None, create: bool = True, **defaults) -> Optional[Account]:
    """
    The company a contact belongs to (BR-CM-04): matched by the email's domain, created
    from it when new. Personal email providers fall back to the company name.
    """
    domain = email_domain(email)
    name = (company_name or "").strip()[:255] or None
    live = db.query(Account).filter(Account.tenant_id == tenant_id, Account.deleted_at.is_(None))

    if domain and not is_personal_domain(domain):
        account = live.filter(Account.domain == domain).first()
        if account:
            return account
        if not create:
            return None
        # A domain-less account with the same name is the same company: give it the domain
        if name:
            same_name = live.filter(Account.name == name, Account.domain.is_(None)).first()
            if same_name:
                same_name.domain = domain
                return same_name
        new_name = name or _name_from_domain(domain)
        if db.query(Account.account_id).filter(Account.tenant_id == tenant_id, Account.name == new_name).first():
            new_name = f"{new_name} ({domain})"[:255]
        account = Account(tenant_id=tenant_id, name=new_name, domain=domain,
                          website=f"https://{domain}", lifecycle_stage="LEAD",
                          **{k: v for k, v in defaults.items() if v})
        db.add(account)
        db.flush()
        return account

    if not name:
        return None
    account = live.filter(Account.name == name).first()
    if account or not create:
        return account
    account = Account(tenant_id=tenant_id, name=name, lifecycle_stage="LEAD",
                      **{k: v for k, v in defaults.items() if v})
    db.add(account)
    db.flush()
    return account


# ── Soft delete / restore ───────────────────────────────────────

def soft_delete_contacts(db: Session, prospects: list, user_id: Optional[str]) -> int:
    """Hide contacts for 90 days and stop anything still queued to them."""
    now = datetime.utcnow()
    ids = [p.prospect_id for p in prospects if p.deleted_at is None]
    if not ids:
        return 0
    db.query(Prospect).filter(Prospect.prospect_id.in_(ids)).update(
        {Prospect.deleted_at: now, Prospect.deleted_by: user_id}, synchronize_session=False)
    db.query(EmailMessage).filter(
        EmailMessage.prospect_id.in_(ids), EmailMessage.status.in_(["QUEUED", "SCHEDULED", "PAUSED_BY_CAMPAIGN"])
    ).update({EmailMessage.status: "CANCELLED", EmailMessage.failure_reason: "Contact deleted"},
             synchronize_session=False)
    db.query(CampaignProspect).filter(
        CampaignProspect.prospect_id.in_(ids), CampaignProspect.status.in_(["ACTIVE", "OPENED", "QUEUED"])
    ).update({CampaignProspect.status: "PAUSED", CampaignProspect.stopped_reason: "Contact deleted"},
             synchronize_session=False)
    for pid in ids:
        db.add(PropertyChange(tenant_id=prospects[0].tenant_id, object_type="CONTACT", object_id=pid,
                              field="deleted", old_value=None, new_value="deleted", source="UI", changed_by=user_id))
    return len(ids)


def restore_contacts(db: Session, tenant_id: str, ids: list, user_id: Optional[str]) -> int:
    cutoff = datetime.utcnow() - timedelta(days=RESTORE_WINDOW_DAYS)
    rows = db.query(Prospect).filter(
        Prospect.tenant_id == tenant_id, Prospect.prospect_id.in_(ids),
        Prospect.deleted_at.isnot(None), Prospect.deleted_at > cutoff, Prospect.merged_into_id.is_(None),
    ).all()
    for p in rows:
        p.deleted_at, p.deleted_by = None, None
        db.add(PropertyChange(tenant_id=tenant_id, object_type="CONTACT", object_id=p.prospect_id,
                              field="deleted", old_value="deleted", new_value=None, source="UI", changed_by=user_id))
    return len(rows)


def purge_expired(db: Session) -> int:
    """Permanently delete contacts and companies deleted more than 90 days ago."""
    from app.services.contact_service import delete_contacts
    cutoff = datetime.utcnow() - timedelta(days=RESTORE_WINDOW_DAYS)
    ids = [r[0] for r in db.query(Prospect.prospect_id).filter(
        Prospect.deleted_at.isnot(None), Prospect.deleted_at <= cutoff).limit(5000)]
    delete_contacts(db, ids)
    accounts = db.query(Account).filter(Account.deleted_at.isnot(None), Account.deleted_at <= cutoff).all()
    for account in accounts:
        db.query(Prospect).filter(Prospect.account_id == account.account_id).update(
            {Prospect.account_id: None}, synchronize_session=False)
        from app.models.crm import CrmTask
        db.query(CrmTask).filter(CrmTask.account_id == account.account_id).delete(synchronize_session=False)
        db.delete(account)
    db.commit()
    return len(ids) + len(accounts)


# ── Filters (AND / OR) ──────────────────────────────────────────
#
# A filter is either a condition {"field", "operator", "value"} or a group
# {"op": "AND"|"OR", "conditions": [...]} — groups nest. Fields are contact properties,
# "custom.<key>" for custom properties, plus "tags", "list", "company.<prop>".

CONTACT_FIELDS = {
    "first_name": Prospect.first_name, "last_name": Prospect.last_name, "email": Prospect.email,
    "phone": Prospect.phone, "mobile_phone": Prospect.mobile_phone, "designation": Prospect.designation,
    "company_name": Prospect.company_name, "industry": Prospect.industry, "emp_band": Prospect.emp_band,
    "linkedin_url": Prospect.linkedin_url, "poc_city": Prospect.poc_city, "poc_state": Prospect.poc_state,
    "poc_country": Prospect.poc_country, "timezone": Prospect.timezone, "lifecycle_stage": Prospect.lifecycle_stage,
    "lead_status": Prospect.lead_status, "lead_source": Prospect.lead_source, "legal_basis": Prospect.legal_basis,
    "owner_id": Prospect.owner_id, "account_id": Prospect.account_id, "consent_status": Prospect.consent_status,
    "is_valid_email": Prospect.is_valid_email, "email_type": Prospect.email_type,
    "created_at": Prospect.created_at, "updated_at": Prospect.updated_at,
}
COMPANY_FIELDS = {
    "name": Account.name, "domain": Account.domain, "industry": Account.industry, "emp_band": Account.emp_band,
    "country": Account.country, "city": Account.city, "lifecycle_stage": Account.lifecycle_stage,
    "annual_revenue": Account.annual_revenue, "owner_id": Account.owner_id,
}
OPERATORS = {"is", "is_not", "in", "not_in", "contains", "not_contains", "starts_with", "is_empty",
             "is_not_empty", "gt", "gte", "lt", "lte", "between", "before", "after", "in_last_days"}

MAX_FILTER_DEPTH = 4
MAX_CONDITIONS = 50


class FilterError(ValueError):
    pass


def _as_list(value):
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    return [value]


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise FilterError(f"Invalid date: {value}")


def _compare(column, operator, value, is_text=True):
    if operator == "is":
        return func.lower(column) == str(value).lower() if is_text and isinstance(value, str) else column == value
    if operator == "is_not":
        return or_(column.is_(None), (func.lower(column) != str(value).lower()) if is_text and isinstance(value, str) else column != value)
    if operator == "in":
        return column.in_(_as_list(value))
    if operator == "not_in":
        return or_(column.is_(None), column.notin_(_as_list(value)))
    if operator == "contains":
        return column.ilike(f"%{value}%")
    if operator == "not_contains":
        return or_(column.is_(None), not_(column.ilike(f"%{value}%")))
    if operator == "starts_with":
        return column.ilike(f"{value}%")
    if operator == "is_empty":
        return or_(column.is_(None), column == "") if is_text else column.is_(None)
    if operator == "is_not_empty":
        return and_(column.isnot(None), column != "") if is_text else column.isnot(None)
    if operator in ("gt", "after"):
        return column > (_parse_dt(value) if operator == "after" and is_text is False else value)
    if operator in ("gte",):
        return column >= value
    if operator in ("lt", "before"):
        return column < (_parse_dt(value) if operator == "before" and is_text is False else value)
    if operator == "lte":
        return column <= value
    if operator == "between":
        low, high = (_as_list(value) + [None, None])[:2]
        return column.between(low, high)
    if operator == "in_last_days":
        return column >= datetime.utcnow() - timedelta(days=int(value))
    raise FilterError(f"Unknown operator: {operator}")


def _condition(db: Session, cond: dict, user: User):
    field, operator, value = cond.get("field"), cond.get("operator"), cond.get("value")
    if operator not in OPERATORS:
        raise FilterError(f"Unknown operator: {operator}")
    if value == "me":
        value = user.user_id
    if field == "tags":
        has = lambda tag: func.json_contains(Prospect.tags, func.json_quote(tag)) == 1
        tags = _as_list(value)
        if operator in ("is", "in", "contains"):
            return or_(*[has(t) for t in tags]) if tags else Prospect.tags.isnot(None)
        if operator in ("is_not", "not_in", "not_contains"):
            return or_(Prospect.tags.is_(None), not_(or_(*[has(t) for t in tags])))
        if operator == "is_empty":
            return or_(Prospect.tags.is_(None), func.json_length(Prospect.tags) == 0)
        if operator == "is_not_empty":
            return func.json_length(Prospect.tags) > 0
        raise FilterError("Tags support is / is_not / is_empty / is_not_empty")
    if field == "list":
        members = db.query(ProspectListMember.prospect_id).filter(ProspectListMember.list_id.in_(_as_list(value)))
        inside = Prospect.prospect_id.in_(members)
        return not_(inside) if operator in ("is_not", "not_in") else inside
    if field and field.startswith("custom."):
        key = field[len("custom."):]
        if not re.fullmatch(r"[a-z0-9_]{1,64}", key):
            raise FilterError(f"Invalid custom field: {key}")
        raw = func.json_unquote(func.json_extract(Prospect.custom_fields, f'$."{key}"'))
        if operator in ("contains", "is") and isinstance(value, str):  # multi-checkbox values are arrays
            arr = func.json_contains(func.json_extract(Prospect.custom_fields, f'$."{key}"'), func.json_quote(value)) == 1
            return or_(_compare(raw, operator, value), arr) if operator == "is" else or_(raw.ilike(f"%{value}%"), arr)
        is_date = isinstance(value, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value))
        if operator in ("gt", "gte", "lt", "lte", "between") and not is_date:
            raw = cast(raw, Numeric(18, 4))  # number properties compare numerically, dates as ISO text
        return _compare(raw, operator, value)
    if field and field.startswith("company."):
        column = COMPANY_FIELDS.get(field[len("company."):])
        if column is None:
            raise FilterError(f"Unknown company property: {field}")
        is_text = field != "company.annual_revenue"
        companies = db.query(Account.account_id).filter(_compare(column, operator, value, is_text=is_text))
        return Prospect.account_id.in_(companies)
    column = CONTACT_FIELDS.get(field)
    if column is None:
        raise FilterError(f"Unknown property: {field}")
    is_text = field not in ("is_valid_email", "created_at", "updated_at")
    if field == "is_valid_email" and isinstance(value, str):
        value = value.lower() in ("true", "1", "yes")
    return _compare(column, operator, value, is_text=is_text)


def filter_clause(db: Session, spec, user: User, depth: int = 0, counter=None):
    """SQL condition for a filter spec (see above); None/empty means 'everything'."""
    counter = counter if counter is not None else [0]
    if not spec:
        return None
    if depth > MAX_FILTER_DEPTH:
        raise FilterError("Filters are nested too deeply")
    if "conditions" in spec:
        parts = [c for c in (filter_clause(db, s, user, depth + 1, counter) for s in spec.get("conditions") or [])
                 if c is not None]
        if not parts:
            return None
        return or_(*parts) if str(spec.get("op", "AND")).upper() == "OR" else and_(*parts)
    counter[0] += 1
    if counter[0] > MAX_CONDITIONS:
        raise FilterError(f"At most {MAX_CONDITIONS} conditions")
    return _condition(db, spec, user)


def parse_filter_param(raw: Optional[str]):
    if not raw:
        return None
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError:
        raise FilterError("filters must be JSON")
    if not isinstance(spec, dict):
        raise FilterError("filters must be an object")
    return spec


# ── Lists ───────────────────────────────────────────────────────

def list_member_ids(db: Session, plist: ProspectList, user: User):
    """Subquery of contact ids in a list. Active lists are evaluated live from their filters."""
    if plist.list_type == "ACTIVE":
        clause = filter_clause(db, plist.filters, user)
        query = db.query(Prospect.prospect_id).filter(Prospect.tenant_id == plist.tenant_id, Prospect.deleted_at.is_(None))
        return query.filter(clause) if clause is not None else query
    return db.query(ProspectListMember.prospect_id).filter(ProspectListMember.list_id == plist.list_id)


# ── Data quality ────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def quality_flags(p: Prospect) -> list:
    """Problems worth fixing on a contact (BR-CM-34)."""
    flags = []
    if not _EMAIL_RE.match(p.email or "") or p.is_valid_email is False:
        flags.append("Email address looks invalid")
    for label, phone in (("Phone", p.phone), ("Mobile", p.mobile_phone)):
        if phone:
            digits = re.sub(r"\D", "", phone)
            if not (7 <= len(digits) <= 15) or re.search(r"[A-Za-z]", phone):
                flags.append(f"{label} number format looks wrong")
    for label, name in (("First name", p.first_name), ("Last name", p.last_name)):
        if name and len(name) > 1 and (name.islower() or (name.isupper() and len(name) > 3)):
            flags.append(f"{label} capitalisation looks wrong")
    return flags
