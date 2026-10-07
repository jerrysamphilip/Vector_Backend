# app/models/account.py
"""
Account (company) records. Contacts link to an account instead of only
carrying company_name as free text.
"""

from sqlalchemy import Column, String, Text, TIMESTAMP, ForeignKey, UniqueConstraint, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models.base import Base


class Account(Base):
    __tablename__ = "accounts"

    account_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    domain = Column(String(255), nullable=True)
    website = Column(String(500), nullable=True)
    phone = Column(String(50), nullable=True)
    industry = Column(String(100), nullable=True)
    emp_band = Column(String(50), nullable=True)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    street = Column(String(255), nullable=True)
    postal_code = Column(String(20), nullable=True)
    annual_revenue = Column(Numeric(18, 2), nullable=True)
    lifecycle_stage = Column(String(30), nullable=True)

    # Soft delete with 90-day restore (BR-CM-35)
    deleted_at = Column(TIMESTAMP, nullable=True)
    deleted_by = Column(String(36), nullable=True)

    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    owner = relationship("User", foreign_keys=[owner_id])
    contacts = relationship("Prospect", back_populates="account", lazy="dynamic")

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_account_tenant_name"),
        UniqueConstraint("tenant_id", "domain", name="uq_account_tenant_domain"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<Account {self.name}>"
