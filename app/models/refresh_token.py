# app/models/refresh_token.py
"""
Refresh token model for JWT session management.
"""

from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.sql import func
import uuid

from app.models import Base


class RefreshToken(Base):
    """
    Stores hashed refresh tokens for secure session renewal.
    Tokens are revoked on logout or password change.

    Session limits (app/services/session_limits.py, alembic 0015_session_limits): every token of
    one sign-in shares a session_id (the rotation family) and carries the sign-in time
    (session_started_at, absolute cap) and the last user-driven activity (last_activity_at, idle
    timeout) forward through rotation. revoked_reason says why a token stopped working
    ("rotated", "logout", "reuse", "idle", "absolute"; NULL for older / bulk revocations).
    """
    __tablename__ = "refresh_tokens"

    token_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    token_hash = Column(String(255), unique=True, nullable=False)
    device_info = Column(String(255), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    session_id = Column(String(36), nullable=True, index=True)
    session_started_at = Column(DateTime, nullable=True)
    last_activity_at = Column(DateTime, nullable=True)
    revoked_reason = Column(String(16), nullable=True)

    __table_args__ = (
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:
        return f"<RefreshToken user={self.user_id} revoked={self.revoked_at is not None}>"
