# app/services/contact_write_service.py
"""
Write side of contact management (BRD v2.0, section 5.2): create and edit contacts with
property history, bulk actions (BR-CM-36/37), merge (BR-CM-33), soft delete / restore /
purge (BR-CM-35) and associated companies (CM-014).

Only users with manage_prospects may reassign, bulk edit, merge or delete. ``ContactWriteService``
serves the routes: each method is one unit of work that it commits; errors are domain errors
(app/core/errors.py). SQL lives in app/repositories/contacts.py.
"""

import uuid
from typing import Any, Callable, Collection, Dict, List, Optional, Sequence, Tuple

from app.repositories.lists import free_list_name
from app.core.clock import utcnow
from app.core.concurrency import claim_version, resolve_expected
from app.core.errors import BadRequestError, ConflictError, DomainError, NotFoundError, PermissionDenied
from app.models.account import Account
from app.models.crm import ContactCompany, PropertyChange
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectList, ProspectListMember
from app.models.user import User
from app.repositories.contacts import ContactsAccess, ContactsUnitOfWork, Session, contacts_uow
from app.schemas.contact_schema import BulkAction, CompanyLinkCreate, CompanyLinkUpdate, ContactCreate, ContactUpdate, MergeRequest
from app.services import contact_companies, contact_scoring, crm
from app.services.contact_field_service import clean_custom_fields, required_missing
from app.services.contact_query_service import companies_of, detail_of, find_contact, user_name
from app.services.contact_service import (
    NO_MANAGE_PERMISSION,
    can_access_contact,
    can_manage_contacts,
    can_see_owner,
    clean_str,
    delete_contacts,
    merge_into,
    normalize_tags,
    require_manager,
    visibility_scope,
)
from app.services.suppression import active_suppression

__all__ = ["NO_MANAGE_PERMISSION", "ContactWriteService", "require_manager", "update_contact"]

# Properties tracked in history and editable through the API
TRACKED_FIELDS = (
    "first_name", "last_name", "email", "phone", "mobile_phone", "designation", "company_name",
    "account_id", "owner_id", "industry", "emp_band", "linkedin_url", "poc_city", "poc_state",
    "poc_country", "timezone", "lifecycle_stage", "lead_status", "lead_source", "legal_basis",
    "consent_status", "tags", "custom_fields",
)
TEXT_FIELDS = ("first_name", "last_name", "phone", "mobile_phone", "designation", "industry", "emp_band",
               "linkedin_url", "poc_city", "poc_country", "lead_source")
# Properties the bulk "set_property" action may change (plus custom.<key>)
BULK_EDITABLE = frozenset(TEXT_FIELDS) | {"poc_state", "lifecycle_stage", "lead_status", "legal_basis", "consent_status"}
CONSENT_STATUSES = ("OPT_IN", "UNSUBSCRIBED")
# Outbound emails not handed to the provider yet; an email change retargets them
UNSENT_STATUSES = ("QUEUED", "SCHEDULED", "PAUSED", "PAUSED_BY_CAMPAIGN")


# ── Guards ─────────────────────────────────────────────────────────

def require_can_delete(user: User) -> None:
    if user.role == "AGENT":
        raise PermissionDenied("Agents cannot delete contacts")


def check_owner(ctx: ContactsAccess, user: User, owner_id: Optional[str]) -> None:
    """A new owner must be a user of the workspace the actor can see (BR-SH-02)."""
    if owner_id and not ctx.contacts.user_in_tenant(user.tenant_id, owner_id):
        raise BadRequestError("Owner must be a user in your workspace")
    if owner_id and not can_see_owner(ctx.session, user, owner_id):
        raise PermissionDenied("You can only assign records to yourself or your team")


def get_account(ctx: ContactsAccess, user: User, account_id: str) -> Account:
    account = ctx.contacts.live_account(user.tenant_id, account_id)
    if not account:
        raise BadRequestError("Company not found")
    return account


def check_enum(field: str, value: Optional[str], allowed: Collection[str]) -> None:
    """`allowed` may be a code -> label mapping (its keys are the allowed values)."""
    if value is not None and value not in allowed:
        raise BadRequestError(f"{field} must be one of {list(allowed)}")


# ── Field application ──────────────────────────────────────────────

