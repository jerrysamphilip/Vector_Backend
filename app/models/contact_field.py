# app/models/contact_field.py
"""
Tenant-defined custom fields for contacts. Values live in prospects.custom_fields
(a JSON object keyed by field_key).
"""

from sqlalchemy import Column, String, Integer, TIMESTAMP, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.sql import func
import uuid

from app.models.base import Base

FIELD_TYPES = ("TEXT", "NUMBER", "DATE", "SELECT", "URL")


class ContactFieldDefinition(Base):
    __tablename__ = "contact_field_definitions"

    field_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)

    field_key = Column(String(64), nullable=False)
    label = Column(String(100), nullable=False)
    field_type = Column(String(20), nullable=False, default="TEXT")
    options = Column(JSON, nullable=True)  # choices for SELECT
    sort_order = Column(Integer, default=0)

    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "field_key", name="uq_contact_field_key"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
