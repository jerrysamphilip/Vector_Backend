# app/services/contact_service.py
"""
Shared contact-management logic: access rules, account linking, cascade delete
and duplicate merging. Used by contacts_router, accounts_router and the upload flow.
"""

import re
import uuid
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import _DEFAULT_PERMS
from app.models.account import Account
from app.models.campaign import CampaignProspect
from app.models.contact_activity import ContactActivity
from app.models.crm import CrmTask, PropertyChange
from app.models.conversation import Conversation
from app.models.email_message import EmailEvent, EmailMessage
from app.models.prospect import Prospect
from app.models.prospect_list import ProspectListMember
from app.models.prospect_persona import ProspectPersona
from app.models.user import User


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


def can_access_contact(user: User, prospect: Prospect) -> bool:
    return can_manage_contacts(user) or prospect.owner_id == user.user_id


# ── Normalisation ──────────────────────────────────────────────────

def clean_str(value) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def normalize_tags(tags: Optional[Iterable[str]]) -> list:
    """Trim, drop blanks, de-duplicate case-insensitively, keep first spelling and order."""
    seen, result = set(), []
    for tag in tags or []:
        tag = clean_str(tag)
        if not tag:
            continue
        tag = tag[:50]
        if tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


def phone_digits(phone: Optional[str]) -> str:
    return re.sub(r"\D", "", phone or "")


# ── Accounts ───────────────────────────────────────────────────────

def get_or_create_account(db: Session, tenant_id: str, name: Optional[str], **defaults) -> Optional[Account]:
    """Find the tenant's account by (case-insensitive) name, creating it if needed."""
    name = clean_str(name)
    if not name:
        return None
    name = name[:255]
    account = db.query(Account).filter(Account.tenant_id == tenant_id, Account.name == name).first()
    if account:
        return account
    account = Account(
        account_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        name=name,
        **{k: v for k, v in defaults.items() if v},
    )
    db.add(account)
    db.flush()
    return account


def backfill_accounts(db: Session, tenant_id: str) -> int:
    """Create accounts from company names and link contacts that have no account. Returns contacts linked."""
    db.execute(text("""
        INSERT IGNORE INTO accounts (account_id, tenant_id, name, industry, emp_band, created_at, updated_at)
        SELECT UUID(), p.tenant_id, LEFT(TRIM(p.company_name), 255), MAX(p.industry), MAX(p.emp_band), NOW(), NOW()
        FROM prospects p
        WHERE p.tenant_id = :tenant_id AND p.account_id IS NULL
          AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
        GROUP BY p.tenant_id, LEFT(TRIM(p.company_name), 255)
    """), {"tenant_id": tenant_id})
    result = db.execute(text("""
        UPDATE prospects p
        JOIN accounts a ON a.tenant_id = p.tenant_id AND a.name = LEFT(TRIM(p.company_name), 255)
        SET p.account_id = a.account_id
        WHERE p.tenant_id = :tenant_id AND p.account_id IS NULL
          AND p.company_name IS NOT NULL AND TRIM(p.company_name) <> ''
    """), {"tenant_id": tenant_id})
    return result.rowcount or 0


# ── Delete ─────────────────────────────────────────────────────────

def delete_contacts(db: Session, prospect_ids: list) -> None:
    """Delete contacts and every row that references them. Caller commits."""
    if not prospect_ids:
        return
    message_ids = [m[0] for m in db.query(EmailMessage.message_id).filter(
        EmailMessage.prospect_id.in_(prospect_ids)
    ).all()]
    if message_ids:
        db.query(EmailEvent).filter(EmailEvent.message_id.in_(message_ids)).delete(synchronize_session=False)
    for model in (EmailMessage, Conversation, ProspectPersona, CampaignProspect,
                  ProspectListMember, ContactActivity, CrmTask):
        db.query(model).filter(model.prospect_id.in_(prospect_ids)).delete(synchronize_session=False)
    db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                    PropertyChange.object_id.in_(prospect_ids)).delete(synchronize_session=False)
    db.query(Prospect).filter(Prospect.prospect_id.in_(prospect_ids)).delete(synchronize_session=False)


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


