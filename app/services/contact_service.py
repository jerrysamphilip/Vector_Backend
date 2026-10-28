# app/services/contact_service.py
"""
Shared contact-management rules: who may see and manage which contacts (BR-SH-02), value
normalisation, account linking, cascade delete (erasure) and duplicate merging. Used by the contact
services, accounts, lists, sales and the upload flow.

SQL lives in app/repositories/contacts.py; the helpers that take ``db`` keep their signatures for the
modules that still pass a session (they build the repository on it). The caller commits.
"""

import json
from collections.abc import Collection
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Protocol, Set, runtime_checkable

from app.core.auth import _DEFAULT_PERMS
from app.core.clock import utcnow
from app.core.errors import ConflictError, PermissionDenied
from app.domain.contacts import VisibilityScope, phone_digits, team_ids
from app.models.account import Account
from app.models.contact_activity import ContactActivity
from app.models.contact_identity import MergeLog
from app.models.prospect import Prospect
from app.models.user import User
from app.repositories.contacts import (
    ContactRepository,
    Session,
    SqlAlchemyContactRepository,
    apply_scope,
    owner_condition,
    session_of,
)
from app.repositories.duplicates import DuplicateRepository
from app.services.contact_companies import move_links

__all__ = [
    "MERGE_CHOOSABLE", "MERGE_REFUSED", "NO_MANAGE_PERMISSION", "SALES_LEVELS", "backfill_accounts", "can_access_contact",
    "can_see_account",
    "can_manage_contacts", "can_see_owner", "clean_str", "delete_contacts", "get_or_create_account",
    "merge_contacts", "merge_refusal", "normalize_tags", "record_snapshot", "owner_clause", "phone_digits", "require_manager", "scope",
    "sees_everything", "team_user_ids", "visibility_scope", "visible_user_ids",
]

NO_MANAGE_PERMISSION = "Your account does not have the 'manage_prospects' permission."


# ── Access ─────────────────────────────────────────────────────────

def can_manage_contacts(user: User) -> bool:
    """Users with manage_prospects see and edit every contact; everyone else only their own."""
    if user.role == "SUPER_ADMIN":
        return True
    if user.role == "PLATFORM_ADMIN":
        return False
    perms = (
        frozenset(user.custom_permissions)
        if user.custom_permissions is not None
        else _DEFAULT_PERMS.get(user.role, frozenset())
    )
    return "manage_prospects" in perms


def require_manager(user: User) -> None:
    if not can_manage_contacts(user):
        raise PermissionDenied(NO_MANAGE_PERMISSION)


# ── Sales hierarchy visibility (BR-SH-02) ──────────────────────────

SALES_LEVELS = {
    1: "CEO / COO / Sales Head",
    2: "Business Development",
    3: "Business Executive",
    4: "Market Research",
}


def sees_everything(user: User) -> bool:
    """Admins and level 1 see every record; users outside the hierarchy keep the role rule."""
    if user.role in ("SUPER_ADMIN", "ADMIN"):
        return True
    if user.sales_level == 1:
        return True
    return user.sales_level is None and can_manage_contacts(user)


def team_user_ids(db: Session, user: User) -> Set[str]:
    """The user and everyone below them in the hierarchy (direct and indirect reports)."""
    return team_ids(SqlAlchemyContactRepository(db).manager_pairs(user.tenant_id), user.user_id)


def visible_user_ids(db: Session, user: User) -> Optional[Set[str]]:
    """Owners whose records this user may see; None means everyone's. Cached per request."""
    if sees_everything(user):
        return None
    cached = getattr(user, "_visible_user_ids", None)
    if cached is None:
        cached = team_user_ids(db, user) if user.sales_level else {user.user_id}
        user._visible_user_ids = cached
    return cached


def visibility_scope(db: Session, user: User) -> VisibilityScope:
    """The user's ``VisibilityScope``: whose records they see, and whether unassigned ones too."""
    ids = visible_user_ids(db, user)
    if ids is None:
        return VisibilityScope()
    return VisibilityScope(frozenset(ids), include_unassigned=can_manage_contacts(user))


