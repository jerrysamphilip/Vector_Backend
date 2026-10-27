# app/models/sales_extra.py
"""
Phase 2 "Should" features (BRD v2.0 section 5.12, BR-SF-01 .. BR-SF-16):
notifications, workflow rules, sales settings, targets, the email template
library, saved custom reports, products and quote lines, and per-user
Google / Microsoft connections for calendar and email sync.

Typed SQLAlchemy 2 declarations (``Mapped[]``); the DDL is pinned by tests/unit/test_a3_model_ddl.py.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encrypted_type import EncryptedText
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


def _uuid():
    return str(uuid.uuid4())


class Notification(Base):
    """In-app notification, also emailed when the user has email notifications on (BR-SF-07)."""
    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    # WORKFLOW / STALE_DEAL / LEAD_RECYCLED / ASSIGNED / ...
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)   # app path, e.g. /app/deals/<id>
    # suppress repeats (e.g. one stale alert per deal per week)
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    emailed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read_at", "created_at"),
        Index("ix_notifications_dedupe", "user_id", "dedupe_key"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class WorkflowRule(Base):
    """When a field changes on a lead, deal or contact: create a task, alert someone or set a field (BR-SF-13)."""
    __tablename__ = "workflow_rules"

    rule_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    object_type: Mapped[str] = mapped_column(String(10), nullable=False)   # LEAD / DEAL / CONTACT
    field: Mapped[str] = mapped_column(String(60), nullable=False)          # e.g. stage, stage_id, amount, created
    # changes / equals / greater_than
    operator: Mapped[str] = mapped_column(String(20), nullable=False, default="changes")
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # [{"type": "create_task", ...}, {"type": "notify", ...}]
    actions: Mapped[Any] = mapped_column(JSON, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        Index("ix_workflow_rules_tenant", "tenant_id", "object_type", "active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SalesSetting(Base):
    """Per-workspace sales settings: lead assignment, reasons, stale-deal days, amount visibility."""
    __tablename__ = "sales_settings"

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), primary_key=True)
    settings: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)


class SalesTarget(Base):
    """Revenue target per rep per fiscal quarter (BR-SF-08)."""
    __tablename__ = "sales_targets"

    target_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    fy: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)           # 1-4
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    set_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)

    __table_args__ = (
        Index("uq_sales_targets", "tenant_id", "user_id", "fy", "quarter", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class MessageTemplate(Base):
    """Reusable email template with merge fields (BR-SF-10)."""
    __tablename__ = "message_templates"

    template_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)

    __table_args__ = (
        Index("ix_message_templates_tenant", "tenant_id", "category"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SavedReport(Base):
    """A manager-built report: object, filters, grouping and measure (BR-SF-14)."""
    __tablename__ = "saved_reports"

    report_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    definition: Mapped[Any] = mapped_column(JSON, nullable=False)
    shared: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)


class Product(Base):
    """Price-book item for quotes (BR-SF-15)."""
    __tablename__ = "products"

    product_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    sku: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    unit: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)   # e.g. seat / month, project
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        Index("ix_products_tenant", "tenant_id", "active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class ProposalLine(Base):
    """Quote line on a proposal; the proposal amount is the sum of its lines (BR-SF-15)."""
    __tablename__ = "proposal_lines"

    line_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    proposal_id: Mapped[str] = mapped_column(String(36), ForeignKey("proposals.proposal_id"), nullable=False,
                                             index=True)
    product_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("products.product_id"), nullable=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=1)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    discount_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class UserConnection(Base):
    """A user's own Google or Microsoft account, for calendar and email sync (BR-SF-16)."""
    __tablename__ = "user_connections"

    connection_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)       # GOOGLE / MICROSOFT
    account_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    access_token: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(EncryptedText, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    sync_email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sync_calendar: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        Index("uq_user_connections", "user_id", "provider", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class ForecastSnapshot(Base):
    """The forecast (closed won + weighted open pipeline closing in the period) per owner,
    taken at the start of a month or fiscal quarter, for forecast accuracy (BRD section 8)."""
    __tablename__ = "forecast_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)    # MONTH / QUARTER
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)           # exclusive
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"),
                                          nullable=False)
    won: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    weighted: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    forecast: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False, default=0)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Committed forecast (BR-SF-08, BRD section 8 forecast accuracy): open deals in the Commit
    # category closing in the period, and closed won + that. NULL on snapshots from before 0014.
    commit_open: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    committed: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    # SCHEDULED / MANUAL
    source: Mapped[str] = mapped_column(String(10), nullable=False, default="SCHEDULED")
    taken_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    taken_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)

    __table_args__ = (
        Index("uq_forecast_snapshots", "tenant_id", "period_type", "period_start", "owner_id", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
