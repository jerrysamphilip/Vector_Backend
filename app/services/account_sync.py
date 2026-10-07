"""
Calendar and email sync with Gmail / Google Calendar and Outlook / Microsoft 365
(BRD v2.0 BR-SF-16).

Each user connects their own account (OAuth). Every 10 minutes the sync reads
recent mail and calendar events and logs those involving the user's contacts
on each contact's timeline as EMAIL / MEETING activities (de-duplicated by the
provider's id). Meetings logged in Vector with "add to my calendar" are
created in the user's calendar, with the contact invited.

Needs, per provider, an OAuth app registration:
  Google:    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_SYNC_REDIRECT_URI
             (scopes gmail.readonly, calendar.events, openid email)
  Microsoft: MS365_CLIENT_ID / MS365_CLIENT_SECRET (the same app as mailbox OAuth)
             with GRAPH_REDIRECT_URI and Graph delegated permissions
             Mail.Read, Calendars.ReadWrite, User.Read, offline_access
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.contact_activity import ContactActivity
from app.models.prospect import Prospect
from app.models.sales_extra import UserConnection
from app.models.user import User

logger = logging.getLogger(__name__)

PROVIDERS = ("GOOGLE", "MICROSOFT")
GOOGLE_SCOPES = ("openid email https://www.googleapis.com/auth/gmail.readonly "
                 "https://www.googleapis.com/auth/calendar.events")
GRAPH_SCOPES = "offline_access openid email User.Read Mail.Read Calendars.ReadWrite"
LOOKBACK_DAYS = 14
GRAPH = "https://graph.microsoft.com/v1.0"


class SyncError(Exception):
    pass


# HTTP is behind two functions so tests can replace them
def http_post(url: str, data: dict) -> httpx.Response:
    return httpx.post(url, data=data, timeout=20)


def http_request(method: str, url: str, token: str, params: dict = None, json: dict = None) -> httpx.Response:
    return httpx.request(method, url, params=params, json=json, timeout=30,
                         headers={"Authorization": f"Bearer {token}"})


# ── OAuth ────────────────────────────────────────────────────

def configured(provider: str) -> bool:
    if provider == "GOOGLE":
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.GOOGLE_SYNC_REDIRECT_URI)
    return bool(settings.MS365_CLIENT_ID and settings.MS365_CLIENT_SECRET and settings.GRAPH_REDIRECT_URI)


def _ms_endpoint(path: str) -> str:
    return f"https://login.microsoftonline.com/{settings.MS365_TENANT_ID or 'common'}/oauth2/v2.0/{path}"


def make_state(user: User, provider: str) -> str:
    return jwt.encode({"user_id": user.user_id, "provider": provider, "purpose": "account_sync",
                       "exp": datetime.utcnow() + timedelta(minutes=15)},
                      settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def read_state(state: str) -> dict:
    try:
        data = jwt.decode(state, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise SyncError("The sign-in link has expired. Try connecting again.") from exc
    if data.get("purpose") != "account_sync" or data.get("provider") not in PROVIDERS:
        raise SyncError("Invalid sign-in state")
    return data


def authorize_url(provider: str, user: User) -> str:
    state = make_state(user, provider)
    if provider == "GOOGLE":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": settings.GOOGLE_CLIENT_ID, "redirect_uri": settings.GOOGLE_SYNC_REDIRECT_URI,
            "response_type": "code", "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true", "state": state, "login_hint": user.email})
    return _ms_endpoint("authorize") + "?" + urlencode({
        "client_id": settings.MS365_CLIENT_ID, "redirect_uri": settings.GRAPH_REDIRECT_URI, "response_type": "code",
        "response_mode": "query", "scope": GRAPH_SCOPES, "state": state, "login_hint": user.email,
        "prompt": "select_account"})


def _token(provider: str, data: dict) -> dict:
    if provider == "GOOGLE":
        url = "https://oauth2.googleapis.com/token"
        data = {"client_id": settings.GOOGLE_CLIENT_ID, "client_secret": settings.GOOGLE_CLIENT_SECRET, **data}
    else:
        url = _ms_endpoint("token")
        data = {"client_id": settings.MS365_CLIENT_ID, "client_secret": settings.MS365_CLIENT_SECRET,
                "scope": GRAPH_SCOPES, **data}
    resp = http_post(url, data)
    body = resp.json() if "json" in resp.headers.get("content-type", "") else {}
    if resp.status_code != 200 or "access_token" not in body:
        raise SyncError(body.get("error_description") or body.get("error") or f"Token request failed ({resp.status_code})")
    return body


def complete(db: Session, user: User, provider: str, code: str) -> UserConnection:
    redirect = settings.GOOGLE_SYNC_REDIRECT_URI if provider == "GOOGLE" else settings.GRAPH_REDIRECT_URI
    tokens = _token(provider, {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect})
    conn = db.query(UserConnection).filter(UserConnection.user_id == user.user_id,
                                           UserConnection.provider == provider).first()
    if not conn:
        conn = UserConnection(tenant_id=user.tenant_id, user_id=user.user_id, provider=provider)
        db.add(conn)
    _store(conn, tokens)
    conn.last_error = None
    try:
        if provider == "GOOGLE":
            me = http_request("GET", "https://openidconnect.googleapis.com/v1/userinfo", conn.access_token).json()
            conn.account_email = me.get("email")
        else:
            me = http_request("GET", f"{GRAPH}/me", conn.access_token, params={"$select": "mail,userPrincipalName"}).json()
            conn.account_email = me.get("mail") or me.get("userPrincipalName")
    except Exception as exc:
        logger.warning(f"[AccountSync] Could not read account email: {exc}")
    db.commit()
    return conn


def _store(conn: UserConnection, tokens: dict):
    conn.access_token = tokens["access_token"]
    if tokens.get("refresh_token"):
        conn.refresh_token = tokens["refresh_token"]
    conn.expires_at = datetime.utcnow() + timedelta(seconds=int(tokens.get("expires_in", 3600)))


def access_token(db: Session, conn: UserConnection) -> str:
    if conn.access_token and conn.expires_at and conn.expires_at > datetime.utcnow() + timedelta(minutes=3):
        return conn.access_token
    if not conn.refresh_token:
        raise SyncError("The connection has expired. Reconnect your account.")
    _store(conn, _token(conn.provider, {"grant_type": "refresh_token", "refresh_token": conn.refresh_token}))
    db.commit()
    return conn.access_token


# ── Matching ─────────────────────────────────────────────────

def _contacts_by_email(db: Session, user: User, emails: Iterable[str]) -> Dict[str, Prospect]:
    """Contacts this user may see, by lower-cased email."""
    from app.services.contact_service import scope
    wanted = {e.strip().lower() for e in emails if e and "@" in e}
    if not wanted:
        return {}
    q = db.query(Prospect).filter(Prospect.tenant_id == user.tenant_id, Prospect.deleted_at.is_(None),
                                  func.lower(Prospect.email).in_(wanted))
    return {p.email.lower(): p for p in scope(q, db, user, Prospect.owner_id)}


def _log(db: Session, user: User, prospect: Prospect, kind: str, source: str, external_id: str,
         at: datetime, subject: Optional[str], body: Optional[str], duration: Optional[int] = None) -> bool:
    key = f"{external_id}:{prospect.prospect_id}"[:255]
    if db.query(ContactActivity.activity_id).filter(ContactActivity.external_id == key).first():
        return False
    db.add(ContactActivity(tenant_id=user.tenant_id, prospect_id=prospect.prospect_id, activity_type=kind,
                           subject=(subject or "(no subject)")[:255], body=(body or "")[:2000] or None,
                           duration_minutes=duration, occurred_at=at, created_by=user.user_id,
                           source=source, external_id=key))
    return True


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _parse_iso(value: str) -> datetime:
    """ISO 8601 from Google / Graph. Graph sends 7 fractional digits, which Python rejects."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.strip()).replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(value))


