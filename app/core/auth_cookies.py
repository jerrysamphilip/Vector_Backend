# app/core/auth_cookies.py
"""
Browser session cookies and CSRF (double-submit) for the SPA.

The web app sends "X-Auth-Mode: cookie" on every request. Token-issuing endpoints then set

  access_token   httpOnly, SameSite=Lax,    path=/,                    max-age = access TTL
  refresh_token  httpOnly, SameSite=Strict, path=REFRESH_COOKIE_PATH,  max-age = refresh TTL
  csrf_token     readable, SameSite=Lax,    path=/,                    max-age = refresh TTL

(Secure when COOKIE_SECURE, default: production) and leave the tokens out of the JSON body, so
JavaScript never sees them. Clients that don't send the header (scripts, the test suite) keep
getting the tokens in the body and no cookies, and authenticate with "Authorization: Bearer".

A request authenticated by the access_token cookie that changes state (not GET/HEAD/OPTIONS)
must echo the csrf_token cookie in the X-CSRF-Token header; Bearer requests need no CSRF token.
"""

import hmac
import secrets

from fastapi import HTTPException, Request, Response, status

from app.core.config import settings

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
AUTH_MODE_HEADER = "X-Auth-Mode"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def wants_cookies(request: Request) -> bool:
    return (request.headers.get(AUTH_MODE_HEADER) or "").strip().lower() == "cookie"


def _common() -> dict:
    kwargs = {"secure": settings.cookie_secure}
    if settings.COOKIE_DOMAIN:
        kwargs["domain"] = settings.COOKIE_DOMAIN
    return kwargs


def _refresh_path() -> str:
    return (settings.REFRESH_COOKIE_PATH or "/").strip() or "/"


def set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    common = _common()
    refresh_age = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    response.set_cookie(ACCESS_COOKIE, access_token, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
                        path="/", httponly=True, samesite="lax", **common)
    response.set_cookie(REFRESH_COOKIE, refresh_token, max_age=refresh_age,
                        path=_refresh_path(), httponly=True, samesite="strict", **common)
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), max_age=refresh_age,
                        path="/", httponly=False, samesite="lax", **common)


def clear_auth_cookies(response: Response) -> None:
    common = _common()
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, samesite="lax", **common)
    response.delete_cookie(REFRESH_COOKIE, path=_refresh_path(), httponly=True, samesite="strict", **common)
    response.delete_cookie(CSRF_COOKIE, path="/", httponly=False, samesite="lax", **common)


def csrf_valid(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    return bool(cookie) and bool(header) and hmac.compare_digest(cookie.encode(), header.encode())


def require_csrf(request: Request) -> None:
    """403 unless a state-changing request carries X-CSRF-Token equal to the csrf_token cookie."""
    if request.method.upper() in SAFE_METHODS:
        return
    if not csrf_valid(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing or invalid")
