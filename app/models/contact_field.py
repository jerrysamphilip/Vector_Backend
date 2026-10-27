# app/models/contact_field.py
"""
Tenant-defined custom fields for contacts. Values live in prospects.custom_fields
(a JSON object keyed by field_key).
"""

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

FIELD_TYPES = ("TEXT", "NUMBER", "DATE", "SELECT", "RADIO", "MULTI_CHECKBOX", "PHONE", "URL")


class ContactFieldDefinition(Base):
    __tablename__ = "contact_field_definitions"

    field_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    field_key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    field_type: Mapped[str] = mapped_column(String(20), nullable=False, default="TEXT")
    options: Mapped[Any] = mapped_column(JSON, nullable=True)  # choices for SELECT / RADIO / MULTI_CHECKBOX
    group_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)  # property group (BR-CM-10)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")  # required on create
    sort_order: Mapped[Optional[int]] = mapped_column(Integer, default=0, nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "field_key", name="uq_contact_field_key"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
