# app/models/tenant.py
"""
Tenant model for multi-tenant architecture.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.models.base import Base
from app.models.status_checks import status_check
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


class Tenant(Base):
    """
    Multi-tenant organization.
    All data is scoped to a tenant for data isolation.
    """
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False, default=new_id)
    tenant_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="ACTIVE")  # ACTIVE / SUSPENDED / DELETED
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6, onupdate=NOW6)
    # Every user must sign in with two-factor (set by a SUPER_ADMIN, PUT /api/auth/mfa/tenant-policy)
    require_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=expression.false())

    # Relationships
    users = relationship("User", back_populates="tenant", lazy="dynamic")
    prospects = relationship("Prospect", back_populates="tenant", lazy="dynamic")
    campaigns = relationship("Campaign", back_populates="tenant", lazy="dynamic")
    prospect_lists = relationship("ProspectList", back_populates="tenant", lazy="dynamic")
    sending_inboxes = relationship("SendingInbox", back_populates="tenant", lazy="dynamic")
    campaign_groups = relationship("CampaignGroup", back_populates="tenant", lazy="dynamic")

    __table_args__ = (
        status_check("tenants", "status"),  # D5, 0029_status_checks
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.tenant_name}>"
