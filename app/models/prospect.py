# app/models/prospect.py
"""
Prospect and Global Unsubscribe models.
GDPR compliant with consent tracking and anonymization support.
"""

from sqlalchemy import Column, String, Boolean, TIMESTAMP, ForeignKey, JSON, UniqueConstraint, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class Prospect(Base):
    """
    Prospect/Contact with GDPR compliance fields.
    Includes timezone support and email validation status.
    """
    __tablename__ = "prospects"

    prospect_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    # Basic Info
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    email = Column(String(255), nullable=False)
    email_type = Column(String(50), nullable=True)  # BUSINESS / PERSONAL / UNKNOWN
    email_provider = Column(String(100), nullable=True)
    phone = Column(String(50), nullable=True)
    mobile_phone = Column(String(50), nullable=True)

    # Ownership & account (company_name is kept for display, imports and existing queries)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    account_id = Column(String(36), ForeignKey("accounts.account_id"), nullable=True)

    # CRM lifecycle (BR-CM-06/07), lead source (BR-CM-01) and legal basis (BR-CM-40)
    lifecycle_stage = Column(String(30), nullable=True)
    lead_status = Column(String(30), nullable=True)
    lead_source = Column(String(100), nullable=True)
    legal_basis = Column(String(40), nullable=True)

    # Soft delete with 90-day restore (BR-CM-35); merged duplicates point at the kept record
    deleted_at = Column(TIMESTAMP, nullable=True)
    deleted_by = Column(String(36), nullable=True)
    merged_into_id = Column(String(36), nullable=True)

    # Tags (JSON list of strings) and tenant-defined custom field values (JSON object)
    tags = Column(JSON, nullable=True)
    custom_fields = Column(JSON, nullable=True)

    # Company Info
    company_name = Column(String(255), nullable=True)
    designation = Column(String(150), nullable=True)
    industry = Column(String(100), nullable=True)
    emp_band = Column(String(50), nullable=True)  # e.g. "51-200", "10000+"

    # LinkedIn Integration
    linkedin_url = Column(String(500), nullable=True)
    linkedin_data = Column(JSON, nullable=True)

    # Location & Timezone
    poc_city = Column(String(100), nullable=True)
    poc_state = Column(String(100), nullable=True)
    poc_country = Column(String(100), nullable=True)
    timezone = Column(String(50), nullable=True)

    # GDPR Consent
    consent_status = Column(String(50), default="OPT_IN")  # OPT_IN / UNSUBSCRIBED
    consent_source = Column(String(100), nullable=True)
    consent_timestamp = Column(TIMESTAMP, nullable=True)

    # Validation & Anonymization
    is_valid_email = Column(Boolean, default=True)
    is_anonymized = Column(Boolean, default=False)
    anonymized_at = Column(TIMESTAMP, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    # Relationships
    tenant = relationship("Tenant", back_populates="prospects")
    list_memberships = relationship("ProspectListMember", back_populates="prospect", lazy="dynamic")
    campaign_enrollments = relationship("CampaignProspect", back_populates="prospect", lazy="dynamic")
    email_messages = relationship("EmailMessage", back_populates="prospect", lazy="dynamic")
    owner = relationship("User", foreign_keys=[owner_id])
    account = relationship("Account", back_populates="contacts")
    activities = relationship("ContactActivity", back_populates="prospect", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_tenant_email"),
        Index("ix_prospects_owner_id", "owner_id"),
        Index("ix_prospects_account_id", "account_id"),
        Index("ix_prospects_deleted_at", "deleted_at"),
        Index("ix_prospects_lifecycle_stage", "lifecycle_stage"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<Prospect {self.email}>"

    @property
    def full_name(self) -> str:
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or "Unknown"


class GlobalUnsubscribe(Base):
    """
    Global unsubscribe list (tenant-scoped).
    Emails here should never receive campaigns.
    """
    __tablename__ = "global_unsubscribes"

    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), primary_key=True)
    email = Column(String(255), primary_key=True)
    unsubscribed_at = Column(TIMESTAMP, server_default=func.now())
    reason = Column(String(255), nullable=True)
    # When the suppression period expires. After this datetime, the prospect
    # can be enrolled in new campaigns again.
    # NULL = permanent block (legacy rows or explicitly permanent).
    suppression_expires_at = Column(TIMESTAMP, nullable=True)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<GlobalUnsubscribe {self.email}>"
