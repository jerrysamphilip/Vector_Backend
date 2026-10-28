# app/models/user.py
"""
User model with role-based access control and authentication.
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON, BigInteger, Boolean, Computed, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.core.encrypted_type import EncryptedString
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

# users.active_email (migration 0019 keeps its own literal copy)
ACTIVE_EMAIL_EXPRESSION = "CASE WHEN status IN ('ACTIVE','INVITED') THEN email ELSE NULL END"


class User(Base):
    """
    System user with role-based permissions.

    Role hierarchy (highest → lowest):
      PLATFORM_ADMIN — Neutrino Tech staff. No tenant. Manages platform only.
      SUPER_ADMIN    — Tenant creator. Full control inside their tenant.
      ADMIN          — Tenant administrator.
      MANAGER        — Runs campaigns; cannot manage users.
      AGENT          — Works assigned campaigns.
    """
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False, default=lambda: str(uuid.uuid4()))
    # NULL for PLATFORM_ADMIN (lives outside any tenant)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="AGENT")  # PLATFORM_ADMIN / SUPER_ADMIN / ADMIN / MANAGER / AGENT
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="ACTIVE")  # ACTIVE / INVITED / INACTIVE / SUSPENDED
    # One live account per email across every workspace (F017; migration 0019_unique_active_email):
    # the email while the account is ACTIVE or INVITED, NULL otherwise, under a UNIQUE index. Written
    # by the database only (STORED generated column). Lookups: app/services/user_lookup.py.
    active_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        Computed(ACTIVE_EMAIL_EXPRESSION, persisted=True),
        nullable=True,
    )

    # Auth fields
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)  # null for Google-only users
    auth_provider: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="local")  # local / google
    google_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, unique=True)
    email_verified: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    invited_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)

    # Granular feature permissions (JSON list of permission keys).
    # null = use role defaults. Set explicitly by SUPER_ADMIN to override defaults.
    custom_permissions: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True, default=None)

    last_login_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    # Sales hierarchy (BR-SH-01/02): level 1 CEO / COO / Sales Head, 2 Business
    # Development, 3 Business Executive, 4 Market Research. NULL = not in the
    # hierarchy (visibility then follows the role, as before). manager_id is the
    # user one level up; a user sees their own records and everyone's below them.
    sales_level: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    manager_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    # Also send in-app notifications by email (BR-SF-07)
    notify_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Two-factor sign-in (TOTP, app/core/totp.py; migration 0004_user_mfa). mfa_secret is encrypted at
    # rest and set by /api/auth/mfa/setup before mfa_enabled turns on. mfa_recovery_codes is a JSON list
    # of SHA-256 hashes of the unused recovery codes. mfa_last_step is the last accepted TOTP time step
    # (a code is never accepted twice).
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=expression.false())
    mfa_secret: Mapped[Optional[str]] = mapped_column(EncryptedString, nullable=True)
    mfa_recovery_codes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    mfa_enabled_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    mfa_last_step: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="users")
    created_campaigns = relationship("Campaign", back_populates="creator", lazy="dynamic")
    uploaded_lists = relationship("ProspectList", back_populates="uploader", lazy="dynamic")
    approved_templates = relationship("EmailTemplate", back_populates="approver", lazy="dynamic")
    template_versions = relationship("EmailTemplateVersion", back_populates="creator", lazy="dynamic")
    owned_groups = relationship("CampaignGroup", back_populates="owner", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
        Index("uq_users_active_email", "active_email", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