# ── Google ───────────────────────────────────────────────────

def _google_mail(db, user, conn, token, since: datetime) -> int:
    resp = http_request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages", token,
                        params={"q": f"after:{int(since.replace(tzinfo=timezone.utc).timestamp())}", "maxResults": 200})
    if resp.status_code != 200:
        raise SyncError(f"Gmail: {resp.status_code} {resp.text[:200]}")
    logged = 0
    me = (conn.account_email or user.email or "").lower()
    for m in resp.json().get("messages", []):
        msg = http_request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m['id']}", token,
                           params={"format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Subject", "Date"]}).json()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        people = [a.lower() for _, a in getaddresses([headers.get(k, "") for k in ("from", "to", "cc")]) if a]
        at = _utc(parsedate_to_datetime(headers["date"])) if headers.get("date") else datetime.utcnow()
        outbound = bool(me) and headers.get("from", "").lower().find(me) >= 0
        for p in _contacts_by_email(db, user, [a for a in people if a != me]).values():
            logged += _log(db, user, p, "EMAIL", "GMAIL", f"gmail:{m['id']}", at,
                           f"{'Sent' if outbound else 'Received'}: {headers.get('subject', '')}", msg.get("snippet"))
    return logged


def _google_calendar(db, user, conn, token, since: datetime) -> int:
    now = datetime.utcnow()
    resp = http_request("GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events", token, params={
        "updatedMin": since.isoformat() + "Z", "timeMin": (now - timedelta(days=LOOKBACK_DAYS)).isoformat() + "Z",
        "timeMax": (now + timedelta(days=30)).isoformat() + "Z", "singleEvents": "true", "maxResults": 250})
    if resp.status_code != 200:
        raise SyncError(f"Google Calendar: {resp.status_code} {resp.text[:200]}")
    logged = 0
    me = (conn.account_email or user.email or "").lower()
    for ev in resp.json().get("items", []):
        if ev.get("status") == "cancelled" or "dateTime" not in (ev.get("start") or {}):
            continue
        start, end = _parse_iso(ev["start"]["dateTime"]), _parse_iso(ev["end"]["dateTime"])
        emails = [a.get("email", "").lower() for a in ev.get("attendees", [])]
        for p in _contacts_by_email(db, user, [e for e in emails if e != me]).values():
            logged += _log(db, user, p, "MEETING", "GOOGLE_CALENDAR", f"gcal:{ev['id']}", start, ev.get("summary"),
                           ev.get("description"), int((end - start).total_seconds() // 60))
    return logged


# ── Microsoft Graph ──────────────────────────────────────────

def _graph_mail(db, user, conn, token, since: datetime) -> int:
    resp = http_request("GET", f"{GRAPH}/me/messages", token, params={
        "$filter": f"lastModifiedDateTime ge {since.isoformat()}Z", "$top": 200,
        "$select": "id,subject,bodyPreview,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime"})
    if resp.status_code != 200:
        raise SyncError(f"Outlook mail: {resp.status_code} {resp.text[:200]}")
    logged = 0
    me = (conn.account_email or user.email or "").lower()
    for m in resp.json().get("value", []):
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
        people = [sender] + [((r.get("emailAddress") or {}).get("address") or "").lower()
                             for r in (m.get("toRecipients") or []) + (m.get("ccRecipients") or [])]
        at = _parse_iso(m.get("sentDateTime") or m.get("receivedDateTime")) if (m.get("sentDateTime") or m.get("receivedDateTime")) else datetime.utcnow()
        outbound = sender == me
        for p in _contacts_by_email(db, user, [a for a in people if a and a != me]).values():
            logged += _log(db, user, p, "EMAIL", "OUTLOOK", f"outlook:{m['id']}", at,
                           f"{'Sent' if outbound else 'Received'}: {m.get('subject') or ''}", m.get("bodyPreview"))
    return logged


def _graph_calendar(db, user, conn, token, since: datetime) -> int:
    now = datetime.utcnow()
    resp = http_request("GET", f"{GRAPH}/me/calendarView", token, params={
        "startDateTime": (now - timedelta(days=LOOKBACK_DAYS)).isoformat() + "Z",
        "endDateTime": (now + timedelta(days=30)).isoformat() + "Z", "$top": 250,
        "$select": "id,subject,bodyPreview,start,end,attendees,isCancelled"})
    if resp.status_code != 200:
        raise SyncError(f"Outlook calendar: {resp.status_code} {resp.text[:200]}")
    logged = 0
    me = (conn.account_email or user.email or "").lower()
    for ev in resp.json().get("value", []):
        if ev.get("isCancelled"):
            continue
        start = _parse_iso(ev["start"]["dateTime"] + ("" if ev["start"]["dateTime"].endswith("Z") else "Z"))
        end = _parse_iso(ev["end"]["dateTime"] + ("" if ev["end"]["dateTime"].endswith("Z") else "Z"))
        emails = [((a.get("emailAddress") or {}).get("address") or "").lower() for a in ev.get("attendees", [])]
        for p in _contacts_by_email(db, user, [e for e in emails if e and e != me]).values():
            logged += _log(db, user, p, "MEETING", "OUTLOOK_CALENDAR", f"ocal:{ev['id']}", start, ev.get("subject"),
                           ev.get("bodyPreview"), int((end - start).total_seconds() // 60))
    return logged


# ── Sync entry points ────────────────────────────────────────

def sync_connection(db: Session, conn: UserConnection) -> dict:
    user = db.query(User).filter(User.user_id == conn.user_id).first()
    if not user or user.status != "ACTIVE":
        return {"skipped": True}
    since = (conn.last_sync_at or datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)) - timedelta(minutes=5)
    result = {"emails": 0, "meetings": 0}
    try:
        token = access_token(db, conn)
        if conn.provider == "GOOGLE":
            if conn.sync_email:
                result["emails"] = _google_mail(db, user, conn, token, since)
            if conn.sync_calendar:
                result["meetings"] = _google_calendar(db, user, conn, token, since)
        else:
            if conn.sync_email:
                result["emails"] = _graph_mail(db, user, conn, token, since)
            if conn.sync_calendar:
                result["meetings"] = _graph_calendar(db, user, conn, token, since)
        conn.last_sync_at, conn.last_error = datetime.utcnow(), None
    except Exception as exc:
        db.rollback()
        conn = db.query(UserConnection).filter(UserConnection.connection_id == conn.connection_id).first()
        conn.last_error = f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC: {exc}"[:2000]
        result["error"] = str(exc)
    db.commit()
    return result


def sync_all(db: Session) -> int:
    n = 0
    for conn in db.query(UserConnection).all():
        if (conn.provider == "GOOGLE" and not configured("GOOGLE")) or (conn.provider == "MICROSOFT" and not configured("MICROSOFT")):
            continue
        sync_connection(db, conn)
        n += 1
    return n


def push_meeting(db: Session, user: User, activity: ContactActivity, deal_name: Optional[str] = None) -> Optional[str]:
    """Create the logged meeting in the user's calendar, inviting the contact. Best effort."""
    conn = db.query(UserConnection).filter(UserConnection.user_id == user.user_id, UserConnection.sync_calendar.is_(True)).first()
    if not conn or not configured(conn.provider):
        return None
    prospect = db.query(Prospect).filter(Prospect.prospect_id == activity.prospect_id).first() if activity.prospect_id else None
    start = activity.occurred_at
    end = start + timedelta(minutes=activity.duration_minutes or 30)
    title = activity.subject or (f"Meeting: {deal_name}" if deal_name else "Meeting")
    try:
        token = access_token(db, conn)
        if conn.provider == "GOOGLE":
            body = {"summary": title, "description": activity.body or "",
                    "start": {"dateTime": start.isoformat() + "Z"}, "end": {"dateTime": end.isoformat() + "Z"},
                    "attendees": [{"email": prospect.email}] if prospect and prospect.email else []}
            resp = http_request("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", token, json=body)
            event_id = resp.json().get("id")
            prefix = "gcal"
        else:
            body = {"subject": title, "body": {"contentType": "text", "content": activity.body or ""},
                    "start": {"dateTime": start.isoformat(), "timeZone": "UTC"},
                    "end": {"dateTime": end.isoformat(), "timeZone": "UTC"},
                    "attendees": [{"emailAddress": {"address": prospect.email}, "type": "required"}] if prospect and prospect.email else []}
            resp = http_request("POST", f"{GRAPH}/me/events", token, json=body)
            event_id = resp.json().get("id")
            prefix = "ocal"
        if event_id and prospect:
            # the next calendar sync will see this event; mark it so it isn't logged twice
            activity.external_id = f"{prefix}:{event_id}:{prospect.prospect_id}"[:255]
            db.commit()
        return event_id
    except Exception as exc:
        logger.warning(f"[AccountSync] Could not add meeting to calendar: {exc}")
        return None
