# app/routers/connections_router.py
"""Connect your own Google or Microsoft account for calendar and email sync (BRD v2.0 BR-SF-16)."""
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.config import settings
from app.core.database import get_db
from app.models.sales_extra import UserConnection
from app.models.user import User
from app.services import account_sync, oauth_binding

router = APIRouter(prefix="/connections", tags=["Calendar & email sync"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")
PROVIDER = {"google": "GOOGLE", "microsoft": "MICROSOFT"}


def _dict(c: UserConnection) -> dict:
    return {"connection_id": c.connection_id, "provider": c.provider, "account_email": c.account_email,
            "sync_email": c.sync_email, "sync_calendar": c.sync_calendar, "last_sync_at": c.last_sync_at,
            "last_error": c.last_error, "created_at": c.created_at}


@router.get("")
def my_connections(db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    rows = db.query(UserConnection).filter(UserConnection.user_id == current_user.user_id).all()
    return {"connections": [_dict(c) for c in rows],
            "available": {p: account_sync.configured(p) for p in account_sync.PROVIDERS}}


@router.post("/{provider}/start")
def start(provider: str, response: Response, current_user: User = Depends(tenant_user)):
    p = PROVIDER.get(provider)
    if not p:
        raise HTTPException(status_code=404, detail="Unknown provider")
    if not account_sync.configured(p):
        names = ("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_SYNC_REDIRECT_URI" if p == "GOOGLE"
                 else "MS365_CLIENT_ID, MS365_CLIENT_SECRET and GRAPH_REDIRECT_URI")
        raise HTTPException(status_code=400, detail=f"{provider.title()} sync is not set up on the server. Set {names}.")
    return {"authorize_url": account_sync.authorize_url(p, current_user, oauth_binding.issue(response))}


@router.get("/{provider}/callback", include_in_schema=False)
def callback(provider: str, request: Request, state: str = "", code: str = "", error: str = "",
             error_description: str = "", db: Session = Depends(get_db)):
    def back(**params):
        url = settings.CONNECTIONS_POST_CONNECT_URL or "/"
        return RedirectResponse(f"{url}{'&' if '?' in url else '?'}{urlencode(params)}", status_code=302)
    try:
        data = account_sync.read_state(state)
    except account_sync.SyncError as exc:
        return back(sync="error", message=str(exc))
    if not oauth_binding.matches(request, data.get("nonce")):
        return back(sync="error", message=oauth_binding.MISMATCH)
    if error:
        return back(sync="error", message=error_description or error)
    if PROVIDER.get(provider) != data["provider"]:
        return back(sync="error", message="Provider mismatch")
    user = db.query(User).filter(User.user_id == data["user_id"]).first()
    if not user:
        return back(sync="error", message="User not found")
    try:
        conn = account_sync.complete(db, user, data["provider"], code)
    except account_sync.SyncError as exc:
        return back(sync="error", message=str(exc))
    account_sync.sync_connection(db, conn)
    return back(sync="connected", provider=provider)


class ConnectionUpdate(BaseModel):
    sync_email: Optional[bool] = None
    sync_calendar: Optional[bool] = None


def _mine(db, user, connection_id) -> UserConnection:
    c = db.query(UserConnection).filter(UserConnection.connection_id == connection_id,
                                        UserConnection.user_id == user.user_id).first()
    if not c:
        raise HTTPException(status_code=404, detail="Connection not found")
    return c


@router.patch("/{connection_id}")
def update(connection_id: str, payload: ConnectionUpdate, db: Session = Depends(get_db),
           current_user: User = Depends(tenant_user)):
    c = _mine(db, current_user, connection_id)
    for f, v in payload.model_dump(exclude_unset=True).items():
        setattr(c, f, bool(v))
    db.commit()
    return _dict(c)


@router.post("/{connection_id}/sync")
def sync_now(connection_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    c = _mine(db, current_user, connection_id)
    return {**account_sync.sync_connection(db, c), "connection": _dict(c)}


@router.delete("/{connection_id}")
def disconnect(connection_id: str, db: Session = Depends(get_db), current_user: User = Depends(tenant_user)):
    c = _mine(db, current_user, connection_id)
    db.delete(c)
    db.commit()
    return {"status": "disconnected"}
