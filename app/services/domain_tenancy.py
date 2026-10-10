# app/services/domain_tenancy.py
"""
Tenant-scoped helpers for sending domains.

A sending domain record (SendingDomain) belongs to one tenant; two tenants sending from
the same domain name each have their own row and their own health, status and alerts.
Every lookup by domain name must also name the tenant, and every per-domain figure must
count only that tenant's mail. EmailMessage has no tenant_id: a message belongs to the
tenant of its mailbox (inbox_id) or, failing that, of its campaign (campaign_id).
"""
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.campaign import Campaign
from app.models.domain_reputation import SendingDomain
from app.models.email_message import EmailMessage
from app.models.sending_inbox import SendingInbox


def domain_of(email: Optional[str]) -> Optional[str]:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def tenant_inbox_domains(db: Session, tenant_id: str) -> set:
    """Domains the tenant has mailboxes on."""
    rows = db.query(SendingInbox.email_address).filter(SendingInbox.tenant_id == tenant_id).all()
    return {d for (email,) in rows if (d := domain_of(email))}


def get_tenant_domain(db: Session, tenant_id: Optional[str], domain_name: str) -> Optional[SendingDomain]:
    if not tenant_id or not domain_name:
        return None
    return (db.query(SendingDomain)
            .filter(SendingDomain.tenant_id == tenant_id,
                    SendingDomain.domain_name == domain_name.strip().lower())
            .first())


def get_or_create_tenant_domain(db: Session, tenant_id: str, domain_name: str, **defaults) -> SendingDomain:
    """The tenant's row for this domain, added (not committed) when missing."""
    domain_name = domain_name.strip().lower()
    row = get_tenant_domain(db, tenant_id, domain_name)
    if row is None:
        row = SendingDomain(tenant_id=tenant_id, domain_name=domain_name, **defaults)
        db.add(row)
        db.flush()
    return row


def tenant_has_domain(db: Session, tenant_id: Optional[str], domain_name: Optional[str]) -> bool:
    """The tenant owns a record for this domain or has a mailbox on it."""
    if not tenant_id or not domain_name:
        return False
    domain_name = domain_name.strip().lower()
    return (get_tenant_domain(db, tenant_id, domain_name) is not None
            or domain_name in tenant_inbox_domains(db, tenant_id))


def tenant_message_criterion(tenant_id: str):
    """SQL criterion: this EmailMessage was sent by (belongs to) the tenant."""
    return or_(
        EmailMessage.inbox_id.in_(select(SendingInbox.inbox_id).where(SendingInbox.tenant_id == tenant_id)),
        EmailMessage.campaign_id.in_(select(Campaign.campaign_id).where(Campaign.tenant_id == tenant_id)),
    )


def from_domain_criterion(domain_name: str):
    return EmailMessage.from_email.like(f"%@{domain_name.strip().lower()}")


def tenant_for_message(db: Session, msg: Optional[EmailMessage]) -> Optional[str]:
    """Tenant that sent this message (mailbox first, then campaign)."""
    if msg is None:
        return None
    if msg.inbox_id:
        t = db.query(SendingInbox.tenant_id).filter(SendingInbox.inbox_id == msg.inbox_id).scalar()
        if t:
            return t
    if msg.campaign_id:
        return db.query(Campaign.tenant_id).filter(Campaign.campaign_id == msg.campaign_id).scalar()
    return None


def tenant_for_sender(db: Session, sender_email: Optional[str]) -> Optional[str]:
    """Tenant owning this exact sending address, when exactly one does."""
    if not sender_email:
        return None
    tenants = {t for (t,) in db.query(SendingInbox.tenant_id)
               .filter(SendingInbox.email_address == sender_email.strip().lower()).all()}
    return tenants.pop() if len(tenants) == 1 else None
