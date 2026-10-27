"""
Where a password-reset / invitation / magic sign-in link may point (F024).

An emailed link is built on the front end the request came from (its Origin or Referer header) only
when that origin is a known front end, or a local one on a developer machine. The origin is rebuilt
from its validated parts (scheme, lower-case host name, numeric port); nothing from the header is
copied verbatim, so ``http://evil.example\\@localhost`` (host name "localhost" to the parser, but
opened as evil.example by a browser) never ends up in a link. A raw value holding a backslash, an
``@``, white space or a control character is refused outright.

Local origins (localhost / 127.0.0.1 / 0.0.0.0) are accepted only when the process is not deployed
(``settings.is_local_environment``); a deployed API falls back to the configured front-end URL.
"""

from collections.abc import Mapping
from typing import Optional
from urllib.parse import urlsplit

from app.core.config import settings

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})
# Front-end origins a link may point back to (taken from the request)
KNOWN_ORIGINS = {
    "sales.neutrino-ai.com": "https://sales.neutrino-ai.com",
    "outreach360.neutrinoaistudio.com": "https://outreach360.neutrinoaistudio.com",
}
# Deployed front ends per APP_PROFILE, used when the configured URLs are all local
PROFILE_BASES = {
    "dev": "https://sales.neutrino-ai.com",
    "stage": "https://outreach360.neutrinoaistudio.com",
}
_MAX_HEADER_LENGTH = 2048
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-")


def _has_forbidden_chars(raw: str) -> bool:
    return any(ch in "\\@" or ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)


def parse_origin(raw_url: Optional[str]) -> Optional[tuple[str, str, Optional[int]]]:
    """(scheme, host, port) of an http(s) URL whose authority is exactly ``host[:port]``, else None."""
    if not raw_url or len(raw_url) > _MAX_HEADER_LENGTH or _has_forbidden_chars(raw_url):
        return None
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError:  # malformed (bad IPv6 literal, non-numeric or out-of-range port)
        return None
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("http", "https") or not host or not set(host) <= _HOST_CHARS:
        return None
    if port == 0:
        return None
    # The authority must be nothing but the host and port: no user info, no stray characters
    expected = host if port is None else f"{host}:{port}"
    if parts.netloc.lower() != expected:
        return None
    return scheme, host, port


def safe_origin(raw_url: Optional[str], allow_local: Optional[bool] = None) -> str:
    """A known front-end origin, or (local development only) a rebuilt local origin; "" otherwise.

    ``allow_local`` defaults to "this process is not deployed"."""
    parsed = parse_origin(raw_url)
    if parsed is None:
        return ""
    scheme, host, port = parsed
    if host in KNOWN_ORIGINS:
        return KNOWN_ORIGINS[host]
    if allow_local is None:
        allow_local = settings.is_local_environment
    if host in LOCAL_HOSTS and allow_local:
        return f"{scheme}://{host}" if port is None else f"{scheme}://{host}:{port}"
    return ""


def request_origin(headers: Mapping[str, str]) -> str:
    """The safe origin of a request: its Origin header, else its Referer ("" when neither is usable)."""
    return safe_origin(headers.get("origin")) or safe_origin(headers.get("referer"))


def is_local_url(url: str) -> bool:
    """True for an empty URL or one on a local host."""
    if not url:
        return True
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS


def resolve_base_url(backend_base: str, frontend_base: str, profile: str) -> str:
    """The front end a link points to when the request gives no usable origin: the profile's deployed
    front end (when every configured URL is local), else the first non-local configured URL."""
    profile_base = ""
    if is_local_url(backend_base) and is_local_url(frontend_base):
        profile_base = PROFILE_BASES.get(profile, "")
    frontend_non_local = bool(frontend_base) and not is_local_url(frontend_base)
    backend_non_local = bool(backend_base) and not is_local_url(backend_base)
    return (
        profile_base
        or (frontend_base if frontend_non_local else "")
        or (backend_base if backend_non_local else "")
        or frontend_base
        or backend_base
    )
