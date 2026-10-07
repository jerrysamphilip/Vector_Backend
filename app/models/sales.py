# app/models/sales.py
"""
Phase 2 sales objects (BRD v2.0 sections 5.6 and 5.7): leads with an SQL
queue, configurable sales stages, opportunities (deals) and proposals.

Every lead and opportunity has exactly one owner and one stage; visibility
follows the owner through the sales hierarchy (app/services/contact_service.py).
"""
import uuid

from sqlalchemy import (JSON, TIMESTAMP, Boolean, Column, Date, ForeignKey, Index, Integer, Numeric, String,
                        Text)
from sqlalchemy.sql import func

from app.models.base import Base

# Lead stages, in order, with the contact lifecycle stage each one implies (BR-LD-02)
LEAD_STAGES = {
    "NEW": ("New", "LEAD"),
    "CONTACTED": ("Contacted", "LEAD"),
    "ENGAGED": ("Engaged (MQL)", "MQL"),
    "SQL": ("Sales qualified (SQL)", "SQL"),
    "CONVERTED": ("Converted to opportunity", "OPPORTUNITY"),
    "DISQUALIFIED": ("Disqualified", None),
}
OPEN_LEAD_STAGES = ("NEW", "CONTACTED", "ENGAGED", "SQL")

# SQL qualification criteria (BR-LD-03): BANT. All must be met to move a lead to SQL.
SQL_CRITERIA = {
    "budget": "Budget confirmed",
    "authority": "Talking to the decision maker",
    "need": "Clear business need",
    "timeline": "Buying timeline known",
}

PROPOSAL_STATUSES = {
    "DRAFT": "Draft", "SENT": "Sent", "UNDER_REVIEW": "Under review",
    "ACCEPTED": "Accepted", "REJECTED": "Rejected",
}
CLIENT_TYPES = {"NEW": "New client", "EXISTING": "Existing client"}

# Seeded per workspace on first use; admins can rename, re-weight, add or retire stages
DEFAULT_SALES_STAGES = [
    ("Qualification", 10, False, False),
    ("Needs analysis", 25, False, False),
    ("Proposal", 50, False, False),
    ("Negotiation", 75, False, False),
    ("Closed won", 100, True, False),
    ("Closed lost", 0, False, True),
]


def _uuid():
    return str(uuid.uuid4())


class Lead(Base):
    __tablename__ = "leads"

    lead_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=False)
    account_id = Column(String(36), ForeignKey("accounts.account_id"), nullable=True)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    source = Column(String(100), nullable=True)
    stage = Column(String(20), nullable=False, default="NEW")
    qualification = Column(JSON, nullable=True)        # {"budget": true, ..., "notes": "..."}
    next_step = Column(String(255), nullable=True)
    next_step_at = Column(TIMESTAMP, nullable=True)
    disqualified_reason = Column(String(255), nullable=True)
    opportunity_id = Column(String(36), nullable=True)
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=True)
    stage_changed_at = Column(TIMESTAMP, server_default=func.now())
    qualified_at = Column(TIMESTAMP, nullable=True)    # reached SQL
    converted_at = Column(TIMESTAMP, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_leads_tenant_stage", "tenant_id", "stage"),
        Index("ix_leads_owner", "owner_id"),
        Index("ix_leads_prospect", "prospect_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class SalesStage(Base):
    __tablename__ = "sales_stages"

    stage_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name = Column(String(100), nullable=False)
    probability = Column(Integer, nullable=False, default=0)   # percent
    sort_order = Column(Integer, nullable=False, default=0)
    is_won = Column(Boolean, nullable=False, default=False)
    is_lost = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_sales_stages_tenant", "tenant_id", "sort_order"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class Opportunity(Base):
    __tablename__ = "opportunities"

    opportunity_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    name = Column(String(255), nullable=False)
    account_id = Column(String(36), ForeignKey("accounts.account_id"), nullable=True)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), nullable=True)
    lead_id = Column(String(36), ForeignKey("leads.lead_id"), nullable=True)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    stage_id = Column(String(36), ForeignKey("sales_stages.stage_id"), nullable=False)
    amount = Column(Numeric(15, 2), nullable=True)
    close_date = Column(Date, nullable=True)
    client_type = Column(String(10), nullable=False, default="NEW")   # NEW / EXISTING (BR-SP-04)
    status = Column(String(10), nullable=False, default="OPEN")       # OPEN / WON / LOST, from the stage
    closed_reason = Column(String(255), nullable=True)
    next_step = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    closed_at = Column(TIMESTAMP, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_opps_tenant_status_close", "tenant_id", "status", "close_date"),
        Index("ix_opps_owner", "owner_id"),
        Index("ix_opps_account", "account_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )


class Proposal(Base):
    __tablename__ = "proposals"

    proposal_id = Column(String(36), primary_key=True, default=_uuid)
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    opportunity_id = Column(String(36), ForeignKey("opportunities.opportunity_id"), nullable=False)
    title = Column(String(255), nullable=False)
    amount = Column(Numeric(15, 2), nullable=True)
    status = Column(String(20), nullable=False, default="DRAFT")
    valid_until = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    sent_at = Column(TIMESTAMP, nullable=True)
    decided_at = Column(TIMESTAMP, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(TIMESTAMP, server_default=func.now())
    updated_at = Column(TIMESTAMP, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_proposals_tenant_status", "tenant_id", "status"),
        Index("ix_proposals_opp", "opportunity_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )
