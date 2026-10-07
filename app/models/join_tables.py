
# app/models/join_tables.py
"""
Join tables for many-to-many relationships.
"""

from sqlalchemy import Table, Column, String, ForeignKey
from app.models.base import Base

# Many-to-Many association between Campaigns and SendingInboxes
campaign_inboxes = Table(
    "campaign_inboxes",
    Base.metadata,
    Column("campaign_id", String(36), ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), primary_key=True),
    Column("inbox_id", String(36), ForeignKey("sending_inboxes.inbox_id", ondelete="CASCADE"), primary_key=True),
)