def owner_clause(db: Session, user: User, column: Any) -> Any:
    """SQL condition limiting `column` (an owner id) to what the user may see; None = no limit."""
    return owner_condition(column, visibility_scope(db, user))


def scope(query: Any, db: Session, user: User, column: Any) -> Any:
    """``query`` narrowed to the owners the user may see (BR-SH-02)."""
    return apply_scope(query, column, visibility_scope(db, user))


def can_see_owner(db: Session, user: User, owner_id: Optional[str]) -> bool:
    return visibility_scope(db, user).allows(owner_id)


def can_access_contact(user: User, prospect: Prospect) -> bool:
    if sees_everything(user):
        return True  # no session needed: everyone's records are visible
    return can_see_owner(session_of(prospect), user, prospect.owner_id)


# ── Normalisation ──────────────────────────────────────────────────

def clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def normalize_tags(tags: Optional[Iterable[str]]) -> list:
    """Trim, drop blanks, de-duplicate case-insensitively, keep first spelling and order."""
    seen, result = set(), []
    for raw_tag in tags or []:
        tag = clean_str(raw_tag)
        if not tag:
            continue
        tag = tag[:50]
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


# ── Accounts ───────────────────────────────────────────────────────

def get_or_create_account(db: Session, tenant_id: str, name: Optional[str], **defaults: Any) -> Optional[Account]:
    """Find the tenant's account by (case-insensitive) name, creating it if needed."""
    name = clean_str(name)
    if not name:
        return None
    name = name[:255]
    repo = SqlAlchemyContactRepository(db)
    account = repo.account_by_name(tenant_id, name)
    if account:
        return account
    account = Account(tenant_id=tenant_id, name=name, **{k: v for k, v in defaults.items() if v})
    repo.add(tenant_id, account)
    repo.flush()
    return account


def backfill_accounts(db: Session, tenant_id: str) -> int:
    """Create accounts from company names and link contacts that have no account. Returns contacts linked."""
    return SqlAlchemyContactRepository(db).backfill_accounts(tenant_id)


# ── Delete ─────────────────────────────────────────────────────────

def delete_contacts(db: Session, prospect_ids: list, actor_id: Optional[str] = None,
                    reason: str = "ERASE_CONTACT") -> None:
    """
    Permanently delete contacts (purge / GDPR erasure) and every row that references them.
    Leads belong to their contact and go with it; deals are company records, so they stay
    but lose the link to the person. Each erasure leaves an audit row (who, when, which
    record id; no personal data). actor_id None = the system (scheduled purge). Caller commits.
    """
    if not prospect_ids:
        return
    from app.services.sales import delete_leads
    repo = SqlAlchemyContactRepository(db)
    repo.add_erasure_audit(prospect_ids, actor_id, reason)
    _scrub_personal_copies(repo, prospect_ids)
    repo.detach_messages(prospect_ids)
    delete_leads(db, repo.lead_ids(prospect_ids))
    repo.erase_rows(prospect_ids)


def _import_error_mentions(entry: dict, emails: Set[str]) -> bool:
    """A rejected import row that carries one of these addresses (in any column, or the reason)."""
    if not isinstance(entry, dict):
        return False
    values = entry.get("values") or {}
    if isinstance(values, dict) and any(isinstance(v, str) and v.strip().lower() in emails
                                        for v in values.values()):
        return True
    reason = str(entry.get("reason") or "").lower()
    return any(e in reason for e in emails)


