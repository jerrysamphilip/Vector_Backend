# app/models/invitation.py
"""
Team invitation model for user onboarding.
"""

from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.sql import func
import uuid

from app.models import Base


class Invitation(Base):
    """
    Invitation to join a tenant workspace.
    Status: PENDING / ACCEPTED / EXPIRED / REVOKED
    """
    __tablename__ = "invitations"

    invitation_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    invited_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    email = Column(String(255), nullable=False)
    role = Column(String(50), nullable=False, default="AGENT")
    token = Column(String(255), unique=True, nullable=False)
    status = Column(String(50), default="PENDING")  # PENDING / ACCEPTED / EXPIRED / REVOKED
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<Invitation {self.email} ({self.status})>"
