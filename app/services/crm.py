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

import re
from datetime import timedelta
from typing import Any, Iterable, Optional

from app.core.clock import utcnow
from app.domain.contacts import CONTACT_FILTER_FIELDS, OPERATORS, FilterError, FilterNode, ListScope
from app.domain.contacts import MAX_CONDITIONS as MAX_CONDITIONS
from app.domain.contacts import MAX_FILTER_DEPTH as MAX_FILTER_DEPTH
from app.domain.contacts import parse_filter_param as parse_filter_param
from app.domain.contacts import parse_filter_tree
from app.models.account import Account
from app.models.crm import PropertyChange
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList
from app.models.user import User
from app.repositories.contacts import (
    ContactRepository,
    Session,
    SqlAlchemyContactRepository,
    SqlAlchemyContactsUnitOfWork,
    compile_filter,
    list_member_query,
)
from app.services.contact_service import delete_contacts, visibility_scope
from app.services.sales_settings import can_see_amounts
from app.utils.company_domain import bare_domain, is_valid_domain, registrable_domain
from app.utils.free_mail import is_free_mail_domain
from app.utils.property_format import fmt_value

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

# Provider labels used by the SQL duplicate count in app/services/crm_kpis.py only. The Python
# rule (is_personal_domain) checks whole domains: app/utils/free_mail.py.
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

_fmt = fmt_value  # history format, shared with workflow rules and webhooks (app/utils/property_format.py)


def snapshot(obj: Any, fields: Iterable[str]) -> dict:
    return {f: getattr(obj, f) for f in fields}


def record_changes(db: Session, tenant_id: str, object_type: str, object_id: str,
                   before: dict, after: dict, user_id: Optional[str], source: str = "UI") -> int:
    """Write one history row per changed field. custom_fields / tags are diffed per key."""
    repo = SqlAlchemyContactRepository(db)
    rows = 0
    changed = {}

    def log(field: str, old: Any, new: Any) -> None:
        repo.add(tenant_id, PropertyChange(tenant_id=tenant_id, object_type=object_type, object_id=object_id,
                                           field=field, old_value=_fmt(old), new_value=_fmt(new),
                                           source=source, changed_by=user_id))

    for field, old in before.items():
        new = after.get(field)
        if field == "custom_fields":
            old, new = old or {}, new or {}
            for key in sorted(set(old) | set(new)):
                if _fmt(old.get(key)) != _fmt(new.get(key)):
                    log(f"custom.{key}", old.get(key), new.get(key))
                    rows += 1
            continue
        if _fmt(old) != _fmt(new):
            log(field, old, new)
            changed[field] = (old, new)
            rows += 1
    # Workflow rules react to the same changes (BR-SF-13); imports and merges are excluded
    if changed and source not in ("IMPORT", "MERGE"):
        from app.services import workflow
        workflow.on_changes(db, tenant_id, object_type, object_id, changed, user_id, record_changes=record_changes)
    if rows:
        # Outbound webhooks (BR-SF-20): contact / lead / deal events, queued in this transaction
        from app.services import webhooks
        webhooks.on_record_changes(db, tenant_id, object_type, object_id, before, after, source)
    return rows


# ── Companies ───────────────────────────────────────────────────