def _redacted_webhook_payload(payload: str, event_id: str, event_type: str) -> str:
    """The delivery envelope with `data` reduced to its *_id fields (no personal data)."""
    try:
        envelope = json.loads(payload)
        if not isinstance(envelope, dict):
            raise ValueError("not an object")
    except ValueError:
        envelope = {"id": event_id, "type": event_type}
    data = envelope.get("data")
    if not isinstance(data, dict):
        data = {}
    slim = {k: v for k, v in data.items() if k.endswith("_id") and isinstance(v, (str, int))}
    slim["redacted"] = True
    envelope["data"] = slim
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _scrub_import_errors(repo: ContactRepository, tenant_id: str, emails: Set[str]) -> None:
    for job in repo.import_jobs_with_errors(tenant_id):
        errors = job.errors if isinstance(job.errors, list) else []
        kept = [e for e in errors if not _import_error_mentions(e, emails)]
        if len(kept) != len(errors):
            job.errors = kept
            job.error_count = max(0, (job.error_count or 0) - (len(errors) - len(kept)))


def _scrub_personal_copies(repo: ContactRepository, prospect_ids: list) -> None:
    """
    Erasure (D-12): personal data of these contacts copied into rows that do not reference
    them by foreign key. Caller commits.
      - import_jobs.errors: rejected rows carrying the contact's address are removed;
      - webhook_deliveries.payload: events naming the contact (id or address) keep only their
        *_id fields; a still-pending delivery is sent in that redacted form;
      - notifications: in-app notifications linking to the contact or its leads, or naming
        its address, are deleted.
    The suppression list (global_unsubscribes) keeps the address on purpose: it is what stops
    the contact from being emailed again after erasure.
    """
    people: Dict[str, list] = {}
    for pid, tenant_id, email in repo.identities(prospect_ids):
        people.setdefault(tenant_id, []).append((pid, (email or "").strip().lower()))
    for tenant_id, rows in people.items():
        ids = [pid for pid, _ in rows]
        emails = {e for _, e in rows if e}
        lead_ids = repo.lead_ids(ids)
        if emails:
            _scrub_import_errors(repo, tenant_id, emails)
        for delivery in repo.deliveries_naming(tenant_id, ids + sorted(emails)):
            delivery.payload = _redacted_webhook_payload(delivery.payload, delivery.event_id, delivery.event_type)
        links = [f"/app/contacts/{pid}" for pid in ids] + [f"/app/leads/{lid}" for lid in lead_ids]
        repo.delete_notifications(tenant_id, links, emails)


# ── Merge ──────────────────────────────────────────────────────────

# Profile fields copied from a duplicate when the primary has no value
_FILL_FIELDS = (
    "first_name", "last_name", "phone", "mobile_phone", "designation", "company_name",
    "account_id", "owner_id", "industry", "emp_band", "linkedin_url", "poc_city",
    "poc_state", "poc_country", "timezone", "lifecycle_stage", "lead_status", "lead_source",
    "legal_basis",
)
# Properties the user may pick per record when merging (BR-CM-33)
MERGE_CHOOSABLE = _FILL_FIELDS + ("email",)


