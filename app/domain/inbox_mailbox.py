"""
Sending inbox (mailbox) edit rules for the email accounts page (F-20). Pure: no I/O.
"""

from __future__ import annotations

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