def email_domain(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def is_personal_domain(domain: Optional[str]) -> bool:
    """A free / consumer mail domain (gmail.com, orange.fr, yahoo.co.jp), or no domain at all."""
    if not domain:
        return True
    return is_free_mail_domain(domain)


def clean_domain(value: Optional[str]) -> Optional[str]:
    """'https://www.Acme.com/about' -> 'acme.com'; a subdomain collapses to its registrable
    domain (us.acme.com -> acme.com, hr.acme.co.uk -> acme.co.uk). Text that is not a valid
    domain is returned cleaned but uncollapsed: callers that need a valid one check is_valid_domain."""
    value = bare_domain(value)
    if value and is_valid_domain(value):
        return registrable_domain(value)
    return value


def company_domain_from_email(email: Optional[str]) -> Optional[str]:
    """The company domain an address implies (registrable domain), or None for free mail / invalid."""
    domain = email_domain(email)
    if not domain or not is_valid_domain(domain) or is_personal_domain(domain):
        return None
    return registrable_domain(domain)


def _name_from_domain(domain: str) -> str:
    label = domain.split(".")[0]
    return " ".join(part.capitalize() for part in re.split(r"[-_]", label) if part) or domain


def resolve_company(db: Session, tenant_id: str, email: Optional[str] = None,
                    company_name: Optional[str] = None, create: bool = True, **defaults: Any) -> Optional[Account]:
    """
    The company a contact belongs to (BR-CM-04): matched by the email's domain, created
    from it when new. Personal email providers fall back to the company name.
    """
    repo = SqlAlchemyContactRepository(db)
    domain = company_domain_from_email(email)
    name = (company_name or "").strip()[:255] or None
    if domain:
        return _company_by_domain(repo, tenant_id, domain, name, create, defaults)
    if not name:
        return None
    account = repo.live_account_by(tenant_id, name=name)
    if account or not create:
        return account
    if repo.deleted_account_holds(tenant_id, name=name):
        return None  # name held by a company in the recycle bin
    return _new_account(repo, Account(tenant_id=tenant_id, name=name, lifecycle_stage="LEAD",
                                      **{k: v for k, v in defaults.items() if v}))


def _new_account(repo: ContactRepository, account: Account) -> Account:
    repo.add(account.tenant_id, account)
    repo.flush()
    return account


def _company_by_domain(repo: ContactRepository, tenant_id: str, domain: str, name: Optional[str], create: bool,
                       defaults: dict) -> Optional[Account]:
    account = repo.live_account_by(tenant_id, domain=domain)
    if account:
        return account
    if not create:
        return None
    # A domain-less account with the same name is the same company: give it the domain
    if name:
        same_name = repo.live_account_by(tenant_id, name=name, domainless=True)
        if same_name:
            same_name.domain = domain
            return same_name
    # A company in the recycle bin still owns its domain (unique key): leave the contact
    # unlinked rather than linking it to the deleted company; restoring the company relinks
    if repo.deleted_account_holds(tenant_id, domain=domain):
        return None
    new_name = name or _name_from_domain(domain)
    if repo.account_name_taken(tenant_id, new_name):
        new_name = f"{new_name} ({domain})"[:255]
    return _new_account(repo, Account(tenant_id=tenant_id, name=new_name, domain=domain,
                                      website=f"https://{domain}", lifecycle_stage="LEAD",
                                      **{k: v for k, v in defaults.items() if v}))


# ── Soft delete / restore ───────────────────────────────────────

def soft_delete_contacts(db: Session, prospects: list, user_id: Optional[str]) -> int:
    """Hide contacts for 90 days and stop anything still queued to them."""
    ids = [p.prospect_id for p in prospects if p.deleted_at is None]
    if not ids:
        return 0
    repo = SqlAlchemyContactRepository(db)
    repo.soft_delete(ids, utcnow(), user_id)
    for pid in ids:
        repo.stage(PropertyChange(tenant_id=prospects[0].tenant_id, object_type="CONTACT", object_id=pid,
                                  field="deleted", old_value=None, new_value="deleted", source="UI",
                                  changed_by=user_id))
    from app.services import webhooks
    webhooks.contacts_deleted(db, [p for p in prospects if p.prospect_id in ids], user_id)
    return len(ids)


def restore_contacts(db: Session, tenant_id: str, ids: list, user_id: Optional[str],
                     user: Optional[User] = None) -> int:
    """Restore soft-deleted contacts; with `user`, only those inside their visibility (BR-SH-02)."""
    cutoff = utcnow() - timedelta(days=RESTORE_WINDOW_DAYS)
    repo = SqlAlchemyContactRepository(db)
    rows = repo.restorable(tenant_id, ids, cutoff, visibility_scope(db, user) if user is not None else None)
    for p in rows:
        p.deleted_at, p.deleted_by = None, None
        repo.add(tenant_id, PropertyChange(tenant_id=tenant_id, object_type="CONTACT", object_id=p.prospect_id,
                                           field="deleted", old_value="deleted", new_value=None, source="UI",
                                           changed_by=user_id))
    return len(rows)


def purge_expired(db: Session) -> int:
    """Permanently delete contacts and companies deleted more than 90 days ago."""
    cutoff = utcnow() - timedelta(days=RESTORE_WINDOW_DAYS)
    with SqlAlchemyContactsUnitOfWork.for_session(db) as uow:
        repo = uow.contacts
        ids = repo.expired_contact_ids(cutoff, 5000)
        delete_contacts(db, ids, reason="PURGE_CONTACT")  # recycle bin expiry, by the system
        accounts = repo.expired_accounts(cutoff)
        for account in accounts:
            repo.purge_account(account)
        uow.commit()
    return len(ids) + len(accounts)


# ── Filters (AND / OR) ──────────────────────────────────────────
#
# A filter is either a condition {"field", "operator", "value"} or a group
# {"op": "AND"|"OR", "conditions": [...]} — groups nest. Fields are contact properties,
# "custom.<key>" for custom properties, plus "tags", "list", "company.<prop>". The tree is parsed
# and checked in app/domain/contacts.py and compiled to SQL in app/repositories/contacts.py.

CONTACT_FIELDS = CONTACT_FILTER_FIELDS
__all__ = ["CONTACT_FIELDS", "OPERATORS", "MAX_CONDITIONS", "MAX_FILTER_DEPTH", "FilterError",
           "parse_filter_param"]


def parse_filter(db: Session, spec: Any, user: User) -> Optional[FilterNode]:
    """The checked AND/OR tree for ``user`` ("me" = the user; revenue filters need the amounts right)."""
    return parse_filter_tree(spec, user.user_id, lambda: can_see_amounts(db, user))


def filter_clause(db: Session, spec: Any, user: User, depth: int = 0, counter: Optional[list] = None) -> Any:
    """SQL condition for a filter spec (see above); None/empty means 'everything'."""
    tree = parse_filter_tree(spec, user.user_id, lambda: can_see_amounts(db, user), depth, counter)
    return compile_filter(db, tree)


# ── Lists ───────────────────────────────────────────────────────

def list_scope(db: Session, plist: ProspectList, user: User) -> ListScope:
    """Membership of a list; an active list's filters are checked for ``user`` (FilterError if broken)."""
    active = plist.list_type == "ACTIVE"
    return ListScope(plist.list_id, plist.tenant_id, active, parse_filter(db, plist.filters, user) if active else None)


def list_member_ids(db: Session, plist: ProspectList, user: User) -> Any:
    """Subquery of contact ids in a list. Active lists are evaluated live from their filters."""
    return list_member_query(db, list_scope(db, plist, user))


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


# ── Exports ─────────────────────────────────────────────────────

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> Any:
    """Neutralise spreadsheet formula injection: text starting with = + - @ (or tab / CR)
    is prefixed with a single quote so Excel / Sheets treat it as text."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_safe_row(values) -> list:
    return [csv_safe(v) for v in values]
