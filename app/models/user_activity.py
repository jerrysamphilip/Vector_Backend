# app/models/user_activity.py
"""Days on which a user signed in or used the API, for CRM adoption (BRD v2.0 section 8).
Written by app/services/user_activity.py; table created by alembic 0014_crm_kpis."""
from sqlalchemy import Column, Date, ForeignKey, Index, String

from app.models.base import Base


class UserActivityDay(Base):
    __tablename__ = "user_activity_days"

    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    day = Column(Date, primary_key=True)  # UTC

    __table_args__ = (
        Index("ix_user_activity_days_tenant_day", "tenant_id", "day"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