class _MergeState:
    """What the primary already has while duplicates are folded into it."""

    def __init__(self, repo: ContactRepository, primary: Prospect) -> None:
        self.repo, self.primary = repo, primary
        self.lists = {m.list_id: m for m in repo.memberships(primary.prospect_id)}
        self.campaigns = {e.campaign_id for e in repo.enrollment_rows(primary.prospect_id)}
        self.has_persona = repo.persona(primary.prospect_id) is not None
        self.tags = list(primary.tags or [])
        self.custom = dict(primary.custom_fields or {})
        self.labels: List[str] = []
        self.moved = {"lists": 0, "campaigns": 0, "emails": 0, "activities": 0}

    def take_lists(self, dup: Prospect) -> None:
        for membership in self.repo.memberships(dup.prospect_id):
            existing = self.lists.get(membership.list_id)
            if existing:
                if membership.notes:
                    existing.notes = "\n\n".join(n for n in (existing.notes, membership.notes) if n)
                self.repo.remove(membership)
            else:
                membership.prospect_id = self.primary.prospect_id
                self.lists[membership.list_id] = membership
                self.moved["lists"] += 1

    def take_campaigns(self, dup: Prospect) -> None:
        for enrollment in self.repo.enrollment_rows(dup.prospect_id):
            if enrollment.campaign_id in self.campaigns:
                self.repo.remove(enrollment)
            else:
                enrollment.prospect_id = self.primary.prospect_id
                self.campaigns.add(enrollment.campaign_id)
                self.moved["campaigns"] += 1

    def take_persona(self, dup: Prospect) -> None:
        persona = self.repo.persona(dup.prospect_id)
        if persona:
            if self.has_persona:
                self.repo.remove(persona)
            else:
                persona.prospect_id = self.primary.prospect_id
                self.has_persona = True

    def take_values(self, dup: Prospect) -> None:
        primary = self.primary
        for field in _FILL_FIELDS:
            if not getattr(primary, field) and getattr(dup, field):
                setattr(primary, field, getattr(dup, field))
        self.tags.extend(dup.tags or [])
        for key, value in (dup.custom_fields or {}).items():
            if self.custom.get(key) in (None, "") and value not in (None, ""):
                self.custom[key] = value
        # Never lose an opt-out
        if dup.consent_status == "UNSUBSCRIBED":
            primary.consent_status = "UNSUBSCRIBED"
        self.labels.append(f"{dup.full_name} <{dup.email}>")


def _fold_duplicates(repo: ContactRepository, state: _MergeState, duplicates: list) -> None:
    """Move every duplicate's records to the primary and fill its blanks; tags and custom values merged."""
    primary = state.primary
    for dup in duplicates:
        state.take_lists(dup)
        state.take_campaigns(dup)
        emails, activities = repo.reassign_children(dup.prospect_id, primary.prospect_id)
        state.moved["emails"] += emails
        state.moved["activities"] += activities
        move_links(repo, dup, primary)
        repo.repoint_merged(dup.prospect_id, primary.prospect_id)
        state.take_persona(dup)
        state.take_values(dup)
    primary.tags = normalize_tags(state.tags) or None
    primary.custom_fields = state.custom or None


def merge_contacts(db: Session, primary: Prospect, duplicates: list, merged_by: Optional[str],
                   choices: Optional[dict] = None) -> dict:
    """
    Fold duplicates into primary: move lists, campaigns, emails, conversations, activities,
    tasks and history across; take the value chosen per property in `choices`
    ({field: prospect_id}) and otherwise fill the primary's blanks; union tags; then delete
    the duplicates. Caller commits.
    """
    return merge_into(SqlAlchemyContactRepository(db), primary, duplicates, merged_by, choices)


def merge_into(repo: ContactRepository, primary: Prospect, duplicates: list, merged_by: Optional[str],
               choices: Optional[dict] = None) -> dict:
    """``merge_contacts`` on a repository (the service's unit of work commits).

    With the identity store (the SQL repository; C-10 / C-11 / C-19): every involved contact is locked
    (SELECT ... FOR UPDATE) and the merge is refused (409) when one was deleted or merged meanwhile; the
    records are snapshotted into ``merge_logs`` with the choices and what moved; the duplicates' addresses
    stay in the e-mail history pointing at the survivor; their open duplicate pairs become MERGED."""
    choices = {f: pid for f, pid in (choices or {}).items() if f in MERGE_CHOOSABLE}
    by_id = {d.prospect_id: d for d in duplicates}
    chosen_values = {f: getattr(by_id[pid], f) for f, pid in choices.items() if pid in by_id}
    store = repo.duplicates if isinstance(repo, IdentityAware) else None
    snapshot = _lock_and_snapshot(store, primary, duplicates) if store is not None else None
    state = _MergeState(repo, primary)
    _fold_duplicates(repo, state, duplicates)
    for field, value in chosen_values.items():
        if field != "email":
            setattr(primary, field, value)

    if store is not None:
        store.carry_identities(primary.tenant_id, primary.prospect_id,
                               [(d.prospect_id, d.email) for d in duplicates],
                               chosen_values.get("email") or primary.email)
    repo.flush()
    repo.delete_rows([d.prospect_id for d in duplicates])
    if "email" in chosen_values and chosen_values["email"]:
        repo.flush()  # the duplicate holding this address is gone, so the unique key is free
        primary.email = chosen_values["email"]

    repo.stage(ContactActivity(
        tenant_id=primary.tenant_id,
        prospect_id=primary.prospect_id,
        activity_type="NOTE",
        subject="Merged duplicate contacts",
        body="Merged into this contact: " + "; ".join(state.labels),
        created_by=merged_by,
    ))
    if store is not None and snapshot is not None:
        _log_merge(store, primary, duplicates, merged_by, choices, state.moved, snapshot)
    return state.moved


