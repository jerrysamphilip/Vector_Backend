"""Revoked access tokens (sign-out).

Access JWTs are stateless and live ACCESS_TOKEN_EXPIRE_MINUTES. Signing out revokes the refresh token
and adds the access token's jti here, so a copy of that access token (cookie or bearer) is refused
from then on instead of staying valid until it expires. Only that one token is revoked: the user's
other sessions / devices are untouched. Rows are only needed until the token's own expiry; expired
rows are pruned whenever a token is revoked.

Table created by alembic 0013_revoked_access_tokens.
"""
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.clock import utcnow


def is_revoked(db: Session, jti: str | None) -> bool:
    if not jti:
        return False  # tokens issued before jti existed: they expire on their own
    return db.execute(text("SELECT 1 FROM revoked_access_tokens WHERE jti = :j"), {"j": jti}).first() is not None


def revoke(db: Session, payload: dict) -> None:
    """Revoke the access token whose decoded payload is given (no-op without jti). Commits."""
    jti = payload.get("jti")
    if not jti:
        return
    exp = payload.get("exp")
    expires_at = (datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None)
                  if isinstance(exp, (int, float)) else utcnow())
    db.execute(text("DELETE FROM revoked_access_tokens WHERE expires_at < :now"), {"now": utcnow()})
    db.execute(text("INSERT IGNORE INTO revoked_access_tokens (jti, user_id, expires_at) VALUES (:j, :u, :e)"),
               {"j": jti, "u": payload.get("sub"), "e": expires_at})
    db.commit()
