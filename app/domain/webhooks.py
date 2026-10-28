"""
Outbound webhooks aggregate: query objects and pure rules for the endpoints API (ADR 0009, chunk A7).

Event catalogue, signatures, backoff and URL safety stay in app/utils/webhooks.py (shared with the
worker). Pure: no I/O. Invalid values raise ``WebhookInputError`` (a ``ValueError``); the service maps
it to the 422 the API has always returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DELIVERY_STATUSES = ("PENDING", "SUCCEEDED", "FAILED", "CANCELLED")


class WebhookInputError(ValueError):
    """A request value the webhooks API refuses (422)."""


@dataclass(frozen=True)
class DeliveryFilter:
    """GET /integrations/webhooks/{id}/deliveries parameters, validated."""

    status: Optional[str] = None
    event_type: Optional[str] = None
    limit: int = 50
    offset: int = 0

    @classmethod
    def parse(cls, status: Optional[str], event_type: Optional[str], limit: int, offset: int) -> DeliveryFilter:
        wanted = status.upper() if status else None
        if wanted and wanted not in DELIVERY_STATUSES:
            raise WebhookInputError(f"status must be one of {', '.join(DELIVERY_STATUSES)}")
        return cls(wanted, event_type or None, limit, offset)


def clean_description(text: Optional[str]) -> Optional[str]:
    text = (text or "").strip()
    return text[:255] or None
