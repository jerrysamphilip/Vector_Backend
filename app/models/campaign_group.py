# app/models/campaign_group.py
"""
Campaign Group models for grouping campaigns with shared limits.
"""

from sqlalchemy import Column, String, Integer, TIMESTAMP, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import uuid

from app.models import Base


class CampaignGroup(Base):
    """
    Group of campaigns that share cooling periods and sending limits.
    """
    __tablename__ = "campaign_groups"

    group_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    
    group_name = Column(String(255), nullable=False)
    owner_id = Column(String(36), ForeignKey("users.user_id"), nullable=False)
    
    # Shared Limits
    cooling_period_hours = Column(Integer, default=24)
    max_emails_per_day = Column(Integer, default=500)
    
    created_at = Column(TIMESTAMP, server_default=func.now())

    # Relationships
    tenant = relationship("Tenant", back_populates="campaign_groups")
    owner = relationship("User", back_populates="owned_groups")
    members = relationship("CampaignGroupMember", back_populates="group", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CampaignGroup {self.group_name}>"


class CampaignGroupMember(Base):
    """
    Association between campaigns and groups.
    """
    __tablename__ = "campaign_group_members"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    group_id = Column(String(36), ForeignKey("campaign_groups.group_id"), nullable=False)
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id"), nullable=False)

    # Relationships
    group = relationship("CampaignGroup", back_populates="members")
    campaign = relationship("Campaign", back_populates="group_memberships")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self):
        return f"<CampaignGroupMember group={self.group_id} campaign={self.campaign_id}>"
