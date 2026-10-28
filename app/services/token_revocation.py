"""Revoked access tokens (sign-out).

Access JWTs are stateless and live ACCESS_TOKEN_EXPIRE_MINUTES. Signing out revokes the refresh token
and adds the access token's jti here, so a copy of that access token (cookie or bearer) is refused
from then on instead of staying valid until it expires. Only that one token is revoked: the user's
other sessions / devices are untouched. Rows are only needed until the token's own expiry; expired
rows are pruned whenever a token is revoked.

Table created by alembic 0013_revoked_access_tokens; SQL in app/repositories/identity_tokens.py.
``is_revoked`` / ``revoke`` take a session (app.core.auth, legacy callers); services use
``revoke_payload`` with their unit of work's token repository.
"""
from datetime import datetime, timezone
from typing import Any

from app.core.clock import utcnow
from app.repositories.identity_tokens import SqlAlchemyTokenRepository, TokenRepository


def expiry_of(payload: dict) -> datetime:
    """The token's own expiry (naive UTC), now when the payload carries none."""
    exp = payload.get("exp")
    return (datetime.fromtimestamp(exp, tz=timezone.utc).replace(tzinfo=None)
            if isinstance(exp, (int, float)) else utcnow())


def revoke_payload(tokens: TokenRepository, payload: dict) -> bool:
    """Stage the revocation of the access token whose decoded payload is given; False without jti
    (tokens issued before jti existed expire on their own). Does not commit."""
    jti = payload.get("jti")
    if not jti:
        return False
    tokens.revoke_access_token(jti, payload.get("sub"), expiry_of(payload), utcnow())
    return True


def is_revoked(db: Any, jti: str | None) -> bool:
    if not jti:
        return False  # tokens issued before jti existed: they expire on their own
    return SqlAlchemyTokenRepository(db).access_token_revoked(jti)


def revoke(db: Any, payload: dict) -> None:
    """Revoke the access token whose decoded payload is given (no-op without jti). Commits."""
    if revoke_payload(SqlAlchemyTokenRepository(db), payload):
        db.commit()
