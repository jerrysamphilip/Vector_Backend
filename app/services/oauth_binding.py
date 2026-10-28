"""Ties an OAuth sign-in to the browser that started it (login-CSRF protection).

The start endpoint sets a random nonce in an httpOnly cookie and puts the same
nonce in the signed state; the callback only accepts a state whose nonce matches
the cookie, so a sign-in link started by someone else can't be completed here.
"""
import hmac
import secrets
from collections.abc import Mapping
from typing import Any, Protocol

from app.core.config import settings

COOKIE = "oauth_nonce"
TTL_SECONDS = 900


class CookieWriter(Protocol):
    """What ``issue`` needs of the HTTP response (fastapi.Response satisfies it)."""

    def set_cookie(self, key: str, value: str = "", max_age: Any = None, *, httponly: bool = False,
                   samesite: Any = "lax", secure: bool = False, path: Any = "/") -> Any: ...


class CookieReader(Protocol):
    """What ``matches`` needs of the HTTP request (fastapi.Request satisfies it)."""

    @property
    def cookies(self) -> Mapping[str, str]: ...


def issue(response: CookieWriter) -> str:
    nonce = secrets.token_urlsafe(24)
    response.set_cookie(COOKIE, nonce, max_age=TTL_SECONDS, httponly=True, samesite="lax",
                        secure=settings.is_production, path="/")
    return nonce


def matches(request: CookieReader, state_nonce: Any) -> bool:
    cookie = request.cookies.get(COOKIE) or ""
    return bool(cookie and state_nonce) and hmac.compare_digest(cookie, str(state_nonce))


MISMATCH = "Finish the sign-in in the same browser you started it from, then try again."