def email_meta(email: str) -> Tuple[Optional[str], Optional[str]]:
    """(email_type, email_provider) for an address."""
    from app.utils.email_utils import parse_email
    info = parse_email(email)
    if info:
        return info.get("email_type"), info.get("email_provider")
    return "PERSONAL", email.split("@")[-1] if "@" in email else None


def merge_custom_values(current: Optional[Dict[str, Any]], cleaned: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cleaned custom values over the current ones; None removes a key; empty -> None."""
    merged = dict(current or {})
    for key, value in cleaned.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged or None


def sync_subscription(db: Session, prospect: Prospect, status: str, user: User) -> None:
    """Keep the global unsubscribe list in step with the contact's subscription (BR-CM-39)."""
    from app.services.suppression import lift, suppress
    if status == "UNSUBSCRIBED":
        # Excluded from every campaign at once; pending emails are cancelled (BR-DF-08)
        suppress(db, prospect.tenant_id, prospect.email, f"Unsubscribed by {user_name(user)}",
                 kind="UNSUBSCRIBE", source="MANUAL")
    else:
        lift(db, prospect.tenant_id, prospect.email)
    prospect.consent_timestamp = utcnow()
    prospect.consent_source = "MANUAL"


def _apply_company(ctx: ContactsAccess, user: User, prospect: Prospect, data: dict, creating: bool) -> None:
    """An explicit company wins; otherwise a changed company name relinks (by domain first)."""
    if "account_id" in data:
        if data["account_id"]:
            account = get_account(ctx, user, data["account_id"])
            prospect.account_id = account.account_id
            prospect.company_name = account.name
        else:
            prospect.account_id = None
            if "company_name" in data:
                prospect.company_name = clean_str(data["company_name"])
    elif "company_name" in data or creating:
        if "company_name" in data:
            prospect.company_name = clean_str(data["company_name"])
        account = crm.resolve_company(ctx.session, user.tenant_id, prospect.email if creating else None,
                                      prospect.company_name, industry=prospect.industry, emp_band=prospect.emp_band)
        if account is None and not creating and prospect.company_name is None:
            prospect.account_id = None
        elif account is not None:
            prospect.account_id = account.account_id
            prospect.company_name = prospect.company_name or account.name


def _apply_picklists(user: User, prospect: Prospect, data: dict) -> None:
    if "lifecycle_stage" in data:
        stage = data["lifecycle_stage"]
        check_enum("lifecycle_stage", stage, crm.LIFECYCLE_STAGES)
        if not crm.lifecycle_move_allowed(prospect.lifecycle_stage, stage, user):
            raise BadRequestError(
                f"Lifecycle stage moves forward only: {crm.LIFECYCLE_STAGES[prospect.lifecycle_stage]} "
                f"can't go back to {crm.LIFECYCLE_STAGES[stage]}. Ask an admin to change it.")
        prospect.lifecycle_stage = stage
    if "lead_status" in data:
        check_enum("lead_status", data["lead_status"], crm.LEAD_STATUSES)
        prospect.lead_status = data["lead_status"]
    if "legal_basis" in data:
        check_enum("legal_basis", data["legal_basis"], crm.LEGAL_BASES)
        prospect.legal_basis = data["legal_basis"]


def apply_fields(ctx: ContactsAccess, user: User, prospect: Prospect, data: dict, creating: bool = False) -> None:
    """Apply create/update fields, keeping company name, company link and subscription in step."""
    if "owner_id" in data:
        if not can_manage_contacts(user) and data["owner_id"] != user.user_id:
            raise PermissionDenied("Only users who manage prospects can reassign contacts")
        check_owner(ctx, user, data["owner_id"])
        prospect.owner_id = data["owner_id"]

    for field in TEXT_FIELDS:
        if field in data:
            setattr(prospect, field, clean_str(data[field]))

    if "poc_state" in data:
        prospect.poc_state = clean_str(data["poc_state"])
        from app.utils.business_calendar import get_timezone_for_state
        prospect.timezone = get_timezone_for_state(prospect.poc_state) if prospect.poc_state else None

    _apply_picklists(user, prospect, data)

    if "consent_status" in data and data["consent_status"] != prospect.consent_status:
        check_enum("consent_status", data["consent_status"], CONSENT_STATUSES)
        prospect.consent_status = data["consent_status"]
        if not creating:
            sync_subscription(ctx.session, prospect, prospect.consent_status, user)

    if "tags" in data:
        prospect.tags = normalize_tags(data["tags"]) or None

    if "custom_fields" in data:
        prospect.custom_fields = merge_custom_values(
            prospect.custom_fields, clean_custom_fields(ctx.contacts, user.tenant_id, data["custom_fields"] or {}))

    _apply_company(ctx, user, prospect, data, creating)


# ── Create / update ────────────────────────────────────────────────

def _check_new_email(ctx: ContactsAccess, user: User, email: str) -> None:
    existing = ctx.contacts.by_email(user.tenant_id, email)
    if existing:
        if existing.deleted_at is not None:
            message = "A deleted contact has this email. Restore it from Recently deleted instead."
            raise ConflictError(message, detail={
                "message": message, "prospect_id": existing.prospect_id, "deleted": True})
        message = "A contact with this email already exists."
        raise ConflictError(message, detail={
            "message": message,
            "prospect_id": existing.prospect_id if can_access_contact(user, existing) else None,
        })


def _add_to_static_list(ctx: ContactsAccess, user: User, prospect: Prospect, list_id: str) -> None:
    target = ctx.contacts.list_in_tenant(user.tenant_id, list_id)
    if not target or target.list_type != "STATIC":
        raise BadRequestError("Static list not found")
    ctx.contacts.stage(ProspectListMember(list_id=target.list_id, prospect_id=prospect.prospect_id,
                                          is_new_prospect=True))


def create_contact(ctx: ContactsAccess, user: User, payload: ContactCreate) -> Prospect:
    """A new contact (staged; the caller commits). Duplicate emails are 409 (BR-CM-02)."""
    repo = ctx.contacts
    email = payload.email.strip()
    _check_new_email(ctx, user, email)
    email_type, email_provider = email_meta(email)
    unsubscribed = repo.is_unsubscribed(user.tenant_id, email)

    prospect = Prospect(
        prospect_id=str(uuid.uuid4()),
        tenant_id=user.tenant_id,
        email=email,
        email_type=email_type,
        email_provider=email_provider,
        consent_status="UNSUBSCRIBED" if unsubscribed else "OPT_IN",
        consent_source="MANUAL",
        consent_timestamp=utcnow(),
        is_valid_email=True,
        owner_id=user.user_id,
        lifecycle_stage="LEAD",
        lead_status="NEW",
    )
    data = payload.model_dump(exclude_unset=True, exclude={"email", "list_id"})
    data.setdefault("owner_id", user.user_id)
    if unsubscribed:
        data.pop("consent_status", None)
    apply_fields(ctx, user, prospect, data, creating=True)
    missing = required_missing(repo, user.tenant_id, prospect)
    if missing:
        raise BadRequestError(f"Required: {', '.join(missing)}")
    repo.add(user.tenant_id, prospect)
    repo.flush()
    if prospect.consent_status == "UNSUBSCRIBED" and not unsubscribed:
        sync_subscription(ctx.session, prospect, "UNSUBSCRIBED", user)

    if payload.list_id:
        _add_to_static_list(ctx, user, prospect, payload.list_id)

    contact_companies.sync_primary(repo, prospect, user.user_id)
    repo.add(user.tenant_id, PropertyChange(tenant_id=user.tenant_id, object_type="CONTACT",
                                            object_id=prospect.prospect_id, field="created",
                                            new_value="Contact created", source="UI", changed_by=user.user_id))
    from app.services import webhooks
    webhooks.emit_event(ctx.session, user.tenant_id, "contact.created", webhooks.contact_data(prospect))
    return prospect


def change_email(ctx: ContactsAccess, user: User, prospect: Prospect, new_email: str) -> int:
    """Give the contact a new address (caller records history and commits).

    Refused when another contact has it (409) or it is suppressed (unsubscribed, bounced,
    complained: 409), since changing the address must not route around the suppression list.
    The new address has not bounced, so the contact counts as valid again, and emails that are
    queued but not sent yet go to the new address. Returns the number of emails retargeted."""
    if ctx.contacts.email_taken(user.tenant_id, new_email, prospect.prospect_id):
        raise ConflictError("A contact with this email already exists.")
    entry = active_suppression(ctx.session, user.tenant_id, new_email)
    if entry:
        raise ConflictError(f"{new_email} is on the suppression list ({entry.reason or 'unsubscribed'}) "
                            "and cannot be used for this contact.")
    prospect.email = new_email
    prospect.email_type, prospect.email_provider = email_meta(new_email)
    prospect.is_valid_email = True
    return ctx.contacts.retarget_unsent(prospect.prospect_id, new_email, UNSENT_STATUSES)


def _update_staged(ctx: ContactsAccess, user: User, prospect_id: str, payload: ContactUpdate,
                   if_match: Optional[int]) -> Prospect:
    prospect = find_contact(ctx, user, prospect_id)
    data = payload.model_dump(exclude_unset=True)
    # T-3: the version the client saw, before anything changes (409 STALE_VERSION when stale)
    claim_version(ctx.session, prospect, resolve_expected(data.pop("expected_version", None), if_match),
                  record="contact")
    before = crm.snapshot(prospect, TRACKED_FIELDS)

    new_email = data.pop("email", None)
    if new_email and new_email.strip().lower() != (prospect.email or "").lower():
        change_email(ctx, user, prospect, new_email.strip())

    apply_fields(ctx, user, prospect, data)
    crm.record_changes(ctx.session, user.tenant_id, "CONTACT", prospect.prospect_id, before,
                       crm.snapshot(prospect, TRACKED_FIELDS), user.user_id, "UI")
    if before.get("account_id") != prospect.account_id:
        contact_companies.sync_primary(ctx.contacts, prospect, user.user_id)
    return prospect


def update_contact(db: Session, user: User, prospect_id: str, payload: ContactUpdate,
                   if_match: Optional[int] = None) -> Prospect:
    """Edit a contact and commit (also used by the prospects API)."""
    return ContactWriteService.for_session(db).update_contact(user, prospect_id, payload, if_match)


# ── Bulk actions (BR-CM-36/37) ─────────────────────────────────────

class _Bulk:
    """One bulk request: the selected contacts, the actor and the result being built."""

    def __init__(self, uow: ContactsUnitOfWork, user: User, payload: BulkAction, prospects: List[Prospect]) -> None:
        self.uow, self.db, self.user, self.payload, self.prospects = uow, uow.session, user, payload, prospects
        self.repo = uow.contacts
        self.result: Dict[str, Any] = {"status": "ok", "action": payload.action, "updated": len(prospects)}

    def tracked(self, fn: Callable[[Prospect], None], fields: Sequence[str]) -> None:
        """Apply fn to each contact, recording property history with source BULK.

        Bulk edits are set-based, so they take no expected version (T-3), but they bump each
        contact's row_version: a form opened before the bulk edit gets 409 instead of undoing it."""
        for p in self.prospects:
            before = crm.snapshot(p, fields)
            fn(p)
            self.repo.bump_version(p)
            crm.record_changes(self.db, self.user.tenant_id, "CONTACT", p.prospect_id, before,
                               crm.snapshot(p, fields), self.user.user_id, "BULK")

    @property
    def ids(self) -> List[str]:
        return [p.prospect_id for p in self.prospects]


def _bulk_assign_owner(b: _Bulk) -> None:
    check_owner(b.uow, b.user, b.payload.owner_id)
    b.tracked(lambda p: setattr(p, "owner_id", b.payload.owner_id), ("owner_id",))


def _bulk_tags(b: _Bulk) -> None:
    tags = normalize_tags(b.payload.tags)
    if not tags:
        raise BadRequestError("Give at least one tag")
    if b.payload.action == "add_tags":
        b.tracked(lambda p: setattr(p, "tags", normalize_tags(list(p.tags or []) + tags)), ("tags",))
    else:
        remove = {t.lower() for t in tags}
        b.tracked(lambda p: setattr(p, "tags", [t for t in (p.tags or []) if t.lower() not in remove] or None),
                  ("tags",))


def _bulk_set_account(b: _Bulk) -> None:
    account = get_account(b.uow, b.user, b.payload.account_id) if b.payload.account_id else None

    def link(p: Prospect) -> None:
        p.account_id = account.account_id if account else None
        if account:
            p.company_name = account.name
        contact_companies.sync_primary(b.repo, p, b.user.user_id)
    b.tracked(link, ("account_id", "company_name"))


def bulk_property_data(field: str, value: Any) -> dict:
    """The apply_fields payload for a bulk 'set_property' (custom.<key> edits a custom property)."""
    if field not in BULK_EDITABLE and not field.startswith("custom."):
        raise BadRequestError(f"Can't bulk edit '{field}'")
    return {"custom_fields": {field[len("custom."):]: value}} if field.startswith("custom.") else {field: value}


def _bulk_set_property(b: _Bulk) -> None:
    data = bulk_property_data(b.payload.field or "", b.payload.value)
    skipped: List[dict] = []

    def edit(p: Prospect) -> None:
        # Each contact in its own savepoint: one refusal skips that contact only
        try:
            with b.uow.savepoint():
                apply_fields(b.uow, b.user, p, dict(data))
        except DomainError as exc:
            skipped.append({"prospect_id": p.prospect_id, "email": p.email, "reason": exc.detail})
    b.tracked(edit, TRACKED_FIELDS)
    b.result["skipped"] = skipped
    b.result["updated"] = len(b.prospects) - len(skipped)


def _bulk_target_list(b: _Bulk) -> ProspectList:
    user, payload = b.user, b.payload
    if payload.new_list_name and payload.action == "add_to_list":
        plist = ProspectList(tenant_id=user.tenant_id, list_name=free_list_name(b.db, user.tenant_id, payload.new_list_name),
                             source_type="MANUAL", list_type="STATIC", uploaded_by=user.user_id)
        b.repo.add(user.tenant_id, plist)
        b.repo.flush()
    else:
        found = b.repo.list_in_tenant(user.tenant_id, payload.list_id)
        if not found:
            raise NotFoundError("List not found")
        plist = found
    if plist.list_type != "STATIC":
        raise BadRequestError("Active lists update automatically; add contacts to a static list")
    return plist


def _bulk_list(b: _Bulk) -> None:
    plist = _bulk_target_list(b)
    ids = b.ids
    if b.payload.action == "add_to_list":
        existing = b.repo.list_member_ids(plist.list_id, ids)
        added = [pid for pid in ids if pid not in existing]
        for pid in added:
            b.repo.stage(ProspectListMember(list_id=plist.list_id, prospect_id=pid, is_new_prospect=False))
        b.result.update(updated=len(added), already_in_list=len(existing))
    else:
        b.result["updated"] = b.repo.remove_list_members(plist.list_id, ids)
    b.result.update(list_id=plist.list_id, list_name=plist.list_name)


def _bulk_enroll(b: _Bulk) -> None:
    from app.schemas.campaign_schema import CampaignEnrollmentRequest
    from app.services.campaign_service import CampaignService
    from app.services.daily_limit import enrollment_notice
    from app.services.enrollment_rules import MAX_REJECTIONS_RETURNED, summarize
    db, user = b.db, b.user
    campaign = b.repo.campaign_in_tenant(user.tenant_id, b.payload.campaign_id)
    if not campaign:
        raise NotFoundError("Campaign not found")
    try:
        count, rejections = CampaignService(db).enroll_prospects_with_report(
            # Specific contacts only (list_ids defaults to None; passed so type checkers see it)
            campaign.campaign_id, user.user_id, CampaignEnrollmentRequest(list_ids=None, prospect_ids=b.ids))
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    b.result.update(updated=count, enrolled_count=count, rejected_count=len(rejections),
                    rejected_summary=summarize(rejections), rejected=rejections[:MAX_REJECTIONS_RETURNED])
    b.result["daily_limit_notice"] = enrollment_notice(db, user, count, b.ids, rejections, campaign.campaign_id)


def _bulk_delete(b: _Bulk) -> None:
    require_can_delete(b.user)
    b.result["updated"] = crm.soft_delete_contacts(b.db, b.prospects, b.user.user_id)


_BULK_ACTIONS: Dict[str, Callable[[_Bulk], None]] = {
    "assign_owner": _bulk_assign_owner,
    "add_tags": _bulk_tags,
    "remove_tags": _bulk_tags,
    "set_account": _bulk_set_account,
    "set_property": _bulk_set_property,
    "add_to_list": _bulk_list,
    "remove_from_list": _bulk_list,
    "enroll": _bulk_enroll,
    "delete": _bulk_delete,
}


# ── Associated companies (CM-014) ──────────────────────────────────

def _set_primary(ctx: ContactsAccess, user: User, prospect: Prospect, account: Account) -> None:
    """Make account the primary company (prospects.account_id); the old primary stays associated."""
    before = crm.snapshot(prospect, ("account_id", "company_name"))
    for row in ctx.contacts.links_of(prospect.prospect_id):
        if row.account_id != account.account_id:
            row.is_primary = False
    prospect.account_id, prospect.company_name = account.account_id, account.name
    crm.record_changes(ctx.session, user.tenant_id, "CONTACT", prospect.prospect_id, before,
                       crm.snapshot(prospect, ("account_id", "company_name")), user.user_id, "UI")
    contact_companies.sync_primary(ctx.contacts, prospect, user.user_id)


def _record_association(ctx: ContactsAccess, user: User, prospect: Prospect, field_value: dict) -> None:
    ctx.contacts.add(user.tenant_id, PropertyChange(
        tenant_id=user.tenant_id, object_type="CONTACT", object_id=prospect.prospect_id,
        field="company_association", source="UI", changed_by=user.user_id, **field_value))


def _get_link(ctx: ContactsAccess, prospect: Prospect, account_id: str) -> ContactCompany:
    link = ctx.contacts.link(prospect.prospect_id, account_id)
    if not link:
        raise NotFoundError("This contact is not associated with that company")
    return link


def _remove_link(ctx: ContactsAccess, user: User, prospect: Prospect, account_id: str,
                 new_primary_id: Optional[str]) -> None:
    link = _get_link(ctx, prospect, account_id)
    if prospect.account_id == account_id:
        others = ctx.contacts.linked_account_ids(prospect.prospect_id, except_account=account_id)
        if not others:
            raise BadRequestError(
                "This is the contact's only company. Add another company first, or clear the Company field.")
        if not new_primary_id or new_primary_id not in others:
            raise BadRequestError("Choose which associated company becomes primary (new_primary_id)")
        _set_primary(ctx, user, prospect, get_account(ctx, user, new_primary_id))
    ctx.contacts.remove(link)
    _record_association(ctx, user, prospect, {"old_value": account_id})


# ── The service the routes call ────────────────────────────────────

class ContactWriteService:
    """Contact writes for the API; each method is one unit of work it commits."""

    def __init__(self, uow: ContactsUnitOfWork) -> None:
        self.uow = uow

    @classmethod
    def for_session(cls, db: Session) -> "ContactWriteService":
        return cls(contacts_uow(db))

    def create(self, user: User, payload: ContactCreate) -> dict:
        with self.uow as uow:
            prospect = create_contact(uow, user, payload)
            uow.commit()
            contact_scoring.rescore(uow.session, user.tenant_id, [prospect.prospect_id])  # BR-CM-43
            return detail_of(uow, user, prospect)

    def _update(self, uow: ContactsUnitOfWork, user: User, prospect_id: str, payload: ContactUpdate,
                if_match: Optional[int]) -> Prospect:
        prospect = _update_staged(uow, user, prospect_id, payload, if_match)
        uow.commit()
        contact_scoring.rescore(uow.session, user.tenant_id, [prospect.prospect_id])  # BR-CM-43
        uow.contacts.refresh(prospect)
        return prospect

    def update_contact(self, user: User, prospect_id: str, payload: ContactUpdate,
                       if_match: Optional[int] = None) -> Prospect:
        with self.uow as uow:
            return self._update(uow, user, prospect_id, payload, if_match)

    def update(self, user: User, prospect_id: str, payload: ContactUpdate, if_match: Optional[int] = None) -> dict:
        """Edit a contact (T-3: expected_version or If-Match) and return its record page."""
        with self.uow as uow:
            return detail_of(uow, user, self._update(uow, user, prospect_id, payload, if_match))

    def delete(self, user: User, prospect_id: str) -> dict:
        """Soft delete: hidden everywhere and restorable for the restore window (BR-CM-35)."""
        require_manager(user)
        require_can_delete(user)
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            crm.soft_delete_contacts(uow.session, [prospect], user.user_id)
            uow.commit()
        return {"status": "deleted", "prospect_id": prospect_id, "restorable_days": crm.RESTORE_WINDOW_DAYS}

    def restore(self, user: User, prospect_ids: List[str]) -> dict:
        require_manager(user)
        with self.uow as uow:
            count = crm.restore_contacts(uow.session, user.tenant_id, prospect_ids, user.user_id, user=user)
            uow.commit()
        return {"status": "restored", "restored": count}

    def purge(self, user: User, prospect_ids: List[str]) -> dict:
        """Delete forever contacts in Recently deleted (same visibility as the list and restore).
        Uses the erase path; each contact leaves an audit row."""
        require_manager(user)
        with self.uow as uow:
            ids = uow.contacts.deleted_ids(user.tenant_id, visibility_scope(uow.session, user), prospect_ids)
            delete_contacts(uow.session, ids, actor_id=user.user_id, reason="PURGE_CONTACT")
            uow.commit()
        return {"status": "purged", "purged": len(ids)}

    def bulk(self, user: User, payload: BulkAction) -> Dict[str, Any]:
        """Bulk edit, assign, tag, add to list, enroll or delete."""
        require_manager(user)
        with self.uow as uow:
            prospects = uow.contacts.visible_by_ids(user.tenant_id, visibility_scope(uow.session, user),
                                                    payload.prospect_ids)
            if not prospects:
                raise NotFoundError("No matching contacts")
            handler = _BULK_ACTIONS.get(payload.action)
            if handler is None:
                raise BadRequestError(f"Unknown action '{payload.action}'")
            bulk = _Bulk(uow, user, payload, prospects)
            handler(bulk)
            uow.commit()
            return bulk.result

    def merge(self, user: User, payload: MergeRequest) -> dict:
        """Fold duplicates into the primary (BR-CM-33) and return the merged record page."""
        require_manager(user)
        duplicate_ids = [i for i in dict.fromkeys(payload.duplicate_ids) if i != payload.primary_id]
        if not duplicate_ids:
            raise BadRequestError("Choose at least one other contact to merge")
        with self.uow as uow:
            repo = uow.contacts
            primary = find_contact(uow, user, payload.primary_id)
            duplicates = repo.visible_by_ids(user.tenant_id, visibility_scope(uow.session, user), duplicate_ids)
            if len(duplicates) != len(duplicate_ids):
                raise NotFoundError("One or more contacts to merge were not found")
            before = crm.snapshot(primary, TRACKED_FIELDS)
            moved = merge_into(repo, primary, duplicates, user.user_id, payload.choices)
            crm.record_changes(uow.session, user.tenant_id, "CONTACT", primary.prospect_id, before,
                               crm.snapshot(primary, TRACKED_FIELDS), user.user_id, "MERGE")
            contact_companies.sync_primary(repo, primary, user.user_id)
            uow.commit()
            repo.refresh(primary)
            return {"status": "merged", "merged": len(duplicates), "moved": moved,
                    "contact": detail_of(uow, user, primary)}

    def add_company(self, user: User, prospect_id: str, payload: CompanyLinkCreate) -> dict:
        with self.uow as uow:
            repo = uow.contacts
            prospect = find_contact(uow, user, prospect_id)
            account = get_account(uow, user, payload.account_id)
            contact_companies.sync_primary(repo, prospect, user.user_id)
            if repo.link(prospect.prospect_id, account.account_id):
                raise ConflictError("This contact is already associated with that company")
            repo.add(user.tenant_id, ContactCompany(tenant_id=user.tenant_id, prospect_id=prospect.prospect_id,
                                                    account_id=account.account_id, label=clean_str(payload.label),
                                                    is_primary=False, created_by=user.user_id))
            repo.flush()
            _record_association(uow, user, prospect, {"new_value": account.account_id})
            # The first company is always the primary one
            if payload.make_primary or not prospect.account_id:
                _set_primary(uow, user, prospect, account)
            repo.touch(prospect)
            uow.commit()
            return companies_of(repo, prospect)

    def update_company(self, user: User, prospect_id: str, account_id: str, payload: CompanyLinkUpdate) -> dict:
        with self.uow as uow:
            repo = uow.contacts
            prospect = find_contact(uow, user, prospect_id)
            contact_companies.sync_primary(repo, prospect, user.user_id)
            link = _get_link(uow, prospect, account_id)
            data = payload.model_dump(exclude_unset=True)
            if "label" in data:
                link.label = clean_str(data["label"])
            if data.get("is_primary") is True and prospect.account_id != account_id:
                _set_primary(uow, user, prospect, get_account(uow, user, account_id))
            elif data.get("is_primary") is False and prospect.account_id == account_id:
                raise BadRequestError("Make another company primary instead")
            repo.touch(prospect)
            uow.commit()
            return companies_of(repo, prospect)

    def remove_company(self, user: User, prospect_id: str, account_id: str, new_primary_id: Optional[str]) -> dict:
        """Remove an association. The primary company can only go once another one is chosen as primary."""
        with self.uow as uow:
            prospect = find_contact(uow, user, prospect_id)
            contact_companies.sync_primary(uow.contacts, prospect, user.user_id)
            _remove_link(uow, user, prospect, account_id, new_primary_id)
            uow.contacts.touch(prospect)
            uow.commit()
            return companies_of(uow.contacts, prospect)