# ── Merge safety and history (C-10, C-19) ─────────────────────────

@runtime_checkable
class IdentityAware(Protocol):
    """A contact repository that also reaches the identity store (pairs, merge logs, e-mail history)."""

    @property
    def duplicates(self) -> DuplicateRepository: ...


# What a merge log keeps of each record (enough to rebuild it by hand)
SNAPSHOT_FIELDS = MERGE_CHOOSABLE + (
    "tags", "custom_fields", "consent_status", "consent_source", "email_type", "is_valid_email",
    "score", "score_grade", "created_at", "updated_at",
)
MERGE_CONFLICT = "{who} was deleted or merged while you were merging. Reload and try again."
MERGE_REFUSED = "{who} is in Recently deleted or was already merged, so it cannot be merged."


def record_snapshot(p: Prospect) -> dict:
    """A JSON-safe copy of a contact's properties for the merge log."""
    out: Dict[str, Any] = {"prospect_id": p.prospect_id}
    for field in SNAPSHOT_FIELDS:
        value = getattr(p, field, None)
        out[field] = value.isoformat() if isinstance(value, datetime) else value
    return out


def merge_refusal(prospects: Iterable[Prospect], expected: Collection[str],
                  template: str = MERGE_CONFLICT) -> Optional[str]:
    """Why a merge cannot go ahead: a contact missing, in the recycle bin or already merged; None if fine."""
    found = {p.prospect_id: p for p in prospects}
    for pid in expected:
        p = found.get(pid)
        if p is None or p.deleted_at is not None or p.merged_into_id:
            label = f"{p.full_name} <{p.email}>" if p is not None else "A contact"
            return template.format(who=label)
    return None


def _lock_and_snapshot(store: DuplicateRepository, primary: Prospect, duplicates: list) -> dict:
    ids = [primary.prospect_id] + [d.prospect_id for d in duplicates]
    refusal = merge_refusal(store.lock_contacts(primary.tenant_id, ids), ids)
    if refusal:
        raise ConflictError(refusal)
    return {"survivor": record_snapshot(primary), "merged": [record_snapshot(d) for d in duplicates]}


def _log_merge(store: DuplicateRepository, primary: Prospect, duplicates: list, merged_by: Optional[str],
               choices: dict, moved: dict, snapshot: dict) -> None:
    merged_ids = [d.prospect_id for d in duplicates]
    store.add_merge_log(MergeLog(tenant_id=primary.tenant_id, survivor_id=primary.prospect_id,
                                 merged_ids=merged_ids, snapshot=snapshot, choices=choices or None,
                                 moved=dict(moved), merged_by=merged_by))
    store.resolve_pairs(primary.tenant_id, merged_ids, merged_by, utcnow())


def can_see_account(db: Session, user: User, account: Account) -> bool:
    """Companies owned by the user's team, or holding at least one contact they can see (BR-SH-02)."""
    if sees_everything(user) or (account.owner_id and can_see_owner(db, user, account.owner_id)):
        return True
    return SqlAlchemyContactRepository(db).has_visible_contact_at(
        account.account_id, visible_user_ids(db, user) or {"-"})
