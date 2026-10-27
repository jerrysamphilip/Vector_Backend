# app/models/sales.py
"""
Phase 2 sales objects (BRD v2.0 sections 5.6 and 5.7): leads with an SQL
queue, configurable sales stages, opportunities (deals) and proposals.

Every lead and opportunity has exactly one owner and one stage; visibility
follows the owner through the sales hierarchy (app/services/contact_service.py).

Typed SQLAlchemy 2 declarations (``Mapped[]``). The DDL is pinned by
tests/unit/test_a3_model_ddl.py. The status vocabularies live in app/domain/sales.py
and are re-exported here for existing importers.
"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.sales import (CLIENT_TYPES, DEFAULT_SALES_STAGES, LEAD_STAGES, OPEN_LEAD_STAGES,
                              PROPOSAL_STATUSES, SQL_CRITERIA)
from app.models.base import Base
from app.models.types import NOW6, UtcDateTime

__all__ = ["CLIENT_TYPES", "DEFAULT_SALES_STAGES", "LEAD_STAGES", "OPEN_LEAD_STAGES", "PROPOSAL_STATUSES",
           "SQL_CRITERIA", "Lead", "Opportunity", "Proposal", "SalesStage"]


def _uuid():
    return str(uuid.uuid4())


class Lead(Base):
    __tablename__ = "leads"

    lead_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id: Mapped[str] = mapped_column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("accounts.account_id"), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False, default="NEW")
    # {"budget": true, ..., "notes": "..."}
    qualification: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    next_step: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    next_step_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    disqualified_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # a disqualified lead reopens on this date (BR-SF-02)
    recycle_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    opportunity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"), nullable=True)
    stage_changed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    qualified_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)    # reached SQL
    converted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)
    # T-3, app/core/concurrency.py
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        Index("ix_leads_tenant_stage", "tenant_id", "stage"),
        Index("ix_leads_owner", "owner_id"),
        Index("ix_leads_prospect", "prospect_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SalesStage(Base):
    __tablename__ = "sales_stages"

    stage_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    probability: Mapped[int] = mapped_column(Integer, nullable=False, default=0)   # percent
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_won: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_lost: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_sales_stages_tenant", "tenant_id", "sort_order"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    opportunity_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("accounts.account_id"), nullable=True)
    prospect_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("prospects.prospect_id"),
                                                       nullable=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("leads.lead_id"), nullable=True)
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=False)
    stage_id: Mapped[str] = mapped_column(String(36), ForeignKey("sales_stages.stage_id"), nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    close_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    # NEW / EXISTING (BR-SP-04)
    client_type: Mapped[str] = mapped_column(String(10), nullable=False, default="NEW")
    # OPEN / WON / LOST, from the stage
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="OPEN")
    closed_reason: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    next_step: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Forecast category (BR-SF-08): PIPELINE / BEST_CASE / COMMIT / CLOSED / OMITTED. Follows the
    # stage's probability unless a user set it by hand (forecast_category_manual).
    forecast_category: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    forecast_category_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The campaign that sourced the deal, for campaign ROI (BR-SF-11)
    campaign_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("campaigns.campaign_id"),
                                                       nullable=True)
    closed_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)
    # T-3, app/core/concurrency.py
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        Index("ix_opps_tenant_status_close", "tenant_id", "status", "close_date"),
        Index("ix_opps_owner", "owner_id"),
        Index("ix_opps_account", "account_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class Proposal(Base):
    __tablename__ = "proposals"

    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(36), ForeignKey("opportunities.opportunity_id"),
                                                nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    valid_until: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6)
    updated_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True, server_default=NOW6,
                                                           onupdate=NOW6)
    # T-3, app/core/concurrency.py
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Discount approval (BR-SF-17): NOT_REQUIRED / PENDING / APPROVED / REJECTED (app/services/quote_approval.py)
    approval_status: Mapped[str] = mapped_column(String(20), nullable=False, default="NOT_REQUIRED",
                                                 server_default="NOT_REQUIRED")
    # discount submitted / approved
    approval_discount_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)
    approval_requested_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    approval_requested_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    approval_decided_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    approval_decided_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime, nullable=True)
    # approver's reason / submit note
    approval_reason: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)

    __table_args__ = (
        Index("ix_proposals_tenant_status", "tenant_id", "status"),
        Index("ix_proposals_opp", "opportunity_id"),
        Index("ix_proposals_tenant_approval", "tenant_id", "approval_status"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
