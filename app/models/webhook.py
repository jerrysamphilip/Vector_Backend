"""
Outbound webhooks (BRD v2.0 BR-SF-20): endpoints a workspace admin registers, and the
delivery outbox the worker sends from (app/services/webhooks.py).
"""

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from app.core.encrypted_type import EncryptedString
from app.models.base import Base
from app.models.status_checks import status_check
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


def _uuid():
    return new_id()


class WebhookEndpoint(Base):
    """An HTTPS URL that receives the subscribed events, signed with the endpoint's secret."""
    __tablename__ = "webhook_endpoints"

    endpoint_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    url = Column(String(2000), nullable=False)
    description = Column(String(255), nullable=True)
    secret = Column(EncryptedString, nullable=False)        # encrypted at rest; shown once on create / rotate
    event_types = Column(JSON, nullable=False)              # ["contact.created", ...]
    active = Column(Boolean, nullable=False, default=True, server_default="1")
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    disabled_reason = Column(String(255), nullable=True)    # set when switched off automatically
    disabled_at = Column(UtcDateTime, nullable=True)
    last_success_at = Column(UtcDateTime, nullable=True)
    last_failure_at = Column(UtcDateTime, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = (
        Index("ix_webhook_endpoints_tenant", "tenant_id", "active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class WebhookDelivery(Base):
    """
    One event for one endpoint (outbox). Written in the same transaction as the change that
    raised the event; the worker sends PENDING rows whose next_attempt_at has come.
    status: PENDING / SUCCEEDED / FAILED (attempts used up) / CANCELLED (endpoint disabled or deleted)
    """
    __tablename__ = "webhook_deliveries"

    delivery_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    endpoint_id = Column(String(36), ForeignKey("webhook_endpoints.endpoint_id", ondelete="CASCADE"), nullable=False)
    event_id = Column(String(36), nullable=False)           # same id for every endpoint that gets the event
    event_type = Column(String(50), nullable=False)
    payload = Column(Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False)  # the exact JSON body that is signed and sent
    status = Column(String(12), nullable=False, default="PENDING", server_default="PENDING")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    response_code = Column(Integer, nullable=True)
    last_error = Column(String(500), nullable=True)
    next_attempt_at = Column(UtcDateTime, nullable=True)
    last_attempt_at = Column(UtcDateTime, nullable=True)
    delivered_at = Column(UtcDateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    is_test = Column(Boolean, nullable=False, default=False, server_default="0")
    redelivery_of = Column(String(36), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)

    __table_args__ = (
        status_check("webhook_deliveries", "status"),  # D5, 0029_status_checks
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
        Index("ix_webhook_deliveries_tenant", "tenant_id", "created_at"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
