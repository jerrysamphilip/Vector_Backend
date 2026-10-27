# app/models/contact_scoring.py
"""
Per-workspace contact scoring model (BR-CM-43): fit rules, engagement weights, decay,
weights and grade thresholds as one JSON document (see app/services/scoring_engine.py),
plus the state of the last "recompute all" request.

The scores themselves live on prospects (score, fit_score, engagement_score, score_grade,
scored_at) so the contact list can sort and filter on an index.
"""
from sqlalchemy import JSON, Column, ForeignKey, Integer, String

from app.models.base import Base
from app.models.types import NOW6, UtcDateTime


class ContactScoringModel(Base):
    __tablename__ = "contact_scoring_models"

    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), primary_key=True)
    model = Column(JSON, nullable=False)
    # Set by POST /contacts/scoring-model/recompute (and on every model change); cleared once a
    # full recompute that started after it has finished
    recompute_requested_at = Column(UtcDateTime, nullable=True)
    last_recomputed_at = Column(UtcDateTime, nullable=True)
    last_recompute_count = Column(Integer, nullable=True)
    updated_by = Column(String(36), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)

    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)

    def __repr__(self) -> str:
        return f"<ContactScoringModel tenant={self.tenant_id}>"
