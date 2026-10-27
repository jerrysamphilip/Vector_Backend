# app/models/persona_blueprint.py
"""
Persona Blueprint model for AI email generation.
Stores generation recipes (openers, value angles, CTAs) per persona type.
"""

from sqlalchemy import Column, String, Boolean, JSON
from sqlalchemy.orm import relationship
import uuid

from app.models import Base
from app.models.types import NOW6, UtcDateTime


class PersonaBlueprint(Base):
    """
    Blueprint = Generation Recipe for a persona type.
    Contains multiple options for each email component.
    """
    __tablename__ = "persona_blueprints"

    blueprint_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    persona_type = Column(String(100), unique=True, nullable=False)  # "MARKET_ACCESS", "OPERATIONS_PHARMACY"
    
    # Generation Recipe (JSON arrays of options)
    openers = Column(JSON, nullable=True)       # ["opener1", "opener2", ...]
    value_angles = Column(JSON, nullable=True)  # ["angle1", "angle2", ...]
    ctas = Column(JSON, nullable=True)          # ["cta1", "cta2", ...]
    proof_points = Column(JSON, nullable=True)  # Optional social proof examples
    
    # Tone configuration
    tone_rules = Column(JSON, nullable=True)    # {"style": "technical", "avoid": [...]}
    
    # Metadata
    created_at = Column(UtcDateTime, server_default=NOW6)
    updated_at = Column(UtcDateTime, server_default=NOW6, onupdate=NOW6)
    is_active = Column(Boolean, default=True)

    # Relationships
    prospect_personas = relationship("ProspectPersona", back_populates="blueprint", lazy="dynamic")

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<PersonaBlueprint {self.persona_type}>"
