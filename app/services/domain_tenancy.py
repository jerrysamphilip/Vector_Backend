# app/services/domain_tenancy.py
"""
Tenant-scoped helpers for sending domains.

A sending domain record (SendingDomain) belongs to one tenant; two tenants sending from
the same domain name each have their own row and their own health, status and alerts.
Every lookup by domain name must also name the tenant, and every per-domain figure must
count only that tenant's mail. EmailMessage has no tenant_id: a message belongs to the
tenant of its mailbox (inbox_id) or, failing that, of its campaign (campaign_id).

Thin ``fn(db, ...)`` helpers over app/repositories/deliverability.py (used inside the caller's
transaction: the SES webhook, send safety, deliverability); the pure rules are in
app/domain/deliverability.py and re-exported here under their historical names.
"""
from typing import Any, Optional, Set

from app.domain.deliverability import dns_record_guidance, domain_of, normalize_domain_name
from app.models.email_message import EmailMessage
from app.repositories.deliverability import (SendingDomain, Session, borrow, from_domain_criterion,
                                             tenant_message_criterion)

__all__ = ["dns_record_guidance", "domain_of", "from_domain_criterion", "get_or_create_tenant_domain",
           "get_tenant_domain", "normalize_domain_name", "tenant_for_message", "tenant_for_sender",
           "tenant_has_domain", "tenant_inbox_domains", "tenant_message_criterion"]


def tenant_inbox_domains(db: Session, tenant_id: str) -> Set[str]:
    """Domains the tenant has mailboxes on."""
    return {d for (_, email) in borrow(db).inbox_addresses(tenant_id) if (d := domain_of(email))}


def get_tenant_domain(db: Session, tenant_id: Optional[str], domain_name: str) -> Optional[SendingDomain]:
    return borrow(db).get_domain(tenant_id, domain_name)


def get_or_create_tenant_domain(db: Session, tenant_id: str, domain_name: str, **defaults: Any) -> SendingDomain:
    """The tenant's row for this domain, added (not committed) when missing."""
    domain_name = domain_name.strip().lower()
    repo = borrow(db)
    row = repo.get_domain(tenant_id, domain_name)
    if row is None:
        row = repo.add_domain(SendingDomain(tenant_id=tenant_id, domain_name=domain_name, **defaults))
    return row


def tenant_has_domain(db: Session, tenant_id: Optional[str], domain_name: Optional[str]) -> bool:
    """The tenant owns a record for this domain or has a mailbox on it."""
    if not tenant_id or not domain_name:
        return False
    domain_name = domain_name.strip().lower()
    return (get_tenant_domain(db, tenant_id, domain_name) is not None
            or domain_name in tenant_inbox_domains(db, tenant_id))


def tenant_for_message(db: Session, msg: Optional[EmailMessage]) -> Optional[str]:
    """Tenant that sent this message (mailbox first, then campaign)."""
    if msg is None:
        return None
    repo = borrow(db)
    if msg.inbox_id:
        t = repo.inbox_tenant(msg.inbox_id)
        if t:
            return t
    if msg.campaign_id:
        return repo.campaign_tenant(msg.campaign_id)
    return None


def tenant_for_sender(db: Session, sender_email: Optional[str]) -> Optional[str]:
    """Tenant owning this exact sending address, when exactly one does."""
    if not sender_email:
        return None
    tenants = borrow(db).sender_tenants(sender_email.strip().lower())
    return tenants.pop() if len(tenants) == 1 else None
