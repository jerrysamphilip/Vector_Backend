"""
Identity tokens (ADR 0009, chunk A5): refresh tokens (sessions, app/services/session_limits.py), one-time
magic-login and password-reset links, and the revoked access-token list (sign-out,
app/services/token_revocation.py).

Tokens are looked up by their SHA-256 hash (app.core.security.hash_token); the raw values never reach the
database. Nothing here commits; the caller's unit of work does.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol

from sqlalchemy import text

from app.models.magic_login_token import MagicLoginToken
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken

__all__ = ["SqlAlchemyTokenRepository", "TokenRepository"]


class TokenRepository(Protocol):
    def add_refresh(self, **fields: Any) -> RefreshToken: ...

    def refresh_for_update(self, token_hash: str) -> Optional[RefreshToken]: ...

    def active_refresh(self, token_hash: str) -> Optional[RefreshToken]: ...

    def revoke_session(self, session_id: str, now: datetime, reason: str) -> int: ...

    def revoke_user_sessions(self, user_id: str, now: datetime) -> int: ...

    def add_magic_link(self, user_id: str, token_hash: str, expires_at: datetime) -> MagicLoginToken: ...

    def unused_magic_link(self, token_hash: str) -> Optional[MagicLoginToken]: ...

    def spend_magic_links(self, user_id: str, now: datetime) -> int: ...

    def add_password_reset(self, user_id: str, token_hash: str, expires_at: datetime) -> PasswordResetToken: ...

    def unused_password_reset(self, token_hash: str) -> Optional[PasswordResetToken]: ...

    def access_token_revoked(self, jti: str) -> bool: ...

    def revoke_access_token(self, jti: str, user_id: Optional[str], expires_at: datetime, now: datetime) -> None: ...

    def delete_for_user(self, user_id: str) -> None: ...


class SqlAlchemyTokenRepository:
    def __init__(self, session: Any) -> None:
        self._session = session

    # ── Refresh tokens (one row per issued token; a sign-in's tokens share session_id) ──

    def add_refresh(self, **fields: Any) -> RefreshToken:
        token = RefreshToken(**fields)
        self._session.add(token)
        return token

    def refresh_for_update(self, token_hash: str) -> Optional[RefreshToken]:
        """The presented token, row-locked until the transaction ends (rotation is single-winner)."""
        return self._session.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash).with_for_update().first()

    def active_refresh(self, token_hash: str) -> Optional[RefreshToken]:
        return self._session.query(RefreshToken).filter(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
        ).first()

    def revoke_session(self, session_id: str, now: datetime, reason: str) -> int:
        """Revoke every still-active token of a session (the rotation family)."""
        return self._session.query(RefreshToken).filter(
            RefreshToken.session_id == session_id,
            RefreshToken.revoked_at.is_(None),
        ).update({RefreshToken.revoked_at: now, RefreshToken.revoked_reason: reason}, synchronize_session=False)

    def revoke_user_sessions(self, user_id: str, now: datetime) -> int:
        """Revoke every still-active refresh token of a user (password set / changed / reset)."""
        return self._session.query(RefreshToken).filter(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        ).update({"revoked_at": now})

    # ── Magic-login (set-password) links ──

    def add_magic_link(self, user_id: str, token_hash: str, expires_at: datetime) -> MagicLoginToken:
        link = MagicLoginToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(link)
        return link

    def unused_magic_link(self, token_hash: str) -> Optional[MagicLoginToken]:
        return self._session.query(MagicLoginToken).filter(
            MagicLoginToken.token_hash == token_hash,
            MagicLoginToken.used_at.is_(None),
        ).first()

    def spend_magic_links(self, user_id: str, now: datetime) -> int:
        """Mark the user's unused one-click sign-in links as used."""
        return self._session.query(MagicLoginToken).filter(
            MagicLoginToken.user_id == user_id, MagicLoginToken.used_at.is_(None)
        ).update({MagicLoginToken.used_at: now}, synchronize_session=False)

    # ── Password-reset links ──

    def add_password_reset(self, user_id: str, token_hash: str, expires_at: datetime) -> PasswordResetToken:
        record = PasswordResetToken(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
        self._session.add(record)
        return record

    def unused_password_reset(self, token_hash: str) -> Optional[PasswordResetToken]:
        return self._session.query(PasswordResetToken).filter(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
        ).first()

    # ── Revoked access tokens (alembic 0013_revoked_access_tokens) ──

    def access_token_revoked(self, jti: str) -> bool:
        return self._session.execute(
            text("SELECT 1 FROM revoked_access_tokens WHERE jti = :j"), {"j": jti}).first() is not None

    def revoke_access_token(self, jti: str, user_id: Optional[str], expires_at: datetime, now: datetime) -> None:
        """Add a token to the list; rows past their token's own expiry are pruned on the way."""
        self._session.execute(text("DELETE FROM revoked_access_tokens WHERE expires_at < :now"), {"now": now})
        self._session.execute(
            text("INSERT IGNORE INTO revoked_access_tokens (jti, user_id, expires_at) VALUES (:j, :u, :e)"),
            {"j": jti, "u": user_id, "e": expires_at})

    # ── Permanent user deletion ──

    def delete_for_user(self, user_id: str) -> None:
        for model in (MagicLoginToken, RefreshToken, PasswordResetToken):
            self._session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)
