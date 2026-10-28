"""
Outbound webhooks (BRD v2.0 BR-SF-20), workspace admins only:

  GET    /integrations/webhooks/event-types                       the events an endpoint can subscribe to
  GET    /integrations/webhooks                                   endpoints of the workspace
  POST   /integrations/webhooks                                   register (the secret is returned once)
  GET    /integrations/webhooks/{id}
  PATCH  /integrations/webhooks/{id}                              url, description, event types, active
  DELETE /integrations/webhooks/{id}
  POST   /integrations/webhooks/{id}/rotate-secret                new secret (returned once), old one stops working
  POST   /integrations/webhooks/{id}/test                         send a webhook.test event now and return the result
  GET    /integrations/webhooks/{id}/deliveries                   recent deliveries (delivery log)
  GET    /integrations/webhooks/{id}/deliveries/{delivery_id}     one delivery with its payload
  POST   /integrations/webhooks/{id}/deliveries/{delivery_id}/redeliver   queue the same event again

Layered (ADR 0009, chunk A7): each handler makes one call to WebhookService (app/services/webhooks.py).
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import require_role
from app.core.database import get_db
from app.domain.webhooks import DELIVERY_STATUSES  # noqa: F401  (kept for callers of the first version)
from app.schemas.webhook_schema import EndpointCreate, EndpointUpdate
from app.services.webhooks import WebhookService

router = APIRouter(prefix="/integrations/webhooks", tags=["Webhooks"])
admin_user = require_role("SUPER_ADMIN", "ADMIN")


def webhook_service(db=Depends(get_db)) -> WebhookService:
    return WebhookService.for_session(db)


Hooks = Depends(webhook_service)


@router.get("/event-types")
def event_types(current_user: Any = Depends(admin_user)):
    return WebhookService.event_types()


@router.get("")
def list_endpoints(service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.list(current_user)


@router.post("", status_code=201)
def create_endpoint(payload: EndpointCreate, service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.create(current_user, payload.url, payload.description, payload.event_types, payload.active)


@router.get("/{endpoint_id}")
def get_endpoint(endpoint_id: str, service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.get(current_user, endpoint_id)


@router.patch("/{endpoint_id}")
def update_endpoint(endpoint_id: str, payload: EndpointUpdate, service: WebhookService = Hooks,
                    current_user: Any = Depends(admin_user)):
    return service.update(current_user, endpoint_id, payload.model_dump(exclude_unset=True))


@router.delete("/{endpoint_id}")
def delete_endpoint(endpoint_id: str, service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.delete(current_user, endpoint_id)


@router.post("/{endpoint_id}/rotate-secret")
def rotate_secret(endpoint_id: str, service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.rotate_secret(current_user, endpoint_id)


@router.post("/{endpoint_id}/test")
def send_test(endpoint_id: str, service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    """Send a webhook.test event now (one attempt, does not count toward auto-disable)."""
    return service.send_test(current_user, endpoint_id)


@router.get("/{endpoint_id}/deliveries")
def list_deliveries(endpoint_id: str, status: Optional[str] = Query(None, max_length=12),
                    event_type: Optional[str] = Query(None, max_length=50),
                    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                    service: WebhookService = Hooks, current_user: Any = Depends(admin_user)):
    return service.deliveries(current_user, endpoint_id, status, event_type, limit, offset)


@router.get("/{endpoint_id}/deliveries/{delivery_id}")
def get_delivery(endpoint_id: str, delivery_id: str, service: WebhookService = Hooks,
                 current_user: Any = Depends(admin_user)):
    return service.delivery(current_user, endpoint_id, delivery_id)


@router.post("/{endpoint_id}/deliveries/{delivery_id}/redeliver", status_code=201)
def redeliver(endpoint_id: str, delivery_id: str, service: WebhookService = Hooks,
              current_user: Any = Depends(admin_user)):
    """Queue the same event (same id and body) again as a new delivery; the worker sends it."""
    return service.redeliver(current_user, endpoint_id, delivery_id)
