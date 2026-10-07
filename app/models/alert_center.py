"""
Models for tenant alert preferences and alert email dispatch history.
"""

import uuid
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from app.models.base import Base


class AlertPreference(Base):
    """
    Per-tenant alert settings.
    scope_type:
      - TENANT (global default for alert type)
      - INBOX  (override for one inbox)
    """

    __tablename__ = "alert_preferences"

    preference_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    scope_type = Column(String(20), nullable=False, default="TENANT")
    scope_id = Column(String(36), nullable=True)
    alert_type = Column(String(50), nullable=False)

    enabled = Column(Boolean, nullable=False, default=True)
    email_enabled = Column(Boolean, nullable=False, default=False)
    cooldown_minutes = Column(Integer, nullable=False, default=360)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            "alert_type",
            name="uq_alert_pref_tenant_scope_type",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class AlertEmailDispatch(Base):
    """
    Dedup / cooldown state for alert emails.
    """

    __tablename__ = "alert_email_dispatches"

    dispatch_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    alert_key = Column(String(255), nullable=False)
    recipient_email = Column(String(255), nullable=False)

    last_sent_at = Column(TIMESTAMP, nullable=True)
    send_count = Column(Integer, nullable=False, default=0)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "alert_key",
            "recipient_email",
            name="uq_alert_dispatch_tenant_key_recipient",
        ),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
