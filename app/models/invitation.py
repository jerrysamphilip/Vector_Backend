# app/models/invitation.py
"""
Team invitation model for user onboarding.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class Invitation(Base):
    """
    Invitation to join a tenant workspace.
    Status: PENDING / ACCEPTED / EXPIRED / REVOKED
    """
    __tablename__ = "invitations"

    invitation_id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    invited_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="AGENT")
    token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, default="PENDING")  # PENDING / ACCEPTED / EXPIRED / REVOKED
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Invitation {self.email} ({self.status})>"
