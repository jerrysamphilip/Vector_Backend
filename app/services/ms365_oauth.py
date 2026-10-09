"""
Microsoft 365 mailbox sign-in with OAuth 2.0 (BR-DF-05).

Microsoft 365 blocks password (basic) authentication for IMAP and SMTP, so
those mailboxes connect with the authorization-code flow and then sign in to
IMAP/SMTP using XOAUTH2. Needs an Azure app registration (Entra ID):

  - Redirect URI (Web): MS365_REDIRECT_URI, e.g.
    https://<host>/vector/api/inboxes/oauth/microsoft/callback
  - Delegated API permissions (Office 365 Exchange Online):
    IMAP.AccessAsUser.All, SMTP.Send, plus offline_access
  - A client secret -> MS365_CLIENT_SECRET; Application (client) ID -> MS365_CLIENT_ID
  - SMTP AUTH must be enabled for the mailbox in Exchange admin.

Tokens are stored encrypted on the inbox; the access token is refreshed
automatically shortly before it expires.
"""
import base64
import logging
import imaplib
import smtplib
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.sending_inbox import SendingInbox

logger = logging.getLogger(__name__)

AUTH_TYPE = "OAUTH_MS365"
SCOPES = ("offline_access https://outlook.office.com/IMAP.AccessAsUser.All "
          "https://outlook.office.com/SMTP.Send")
IMAP_HOST, IMAP_PORT = "outlook.office365.com", 993
SMTP_HOST, SMTP_PORT = "smtp.office365.com", 587
STATE_TTL_MINUTES = 15


class OAuthError(Exception):
    pass


class OAuthToken(str):
    """An access token passed where a password is expected; login helpers use XOAUTH2 for it."""


def is_configured() -> bool:
    return bool(settings.MS365_CLIENT_ID and settings.MS365_CLIENT_SECRET and settings.MS365_REDIRECT_URI)


def _endpoint(path: str) -> str:
    return f"https://login.microsoftonline.com/{settings.MS365_TENANT_ID or 'common'}/oauth2/v2.0/{path}"


def make_state(inbox_id: str, tenant_id: str, user_id: str, nonce: str = None) -> str:
    payload = {"inbox_id": inbox_id, "tenant_id": tenant_id, "user_id": user_id, "purpose": "ms365_oauth",
               "nonce": nonce,
               "exp": datetime.utcnow() + timedelta(minutes=STATE_TTL_MINUTES)}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def read_state(state: str) -> dict:
    try:
        data = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise OAuthError("The sign-in link has expired. Start the connection again.") from exc
    if data.get("purpose") != "ms365_oauth":
        raise OAuthError("Invalid sign-in state")
    return data


def authorize_url(inbox: SendingInbox, state: str) -> str:
    return _endpoint("authorize") + "?" + urlencode({
        "client_id": settings.MS365_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.MS365_REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "login_hint": inbox.email_address,
        "prompt": "select_account",
    })


def _token_request(data: dict) -> dict:
    data = {"client_id": settings.MS365_CLIENT_ID, "client_secret": settings.MS365_CLIENT_SECRET,
            "scope": SCOPES, **data}
    resp = httpx.post(_endpoint("token"), data=data, timeout=20)
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200 or "access_token" not in body:
        raise OAuthError(body.get("error_description") or body.get("error") or f"Token request failed ({resp.status_code})")
    return body


def _store(inbox: SendingInbox, tokens: dict) -> None:
    inbox.oauth_access_token = tokens["access_token"]
    if tokens.get("refresh_token"):
        inbox.oauth_refresh_token = tokens["refresh_token"]
    inbox.oauth_expires_at = datetime.utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))
    inbox.oauth_error = None


def complete_connection(db: Session, inbox: SendingInbox, code: str) -> None:
    """Exchange the authorization code and switch the inbox to Microsoft 365 OAuth."""
    tokens = _token_request({"grant_type": "authorization_code", "code": code,
                             "redirect_uri": settings.MS365_REDIRECT_URI})
    _store(inbox, tokens)
    inbox.auth_type = AUTH_TYPE
    inbox.provider = inbox.provider or "outlook"
    inbox.imap_host, inbox.imap_port = IMAP_HOST, IMAP_PORT
    inbox.smtp_host, inbox.smtp_port, inbox.smtp_use_ssl = SMTP_HOST, SMTP_PORT, False
    inbox.imap_username = inbox.imap_username or inbox.email_address
    inbox.smtp_username = inbox.smtp_username or inbox.email_address
    db.commit()


def access_token(db: Session, inbox: SendingInbox) -> OAuthToken:
    """A valid access token, refreshed if it expires within 5 minutes. Commits on refresh."""
    if inbox.oauth_access_token and inbox.oauth_expires_at and \
            inbox.oauth_expires_at > datetime.utcnow() + timedelta(minutes=5):
        return OAuthToken(inbox.oauth_access_token)
    if not inbox.oauth_refresh_token:
        raise OAuthError("Microsoft 365 is not connected for this mailbox. Reconnect it.")
    try:
        tokens = _token_request({"grant_type": "refresh_token", "refresh_token": inbox.oauth_refresh_token})
    except OAuthError as exc:
        inbox.oauth_error = f"Microsoft 365 sign-in expired: {exc}. Reconnect the mailbox."[:2000]
        db.commit()
        raise
    _store(inbox, tokens)
    db.commit()
    return OAuthToken(inbox.oauth_access_token)


def xoauth2(username: str, token: str) -> str:
    return f"user={username}\x01auth=Bearer {token}\x01\x01"


# ── Login helpers used by every IMAP/SMTP connection ──────────

def imap_login(mail: imaplib.IMAP4, username: str, secret: str) -> None:
    if isinstance(secret, OAuthToken):
        mail.authenticate("XOAUTH2", lambda _: xoauth2(username, secret).encode())
    else:
        mail.login(username, secret)


def smtp_login(server: smtplib.SMTP, username: str, secret: str) -> None:
    if isinstance(secret, OAuthToken):
        auth = base64.b64encode(xoauth2(username, secret).encode()).decode()
        code, resp = server.docmd("AUTH", f"XOAUTH2 {auth}")
        if code != 235:
            raise smtplib.SMTPAuthenticationError(code, resp)
    else:
        server.login(username, secret)


def imap_secret(db: Session, inbox: SendingInbox) -> Optional[str]:
    """What to sign in to IMAP with: an OAuth token for Microsoft 365 mailboxes, else the password."""
    if (inbox.auth_type or "PASSWORD") == AUTH_TYPE:
        return access_token(db, inbox)
    return inbox.imap_password


def smtp_secret(db: Session, inbox: SendingInbox) -> Optional[str]:
    if (inbox.auth_type or "PASSWORD") == AUTH_TYPE:
        return access_token(db, inbox)
    return inbox.smtp_password
