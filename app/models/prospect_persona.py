# app/models/prospect_persona.py
"""
ProspectPersona model - stores AI-inferred persona data for each prospect.
Links to Prospect (1:1) without modifying the Prospect model.
"""

from sqlalchemy import Column, String, Float, ForeignKey, JSON, Index
from sqlalchemy.orm import relationship

from app.models import Base
from app.models.types import NOW6, UtcDateTime
from app.core.ids import new_id, short_id


class ProspectPersona(Base):
    """
    AI-inferred persona for a prospect.
    Stores classification results without modifying original Prospect data.
    """
    __tablename__ = "prospect_personas"

    persona_id = Column(String(36), primary_key=True, default=new_id)
    prospect_id = Column(String(36), ForeignKey("prospects.prospect_id"), unique=True, nullable=False)
    # The contact's tenant (D3, 0028_child_tenant_ids); filled from the contact by app/models/tenant_fill.py
    tenant_id = Column(String(36), nullable=False)
    blueprint_id = Column(String(36), ForeignKey("persona_blueprints.blueprint_id"), nullable=True)
    
    # Classification result
    persona_type = Column(String(100), nullable=False)  # "MARKET_ACCESS", "OPERATIONS_PHARMACY", etc.
    confidence_score = Column(Float, default=1.0)       # 0.0 - 1.0
    classification_method = Column(String(50), default="RULE_BASED")  # "RULE_BASED" or "LLM"
    
    # Optional: LLM-inferred details (for future use)
    inferred_pain_points = Column(JSON, nullable=True)
    
    # Metadata
    classified_at = Column(UtcDateTime, server_default=NOW6)

    # Relationships
    prospect = relationship("Prospect", backref="persona", uselist=False)
    blueprint = relationship("PersonaBlueprint", back_populates="prospect_personas")

    __table_args__ = (
        Index("ix_prospect_personas_tenant_prospect", "tenant_id", "prospect_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<ProspectPersona {self.persona_type} for {short_id(self.prospect_id)}...>"
