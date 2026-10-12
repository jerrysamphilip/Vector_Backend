# app/models/user.py
"""
User model with role-based access control and authentication.
"""

from sqlalchemy import BigInteger, Integer, Column, String, Boolean, DateTime, Text, TIMESTAMP, ForeignKey, UniqueConstraint, JSON
from sqlalchemy.sql import expression
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.core.encrypted_type import EncryptedString
from app.models import Base


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

    user_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # NULL for PLATFORM_ADMIN (lives outside any tenant)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=True)

    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="AGENT")  # PLATFORM_ADMIN / SUPER_ADMIN / ADMIN / MANAGER / AGENT
    status = Column(String(50), default="ACTIVE")  # ACTIVE / INACTIVE / SUSPENDED

    # Auth fields
    password_hash = Column(String(255), nullable=True)  # null for Google-only users
    auth_provider = Column(String(50), default="local")  # local / google
    google_id = Column(String(255), nullable=True, unique=True)
    email_verified = Column(Boolean, default=False)
    avatar_url = Column(String(500), nullable=True)
    invited_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)

    # Granular feature permissions (JSON list of permission keys).
    # null = use role defaults. Set explicitly by SUPER_ADMIN to override defaults.
    custom_permissions = Column(JSON, nullable=True, default=None)

    last_login_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Sales hierarchy (BR-SH-01/02): level 1 CEO / COO / Sales Head, 2 Business
    # Development, 3 Business Executive, 4 Market Research. NULL = not in the
    # hierarchy (visibility then follows the role, as before). manager_id is the
    # user one level up; a user sees their own records and everyone's below them.
    sales_level = Column(Integer, nullable=True)
    manager_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    # Also send in-app notifications by email (BR-SF-07)
    notify_email = Column(Boolean, nullable=False, default=True)

    # Two-factor sign-in (TOTP, app/core/totp.py; migration 0004_user_mfa). mfa_secret is encrypted at
    # rest and set by /api/auth/mfa/setup before mfa_enabled turns on. mfa_recovery_codes is a JSON list
    # of SHA-256 hashes of the unused recovery codes. mfa_last_step is the last accepted TOTP time step
    # (a code is never accepted twice).
    mfa_enabled = Column(Boolean, nullable=False, default=False, server_default=expression.false())
    mfa_secret = Column(EncryptedString, nullable=True)
    mfa_recovery_codes = Column(Text, nullable=True)
    mfa_enabled_at = Column(DateTime, nullable=True)
    mfa_last_step = Column(BigInteger, nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="users")
    created_campaigns = relationship("Campaign", back_populates="creator", lazy="dynamic")
    uploaded_lists = relationship("ProspectList", back_populates="uploader", lazy="dynamic")
    approved_templates = relationship("EmailTemplate", back_populates="approver", lazy="dynamic")
    template_versions = relationship("EmailTemplateVersion", back_populates="creator", lazy="dynamic")
    owned_groups = relationship("CampaignGroup", back_populates="owner", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_user_tenant_email"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"
