# app/models/prospect.py
"""
Prospect and Global Unsubscribe models.
GDPR compliant with consent tracking and anonymization support.
"""

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Computed, ForeignKey, Index, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id


SEARCH_TEXT_SQL = (
    "LOWER(CONCAT_WS(' ', first_name, last_name, email, company_name, designation, "
    "REGEXP_REPLACE(COALESCE(phone, ''), '[^0-9]', ''), REGEXP_REPLACE(COALESCE(mobile_phone, ''), '[^0-9]', '')))"
)


class Prospect(Base):
    """
    Prospect/Contact with GDPR compliance fields.
    Includes timezone support and email validation status.
    """
    __tablename__ = "prospects"

    prospect_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    # Basic Info
    first_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    email_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # BUSINESS / PERSONAL / UNKNOWN
    email_provider: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    mobile_phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Ownership & account (company_name is kept for display, imports and existing queries)
    owner_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("accounts.account_id"), nullable=True)

    # CRM lifecycle (BR-CM-06/07), lead source (BR-CM-01) and legal basis (BR-CM-40)
    lifecycle_stage: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    lead_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    lead_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    legal_basis: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # Soft delete with 90-day restore (BR-CM-35); merged duplicates point at the kept record
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    deleted_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    merged_into_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    # Lower-cased name, email, company, title and phone digits in one column, so a search
    # scans one narrow column instead of six (contact search < 1 s at 100k contacts)
    search_text: Mapped[Optional[str]] = mapped_column(String(1100), Computed(SEARCH_TEXT_SQL, persisted=True), nullable=True)

    # Tags (JSON list of strings) and tenant-defined custom field values (JSON object)
    tags: Mapped[Any] = mapped_column(JSON, nullable=True)
    custom_fields: Mapped[Any] = mapped_column(JSON, nullable=True)

    # Company Info
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    designation: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    emp_band: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # e.g. "51-200", "10000+"

    # LinkedIn Integration
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    linkedin_data: Mapped[Any] = mapped_column(JSON, nullable=True)

    # Location & Timezone
    poc_city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    poc_state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    poc_country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    timezone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # GDPR Consent
    consent_status: Mapped[Optional[str]] = mapped_column(String(50), default="OPT_IN", nullable=True)  # OPT_IN / UNSUBSCRIBED
    consent_source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    consent_timestamp: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Contact score on fit and engagement (BR-CM-43): 0-100 each, total and grade A-D;
    # NULL until first scored. Kept on the row so list sort / filter stay one indexed query.
    score: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    fit_score: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    engagement_score: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    score_grade: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)
    scored_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    # Validation & Anonymization
    is_valid_email: Mapped[Optional[bool]] = mapped_column(Boolean, default=True, nullable=True)
    is_anonymized: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, nullable=True)
    anonymized_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6, nullable=True)
    # Optimistic concurrency (T-3, app/core/concurrency.py): bumped by every user edit
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    # Relationships
    tenant = relationship("Tenant", back_populates="prospects")
    list_memberships = relationship("ProspectListMember", back_populates="prospect", lazy="dynamic",
                                    foreign_keys="ProspectListMember.prospect_id",
                                    primaryjoin="Prospect.prospect_id == ProspectListMember.prospect_id")
    campaign_enrollments = relationship("CampaignProspect", back_populates="prospect", lazy="dynamic")
    email_messages = relationship("EmailMessage", back_populates="prospect", lazy="dynamic")
    owner = relationship("User", foreign_keys=[owner_id])
    account = relationship("Account", back_populates="contacts")
    activities = relationship("ContactActivity", back_populates="prospect", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_tenant_email"),
        # Target of the (tenant_id, prospect_id) foreign keys of the contact's child rows (C-22, 0028_child_tenant_ids)
        UniqueConstraint("tenant_id", "prospect_id", name="uq_prospects_tenant_prospect"),
        Index("ix_prospects_owner_id", "owner_id"),
        Index("ix_prospects_account_id", "account_id"),
        Index("ix_prospects_deleted_at", "deleted_at"),
        Index("ix_prospects_lifecycle_stage", "lifecycle_stage"),
        Index("ix_prospects_tenant_live_created", "tenant_id", "deleted_at", "created_at"),
        Index("ix_prospects_tenant_live_updated", "tenant_id", "deleted_at", "updated_at"),
        Index("ix_prospects_tenant_live_score", "tenant_id", "deleted_at", "score"),
        Index("ix_prospects_tenant_live_grade", "tenant_id", "deleted_at", "score_grade"),
        Index("ix_prospects_scored_at", "scored_at"),
        # Contact search pre-filter (D7, 0030): ngram tokens of ngram_token_size (server default 2), built
        # without stopwords (app.core.database.MYSQL_SESSION_INIT_COMMAND); see app/repositories/contacts.py
        Index("ft_prospects_search_text", "search_text", mysql_prefix="FULLTEXT", mysql_with_parser="ngram"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
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

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), primary_key=True)
    email: Mapped[str] = mapped_column(String(255), primary_key=True)
    unsubscribed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # When the suppression period expires. After this datetime, the prospect
    # can be enrolled in new campaigns again.
    # NULL = permanent block (legacy rows or explicitly permanent).
    suppression_expires_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<GlobalUnsubscribe {self.email}>"
