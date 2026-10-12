# app/models/company_profile.py
"""
Company Profile model for reusable sender/company details.
Scoped to a tenant (tenant_id). Legacy rows with NULL tenant_id are hidden.
"""

from sqlalchemy import Column, String, Boolean, Text, TIMESTAMP, ForeignKey
from sqlalchemy.sql import func
import uuid

from app.models import Base


class CompanyProfile(Base):
    """
    Reusable company profile for email campaigns.
    Stores sender details that can be selected when creating campaigns.
    """
    __tablename__ = "company_profiles"

    profile_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # Owning tenant. Nullable only for legacy rows created before tenant
    # scoping; those are never returned to any tenant.
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=True, index=True)
    
    # Profile metadata
    profile_name = Column(String(100), nullable=False)  # e.g., "Acme - Main"
    is_default = Column(Boolean, default=False)
    
    # Company details (for AI context and tokens)
    company_name = Column(String(200), nullable=False)  # {{our_company}}
    company_description = Column(Text, nullable=True)  # About the company for AI context
    # Physical postal address printed in every campaign email footer (CAN-SPAM).
    # The tenant's default profile is its sender identity (app/services/sender_identity.py).
    postal_address = Column(Text, nullable=True)
    
    # Sender details
    default_sender_name = Column(String(100), nullable=True)  # {{your_name}}
    default_cta_link = Column(String(500), nullable=True)  # {{calendar_link}}
    
    # Timestamps
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CompanyProfile {self.profile_name}>"
    
    def to_dict(self):
        return {
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "is_default": self.is_default,
            "company_name": self.company_name,
            "company_description": self.company_description,
            "postal_address": self.postal_address,
            "default_sender_name": self.default_sender_name,
            "default_cta_link": self.default_cta_link,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
