# app/models/tenant.py
"""
Tenant model for multi-tenant architecture.
"""

from sqlalchemy import Boolean, Column, String
from sqlalchemy.sql import expression
from sqlalchemy.orm import relationship
import uuid

from app.models import Base
from app.models.types import NOW6, UtcDateTime


class Tenant(Base):
    """
    Multi-tenant organization.
    All data is scoped to a tenant for data isolation.
    """
    __tablename__ = "tenants"

    tenant_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_name = Column(String(255), nullable=False)
    status = Column(String(50), default="ACTIVE")  # ACTIVE / SUSPENDED / DELETED
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)
    # Every user must sign in with two-factor (set by a SUPER_ADMIN, PUT /api/auth/mfa/tenant-policy)
    require_mfa = Column(Boolean, nullable=False, default=False, server_default=expression.false())

    # Relationships
    users = relationship("User", back_populates="tenant", lazy="dynamic")
    prospects = relationship("Prospect", back_populates="tenant", lazy="dynamic")
    campaigns = relationship("Campaign", back_populates="tenant", lazy="dynamic")
    prospect_lists = relationship("ProspectList", back_populates="tenant", lazy="dynamic")
    sending_inboxes = relationship("SendingInbox", back_populates="tenant", lazy="dynamic")
    campaign_groups = relationship("CampaignGroup", back_populates="tenant", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Tenant {self.tenant_name}>"
