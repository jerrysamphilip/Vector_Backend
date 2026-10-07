# app/models/prospect_list.py
"""
Prospect List models for CSV uploads and list management.
"""

from sqlalchemy import Column, String, Boolean, TIMESTAMP, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models import Base


class ProspectList(Base):
    """
    A list of prospects uploaded via CSV, CRM sync, or API.
    """
    __tablename__ = "prospect_lists"

    list_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    
    list_name = Column(String(255), nullable=False)
    source_type = Column(String(50), nullable=False, default="CSV")  # CSV / CRM / API / MANUAL / IMPORT
    # STATIC lists hold a fixed set of members; ACTIVE lists are defined by `filters`
    # and their membership is evaluated live (BR-CM-22/23)
    list_type = Column(String(10), nullable=False, default="STATIC", server_default="STATIC")
    filters = Column(JSON, nullable=True)
    description = Column(Text, nullable=True)
    
    uploaded_by = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    uploaded_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    tenant = relationship("Tenant", back_populates="prospect_lists")
    uploader = relationship("User", back_populates="uploaded_lists")
    members = relationship("ProspectListMember", back_populates="prospect_list", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<ProspectList {self.list_name}>"


class ProspectListMember(Base):
    """
    Association between prospects and lists.
    Tracks if prospect was newly created or already existed.
    """
    __tablename__ = "prospect_list_members"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    list_id = Column(String(36), ForeignKey("prospect_lists.list_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    
    is_new_prospect = Column(Boolean, default=True)
    added_at = Column(TIMESTAMP, server_default=func.now())
    notes = Column(Text, nullable=True)

    # Relationships
    prospect_list = relationship("ProspectList", back_populates="members")
    prospect = relationship("Prospect", back_populates="list_memberships")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<ProspectListMember list={self.list_id} prospect={self.prospect_id}>"
