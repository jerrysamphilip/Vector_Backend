# app/models/account.py
"""
Account (company) records. Contacts link to an account instead of only
carrying company_name as free text.

Typed SQLAlchemy 2 model (ADR 0009, chunk A1): the DDL is pinned by tests/unit/test_a1a_model_ddl.py.
``custom_fields`` holds company custom property values keyed by field_key (C-16, migration
0024_company_properties); definitions are ContactFieldDefinition rows with object_type COMPANY.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from sqlalchemy import JSON, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

if TYPE_CHECKING:
    from app.models.user import User


class Account(Base):
    __tablename__ = "accounts"

    account_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False, index=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    industry: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    emp_band: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    street: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    annual_revenue: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 2), nullable=True)
    lifecycle_stage: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    # Soft delete with 90-day restore (BR-CM-35)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    deleted_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    owner_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)

    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, server_default=NOW6, onupdate=NOW6,
                                                           nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                             server_default="1")  # T-3, app/core/concurrency.py

    # Company custom property values (C-16): {field_key: value}
    custom_fields: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    owner: Mapped[Optional["User"]] = relationship("User", foreign_keys=[owner_id])
    contacts = relationship("Prospect", back_populates="account", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_account_tenant_name"),
        UniqueConstraint("tenant_id", "domain", name="uq_account_tenant_domain"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<Account {self.name}>"