def merge_contacts(db: Session, primary: Prospect, duplicates: list, merged_by: Optional[str],
                   choices: Optional[dict] = None) -> dict:
    """
    Fold duplicates into primary: move lists, campaigns, emails, conversations, activities,
    tasks and history across; take the value chosen per property in `choices`
    ({field: prospect_id}) and otherwise fill the primary's blanks; union tags; then delete
    the duplicates. Caller commits.
    """
    choices = {f: pid for f, pid in (choices or {}).items() if f in MERGE_CHOOSABLE}
    by_id = {d.prospect_id: d for d in duplicates}
    chosen_values = {f: getattr(by_id[pid], f) for f, pid in choices.items() if pid in by_id}
    moved = {"lists": 0, "campaigns": 0, "emails": 0, "activities": 0}
    primary_lists = {m.list_id: m for m in db.query(ProspectListMember).filter(
        ProspectListMember.prospect_id == primary.prospect_id)}
    primary_campaigns = {c.campaign_id for c in db.query(CampaignProspect.campaign_id).filter(
        CampaignProspect.prospect_id == primary.prospect_id)}
    primary_has_persona = db.query(ProspectPersona).filter(
        ProspectPersona.prospect_id == primary.prospect_id).first() is not None

    tags = list(primary.tags or [])
    custom = dict(primary.custom_fields or {})
    merged_labels = []

    for dup in duplicates:
        for membership in db.query(ProspectListMember).filter(ProspectListMember.prospect_id == dup.prospect_id).all():
            existing = primary_lists.get(membership.list_id)
            if existing:
                if membership.notes:
                    existing.notes = "\n\n".join(n for n in (existing.notes, membership.notes) if n)
                db.delete(membership)
            else:
                membership.prospect_id = primary.prospect_id
                primary_lists[membership.list_id] = membership
                moved["lists"] += 1

        for enrollment in db.query(CampaignProspect).filter(CampaignProspect.prospect_id == dup.prospect_id).all():
            if enrollment.campaign_id in primary_campaigns:
                db.delete(enrollment)
            else:
                enrollment.prospect_id = primary.prospect_id
                primary_campaigns.add(enrollment.campaign_id)
                moved["campaigns"] += 1

        moved["emails"] += db.query(EmailMessage).filter(EmailMessage.prospect_id == dup.prospect_id).update(
            {EmailMessage.prospect_id: primary.prospect_id}, synchronize_session=False)
        db.query(Conversation).filter(Conversation.prospect_id == dup.prospect_id).update(
            {Conversation.prospect_id: primary.prospect_id}, synchronize_session=False)
        moved["activities"] += db.query(ContactActivity).filter(ContactActivity.prospect_id == dup.prospect_id).update(
            {ContactActivity.prospect_id: primary.prospect_id}, synchronize_session=False)
        db.query(CrmTask).filter(CrmTask.prospect_id == dup.prospect_id).update(
            {CrmTask.prospect_id: primary.prospect_id}, synchronize_session=False)
        db.query(PropertyChange).filter(PropertyChange.object_type == "CONTACT",
                                        PropertyChange.object_id == dup.prospect_id).update(
            {PropertyChange.object_id: primary.prospect_id}, synchronize_session=False)

        persona = db.query(ProspectPersona).filter(ProspectPersona.prospect_id == dup.prospect_id).first()
        if persona:
            if primary_has_persona:
                db.delete(persona)
            else:
                persona.prospect_id = primary.prospect_id
                primary_has_persona = True

        for field in _FILL_FIELDS:
            if not getattr(primary, field) and getattr(dup, field):
                setattr(primary, field, getattr(dup, field))
        tags.extend(dup.tags or [])
        for key, value in (dup.custom_fields or {}).items():
            if custom.get(key) in (None, "") and value not in (None, ""):
                custom[key] = value
        # Never lose an opt-out
        if dup.consent_status == "UNSUBSCRIBED":
            primary.consent_status = "UNSUBSCRIBED"
        merged_labels.append(f"{dup.full_name} <{dup.email}>")

    primary.tags = normalize_tags(tags) or None
    primary.custom_fields = custom or None

    for field, value in chosen_values.items():
        if field != "email":
            setattr(primary, field, value)

    db.flush()
    db.query(Prospect).filter(Prospect.prospect_id.in_([d.prospect_id for d in duplicates])).delete(
        synchronize_session=False)
    if "email" in chosen_values and chosen_values["email"]:
        db.flush()  # the duplicate holding this address is gone, so the unique key is free
        primary.email = chosen_values["email"]

    db.add(ContactActivity(
        tenant_id=primary.tenant_id,
        prospect_id=primary.prospect_id,
        activity_type="NOTE",
        subject="Merged duplicate contacts",
        body="Merged into this contact: " + "; ".join(merged_labels),
        created_by=merged_by,
    ))
    return moved
