"""
OAuth / Microsoft Graph (and Google API) HTTP behind a Protocol (ADR 0009 adapter layer, chunk S2).

Used by app/services/ms365_oauth.py (Microsoft 365 mailbox sign-in, token refresh) and
app/services/account_sync.py (Google / Microsoft account connections: token exchange, Gmail, Graph
mail and calendar). Only the transport: the same httpx calls and timeouts as before (token posts 20 s,
API requests 30 s with a bearer token). Unit tests swap in a fake through
``app.adapters.providers.override(oauth_http=...)``.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol

import httpx

TOKEN_TIMEOUT_SECONDS = 20
API_TIMEOUT_SECONDS = 30


class HttpResponse(Protocol):
    """The part of ``httpx.Response`` the callers read."""

    status_code: int

    @property
    def headers(self) -> Mapping[str, str]: ...

    def json(self) -> Any: ...


class OAuthHttp(Protocol):
    def post_form(self, url: str, data: Mapping[str, Any], *, timeout: float = TOKEN_TIMEOUT_SECONDS) -> Any:
        """POST a form (token endpoints); returns the response (``HttpResponse``)."""
        ...

    def bearer_request(self, method: str, url: str, token: str, *, params: Optional[dict] = None,
                       json: Optional[dict] = None, timeout: float = API_TIMEOUT_SECONDS) -> Any:
        """An API call authorised with ``Authorization: Bearer <token>``."""
        ...


class HttpxOAuthHttp:
    def post_form(self, url: str, data: Mapping[str, Any], *, timeout: float = TOKEN_TIMEOUT_SECONDS) -> httpx.Response:
        return httpx.post(url, data=dict(data), timeout=timeout)

    def bearer_request(self, method: str, url: str, token: str, *, params: Optional[dict] = None,
                       json: Optional[dict] = None, timeout: float = API_TIMEOUT_SECONDS) -> httpx.Response:
        return httpx.request(method, url, params=params, json=json, timeout=timeout,
                             headers={"Authorization": f"Bearer {token}"})
