# app/models/sales_extra.py
"""
Phase 2 "Should" features (BRD v2.0 section 5.12, BR-SF-01 .. BR-SF-16):
notifications, workflow rules, sales settings, targets, the email template
library, saved custom reports, products and quote lines, and per-user
Google / Microsoft connections for calendar and email sync.
"""
import uuid

from sqlalchemy import JSON, TIMESTAMP, Boolean, Column, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.sql import func

from app.core.encrypted_type import EncryptedText
from app.models.base import Base


def _uuid():
    return str(uuid.uuid4())


class Notification(Base):
    """In-app notification, also emailed when the user has email notifications on (BR-SF-07)."""
    __tablename__ = "notifications"

    notification_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    kind = Column(String(40), nullable=False)          # WORKFLOW / STALE_DEAL / LEAD_RECYCLED / ASSIGNED / ...
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=True)
    link = Column(String(500), nullable=True)          # app path, e.g. /app/deals/<id>
    dedupe_key = Column(String(120), nullable=True)    # suppress repeats (e.g. one stale alert per deal per week)
    read_at = Column(TIMESTAMP, nullable=True)
    emailed_at = Column(TIMESTAMP, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "read_at", "created_at"),
        Index("ix_notifications_dedupe", "user_id", "dedupe_key"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class WorkflowRule(Base):
    """When a field changes on a lead, deal or contact: create a task, alert someone or set a field (BR-SF-13)."""
    __tablename__ = "workflow_rules"

    rule_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name = Column(String(150), nullable=False)
    object_type = Column(String(10), nullable=False)   # LEAD / DEAL / CONTACT
    field = Column(String(60), nullable=False)          # e.g. stage, stage_id, amount, created
    operator = Column(String(20), nullable=False, default="changes")  # changes / equals / greater_than
    value = Column(String(255), nullable=True)
    actions = Column(JSON, nullable=False)              # [{"type": "create_task", ...}, {"type": "notify", ...}]
    active = Column(Boolean, nullable=False, default=True)
    run_count = Column(Integer, nullable=False, default=0)
    last_run_at = Column(TIMESTAMP, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("ix_workflow_rules_tenant", "tenant_id", "object_type", "active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SalesSetting(Base):
    """Per-workspace sales settings: lead assignment, reasons, stale-deal days, amount visibility."""
    __tablename__ = "sales_settings"

    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), primary_key=True)
    settings = Column(JSON, nullable=False)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class SalesTarget(Base):
    """Revenue target per rep per fiscal quarter (BR-SF-08)."""
    __tablename__ = "sales_targets"

    target_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    fy = Column(Integer, nullable=False)
    quarter = Column(Integer, nullable=False)           # 1-4
    amount = Column(Numeric(15, 2), nullable=False, default=0)
    set_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("uq_sales_targets", "tenant_id", "user_id", "fy", "quarter", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class MessageTemplate(Base):
    """Reusable email template with merge fields (BR-SF-10)."""
    __tablename__ = "message_templates"

    template_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name = Column(String(150), nullable=False)
    category = Column(String(60), nullable=True)
    subject = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    shared = Column(Boolean, nullable=False, default=True)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    usage_count = Column(Integer, nullable=False, default=0)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_message_templates_tenant", "tenant_id", "category"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SavedReport(Base):
    """A manager-built report: object, filters, grouping and measure (BR-SF-14)."""
    __tablename__ = "saved_reports"

    report_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    name = Column(String(150), nullable=False)
    description = Column(Text, nullable=True)
    definition = Column(JSON, nullable=False)
    shared = Column(Boolean, nullable=False, default=False)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())


class Product(Base):
    """Price-book item for quotes (BR-SF-15)."""
    __tablename__ = "products"

    product_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name = Column(String(200), nullable=False)
    sku = Column(String(60), nullable=True)
    description = Column(Text, nullable=True)
    unit_price = Column(Numeric(15, 2), nullable=False, default=0)
    unit = Column(String(30), nullable=True)            # e.g. seat / month, project
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("ix_products_tenant", "tenant_id", "active"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class ProposalLine(Base):
    """Quote line on a proposal; the proposal amount is the sum of its lines (BR-SF-15)."""
    __tablename__ = "proposal_lines"

    line_id = Column(String(36), primary_key=True, default=_uuid)
    proposal_id = Column(String(36), ForeignKey("proposals.proposal_id"), nullable=False, index=True)
    product_id = Column(String(36), ForeignKey("products.product_id"), nullable=True)
    description = Column(String(500), nullable=False)
    quantity = Column(Numeric(12, 2), nullable=False, default=1)
    unit_price = Column(Numeric(15, 2), nullable=False, default=0)
    discount_pct = Column(Numeric(5, 2), nullable=False, default=0)
    sort_order = Column(Integer, nullable=False, default=0)


class UserConnection(Base):
    """A user's own Google or Microsoft account, for calendar and email sync (BR-SF-16)."""
    __tablename__ = "user_connections"

    connection_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    provider = Column(String(20), nullable=False)       # GOOGLE / MICROSOFT
    account_email = Column(String(255), nullable=True)
    access_token = Column(EncryptedText, nullable=True)
    refresh_token = Column(EncryptedText, nullable=True)
    expires_at = Column(TIMESTAMP, nullable=True)
    sync_email = Column(Boolean, nullable=False, default=True)
    sync_calendar = Column(Boolean, nullable=False, default=True)
    last_sync_at = Column(TIMESTAMP, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

    __table_args__ = (
        Index("uq_user_connections", "user_id", "provider", unique=True),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
