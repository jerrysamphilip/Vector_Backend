# app/routers/connections_router.py
"""Connect your own Google or Microsoft account for calendar and email sync (BRD v2.0 BR-SF-16)."""
from typing import Any, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.core.auth import require_role
from app.core.config import settings
from app.core.database import get_db
from app.services import oauth_binding
from app.services.connection_service import PROVIDER, ConnectionService

router = APIRouter(prefix="/connections", tags=["Calendar & email sync"])
tenant_user = require_role("SUPER_ADMIN", "ADMIN", "MANAGER", "AGENT")

__all__ = ["PROVIDER", "router"]


def connection_service(db=Depends(get_db)) -> ConnectionService:
    return ConnectionService.for_session(db)


Service = Depends(connection_service)


@router.get("")
def my_connections(service: ConnectionService = Service, current_user: Any = Depends(tenant_user)):
    return service.mine(current_user)


@router.post("/{provider}/start")
def start(provider: str, response: Response, current_user: Any = Depends(tenant_user)):
    return ConnectionService.start(provider, current_user, lambda: oauth_binding.issue(response))


@router.get("/{provider}/callback", include_in_schema=False)
def callback(provider: str, request: Request, state: str = "", code: str = "", error: str = "",
             error_description: str = "", service: ConnectionService = Service):
    params = service.complete(provider, state, code, error, error_description,
                              lambda nonce: oauth_binding.matches(request, nonce))
    url = settings.CONNECTIONS_POST_CONNECT_URL or "/"
    return RedirectResponse(f"{url}{'&' if '?' in url else '?'}{urlencode(params)}", status_code=302)


class ConnectionUpdate(BaseModel):
    sync_email: Optional[bool] = None
    sync_calendar: Optional[bool] = None


@router.patch("/{connection_id}")
def update(connection_id: str, payload: ConnectionUpdate, service: ConnectionService = Service,
           current_user: Any = Depends(tenant_user)):
    return service.update(connection_id, payload.model_dump(exclude_unset=True), current_user)


@router.post("/{connection_id}/sync")
def sync_now(connection_id: str, service: ConnectionService = Service, current_user: Any = Depends(tenant_user)):
    return service.sync_now(connection_id, current_user)


@router.delete("/{connection_id}")
def disconnect(connection_id: str, service: ConnectionService = Service, current_user: Any = Depends(tenant_user)):
    return service.disconnect(connection_id, current_user)
