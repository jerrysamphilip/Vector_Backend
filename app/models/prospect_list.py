# app/models/prospect_list.py
"""
Contact lists (BR-CM-22/23/24): uploads, imports and lists made by hand.

STATIC lists hold their members in ``prospect_list_members``; ACTIVE lists are a saved filter
(``filters``) whose membership is evaluated live. List names are unique per workspace
(``uq_prospect_lists_tenant_name``, migration 0025; the column collation makes it case-insensitive).
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class ProspectList(Base):
    """
    A list of prospects uploaded via CSV, CRM sync, or API.
    """
    __tablename__ = "prospect_lists"

    list_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    list_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="CSV")  # CSV / CRM / API / MANUAL / IMPORT
    # STATIC lists hold a fixed set of members; ACTIVE lists are defined by `filters`
    # and their membership is evaluated live (BR-CM-22/23)
    list_type: Mapped[str] = mapped_column(String(10), nullable=False, default="STATIC", server_default="STATIC")
    filters: Mapped[Any] = mapped_column(JSON, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    uploaded_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    uploaded_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="prospect_lists")
    uploader = relationship("User", back_populates="uploaded_lists")
    members = relationship("ProspectListMember", back_populates="prospect_list", lazy="dynamic")

    __table_args__ = (
        # One list per name in a workspace (C-21); utf8mb4_0900_ai_ci compares without case or accents
        Index("uq_prospect_lists_tenant_name", "tenant_id", "list_name", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ProspectList {self.list_name}>"


class ProspectListMember(Base):
    """
    Association between prospects and lists.
    Tracks if prospect was newly created or already existed.
    """
    __tablename__ = "prospect_list_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    list_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospect_lists.list_id"), nullable=False)
    prospect_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)

    is_new_prospect: Mapped[Optional[bool]] = mapped_column(Boolean, default=True, nullable=True)
    added_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    prospect_list = relationship("ProspectList", back_populates="members")
    prospect = relationship("Prospect", back_populates="list_memberships")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ProspectListMember list={self.list_id} prospect={self.prospect_id}>"
