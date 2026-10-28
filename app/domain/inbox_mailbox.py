"""
Sending inbox (mailbox) edit rules for the email accounts page (F-20) and the IMAP settings derived from
SMTP (S-26). Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

# Blank values for these keep what is saved (the edit form leaves secrets empty)
CREDENTIAL_FIELDS = ("imap_password", "imap_username", "smtp_password", "smtp_username")
PORT_FIELDS = ("smtp_port", "imap_port")
HOST_FIELDS = ("smtp_host", "imap_host")
WARMUP_DISABLED_MESSAGE = "Warmup is disabled for this inbox."


class InvalidPort(ValueError):
    """A port outside 1-65535 (422)."""


def clean_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    """The edit without blank credentials, with ports checked (InvalidPort) and host names trimmed
    (blank -> None). The hosts still need the mail-host safety check."""
    cleaned = {k: v for k, v in updates.items() if not (k in CREDENTIAL_FIELDS and not v)}
    for field in PORT_FIELDS:
        port = cleaned.get(field)
        if port is not None and not 0 < port < 65536:
            raise InvalidPort(f"{field} must be between 1 and 65535")
    for field in HOST_FIELDS:
        if isinstance(cleaned.get(field), str):
            cleaned[field] = cleaned[field].strip() or None
    return cleaned


def warmup_toggle(enabling: bool, warmup_start_date: Optional[datetime], now: datetime) -> Dict[str, Any]:
    """Fields set when warm-up is switched on or off: a first start date, the status, and the issue
    shown on the page."""
    fields: Dict[str, Any] = {"warmup_status": "ACTIVE" if enabling else "DISABLED"}
    if enabling and not warmup_start_date:
        fields.update(warmup_start_date=now, warmup_day=0)
    if enabling:
        fields.update(warmup_issue_code=None, warmup_issue_message=None)
    else:
        fields.update(warmup_issue_code="WARMUP_DISABLED", warmup_issue_message=WARMUP_DISABLED_MESSAGE)
    return fields


def changes_receiving(updates: Dict[str, Any]) -> bool:
    """New receiving credentials: the old sync error is cleared so the next sync reports fresh status."""
    return any(k.startswith("imap_") for k in updates)


def domain_of_address(email_address: Optional[str]) -> Optional[str]:
    if not email_address or "@" not in email_address:
        return None
    return email_address.split("@")[-1]


# ── IMAP derived from SMTP (S-26) ─────────────────────────────────────────
# Most mailboxes receive on the server they send through: when no IMAP host is saved, it is derived from the
# SMTP host (never stored), and blank IMAP credentials reuse the SMTP ones.
DEFAULT_IMAP_PORT = 993
KNOWN_IMAP_HOSTS = {
    "smtp.gmail.com": "imap.gmail.com",
    "smtp.googlemail.com": "imap.gmail.com",
    "smtp.office365.com": "outlook.office365.com",
    "smtp-mail.outlook.com": "outlook.office365.com",
    "smtp.outlook.com": "outlook.office365.com",
    "smtp.zoho.com": "imap.zoho.com",
    "smtp.zoho.in": "imap.zoho.in",
    "smtp.zoho.eu": "imap.zoho.eu",
    "smtp.zoho.com.au": "imap.zoho.com.au",
    "smtppro.zoho.com": "imappro.zoho.com",
    "smtppro.zoho.in": "imappro.zoho.in",
    "smtppro.zoho.eu": "imappro.zoho.eu",
}
# Relays that only send: their host and credentials say nothing about a mailbox to read
SEND_ONLY_SUFFIXES = (".amazonaws.com", ".sendgrid.net", ".mailgun.org", ".sparkpostmail.com", ".postmarkapp.com")


def _send_only(smtp_host: str) -> bool:
    return smtp_host.endswith(SEND_ONLY_SUFFIXES)


def derived_imap_host(smtp_host: Optional[str]) -> Optional[str]:
    """The IMAP host of a known provider, else smtp.<domain> -> imap.<domain>; None when it cannot be told."""
    host = (smtp_host or "").strip().lower().rstrip(".")
    if not host or _send_only(host):
        return None
    if host in KNOWN_IMAP_HOSTS:
        return KNOWN_IMAP_HOSTS[host]
    if host.startswith("smtp.") and len(host) > len("smtp."):
        return "imap." + host[len("smtp."):]
    return None


@dataclass(frozen=True)
class EffectiveImap:
    """The IMAP settings a mailbox is read with: the saved ones, gaps filled from SMTP (not stored)."""
    effective_imap_host: Optional[str]
    effective_imap_port: int
    effective_imap_username: Optional[str]
    effective_imap_password: Optional[str]
    imap_host_derived: bool

    def configured(self, oauth: bool = False) -> bool:
        """A host and something to sign in with (a password, or a Microsoft 365 token)."""
        return bool(self.effective_imap_host and (self.effective_imap_password or oauth))


def effective_imap(mailbox: Any) -> EffectiveImap:
    """For any object with the SendingInbox imap_* / smtp_* / email_address attributes."""
    saved_host = (getattr(mailbox, "imap_host", None) or "").strip() or None
    host = saved_host or derived_imap_host(getattr(mailbox, "smtp_host", None))
    smtp_host = (getattr(mailbox, "smtp_host", None) or "").strip().lower()
    reuse_smtp = bool(smtp_host) and not _send_only(smtp_host)
    password = getattr(mailbox, "imap_password", None) or None
    username = getattr(mailbox, "imap_username", None) or None
    if not password and reuse_smtp:
        password = getattr(mailbox, "smtp_password", None) or None
        username = username or getattr(mailbox, "smtp_username", None) or None
    port = getattr(mailbox, "imap_port", None) if saved_host else None
    return EffectiveImap(effective_imap_host=host, effective_imap_port=port or DEFAULT_IMAP_PORT,
                         effective_imap_username=username or getattr(mailbox, "email_address", None),
                         effective_imap_password=password, imap_host_derived=host is not None and not saved_host)
