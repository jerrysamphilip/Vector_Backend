"""
One-time magic login tokens for direct-login links.
"""

import uuid

from sqlalchemy import Column, ForeignKey, String
from sqlalchemy.orm import relationship

from app.models import Base
from app.models.types import NOW6, UtcDateTime


class MagicLoginToken(Base):
    __tablename__ = "magic_login_tokens"

    token_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    token_hash = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(UtcDateTime, nullable=False)
    used_at = Column(UtcDateTime, nullable=True)
    created_at = Column(UtcDateTime, server_default=NOW6, nullable=False)

    user = relationship("User")

