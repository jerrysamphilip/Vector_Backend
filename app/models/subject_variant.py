# app/models/subject_variant.py
"""
Subject-line variants for A/B testing a sequence step (BR-SF-18 / RP-104).

A step carries 2-4 variants (A..D). Each enrolled prospect's email for the step
is assigned one variant when it is pre-scheduled (email_messages.subject_variant)
and is sent with that variant's subject. Once the owner promotes the winner, the
step's remaining unsent emails switch to the winner's subject.
"""

import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Integer, String, UniqueConstraint

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class EmailSubjectVariant(Base):
    __tablename__ = "email_subject_variants"

    variant_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    campaign_id = Column(String(36), ForeignKey("campaigns.campaign_id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_id = Column(String(36), ForeignKey("email_sequences.sequence_id", ondelete="CASCADE"), nullable=False)

    label = Column(String(1), nullable=False)          # A / B / C / D
    subject = Column(String(500), nullable=False)
    weight = Column(Integer, nullable=False, default=1)  # relative share of traffic, 1-100

    is_winner = Column(Boolean, nullable=False, default=False)  # set when promoted
    promoted_at = Column(UtcDateTime, nullable=True)
    promoted_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)

    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = (
        UniqueConstraint("sequence_id", "label", name="uq_subject_variants_sequence_label"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<EmailSubjectVariant {self.label} {self.subject[:30]!r}>"
