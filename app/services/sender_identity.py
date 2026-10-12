# app/services/sender_identity.py
"""
Per-tenant sender identity: the company name and physical postal address a
tenant's campaign emails are sent under.

Source of truth is the tenant's default company profile (company_profiles,
tenant-scoped): company_name + postal_address. When a tenant has no profile the
company name falls back to the tenant's own name. There is deliberately NO
fallback to any other company's address: a tenant without an address cannot
launch new campaigns (see require_postal_address), and emails already in flight
go out without an address line rather than with someone else's.

Also provides the AI-prompt branding: prompt text refers to the sender company
with SENDER_COMPANY_TOKEN, which is replaced with the current tenant's company
name right before the text is sent to the LLM (see branded_openai_client).
"""
import contextvars
import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MISSING_ADDRESS_MESSAGE = "Set your company postal address in Settings before launching"
SENDER_COMPANY_TOKEN = "<<SENDER_COMPANY>>"
GENERIC_COMPANY = "our company"
_CACHE_SECONDS = 60


class SenderIdentityMissing(ValueError):
    """The tenant has no postal address set; routes map ValueError to HTTP 400."""


@dataclass(frozen=True)
class SenderIdentity:
    company_name: Optional[str]
    postal_address: Optional[str]

    @property
    def footer_line(self) -> Optional[str]:
        """One-line '<Company>, <address>' for the email footer, or None without an address."""
        address = ", ".join(" ".join(line.split()) for line in (self.postal_address or "").splitlines()
                            if line.strip())
        if not address:
            return None
        name = (self.company_name or "").strip()
        if name and name.lower() not in address.lower():
            return f"{name}, {address}"
        return address


_cache: dict = {}
_cache_lock = threading.Lock()


def invalidate(tenant_id: Optional[str]) -> None:
    with _cache_lock:
        _cache.pop(tenant_id, None)


def _load(db: Session, tenant_id: str) -> SenderIdentity:
    from app.models.company_profile import CompanyProfile
    from app.models.tenant import Tenant

    name = address = None
    try:
        # Savepoint: a missing column (schema not migrated yet) must not abort the caller's transaction
        with db.begin_nested():
            row = db.query(CompanyProfile.company_name, CompanyProfile.postal_address).filter(
                CompanyProfile.tenant_id == tenant_id
            ).order_by(CompanyProfile.is_default.desc(), CompanyProfile.created_at.asc()).first()
        if row:
            name, address = row[0], row[1]
    except Exception as exc:
        logger.warning(f"[SenderIdentity] Could not read company profile for tenant {tenant_id}: "
                       f"{exc.__class__.__name__}: {str(exc)[:200]}")
    if not (name and name.strip()):
        try:
            with db.begin_nested():
                name = db.query(Tenant.tenant_name).filter(Tenant.tenant_id == tenant_id).scalar()
        except Exception:
            name = None
    return SenderIdentity(
        company_name=(name or "").strip() or None,
        postal_address=(address or "").strip() or None,
    )


def get_sender_identity(tenant_id: Optional[str], db: Optional[Session] = None,
                        fresh: bool = False) -> SenderIdentity:
    """The tenant's sender identity (cached ~60s per process)."""
    if not tenant_id:
        return SenderIdentity(None, None)
    now = time.monotonic()
    if not fresh:
        with _cache_lock:
            hit = _cache.get(tenant_id)
        if hit and hit[0] > now:
            return hit[1]
    if db is not None:
        identity = _load(db, tenant_id)
    else:
        from app.core.database import SessionLocal
        own = SessionLocal()
        try:
            identity = _load(own, tenant_id)
        finally:
            own.close()
    with _cache_lock:
        _cache[tenant_id] = (now + _CACHE_SECONDS, identity)
    return identity


def require_postal_address(db: Session, tenant_id: str) -> SenderIdentity:
    """Launch gate: CAN-SPAM requires the sender's postal address in every email."""
    identity = get_sender_identity(tenant_id, db, fresh=True)
    if not identity.postal_address:
        raise SenderIdentityMissing(MISSING_ADDRESS_MESSAGE)
    return identity


# ── AI prompt branding ──────────────────────────────────────────────

_current_company: contextvars.ContextVar = contextvars.ContextVar("sender_company", default=None)


def bind_tenant(tenant_id: Optional[str], db: Optional[Session] = None) -> str:
    """
    Make this tenant's company name the one AI prompts are written for, for the
    rest of the current request / task context. Returns the name used.
    """
    name = get_sender_identity(tenant_id, db).company_name if tenant_id else None
    _current_company.set(name)
    return name or GENERIC_COMPANY


def current_company_name() -> str:
    return _current_company.get() or GENERIC_COMPANY


def brand(text):
    """Replace the sender-company token with the bound tenant's company name."""
    if not isinstance(text, str) or SENDER_COMPANY_TOKEN not in text:
        return text
    if _current_company.get() is None:
        logger.warning("[SenderIdentity] AI prompt built without a tenant bound; using a generic company name")
    return text.replace(SENDER_COMPANY_TOKEN, current_company_name())


class _BrandedCompletions:
    def __init__(self, completions):
        self._completions = completions

    def create(self, *args, **kwargs):
        messages = kwargs.get("messages")
        if messages:
            kwargs["messages"] = [
                {**m, "content": brand(m.get("content"))} if isinstance(m, dict) else m
                for m in messages
            ]
        return self._completions.create(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._completions, name)


class _BrandedChat:
    def __init__(self, chat):
        self.completions = _BrandedCompletions(chat.completions)
        self._chat = chat

    def __getattr__(self, name):
        return getattr(self._chat, name)


class branded_openai_client:
    """Wraps an OpenAI client so every chat prompt is written for the bound tenant."""

    def __init__(self, client):
        self._client = client
        self.chat = _BrandedChat(client.chat)

    def __getattr__(self, name):
        return getattr(self._client, name)
