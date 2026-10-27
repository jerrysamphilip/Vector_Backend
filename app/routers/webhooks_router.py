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
"""
import json
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.auth import require_role
from app.core.database import get_db
from app.models.user import User
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.services import webhooks as svc
from app.utils import webhooks as wh

router = APIRouter(prefix="/integrations/webhooks", tags=["Webhooks"])
admin_user = require_role("SUPER_ADMIN", "ADMIN")

DELIVERY_STATUSES = ("PENDING", "SUCCEEDED", "FAILED", "CANCELLED")


class EndpointCreate(BaseModel):
    url: str = Field(..., max_length=wh.MAX_URL_LENGTH)
    description: Optional[str] = Field(None, max_length=255)
    event_types: List[str] = Field(..., min_length=1, max_length=len(wh.EVENT_TYPES))
    active: bool = True


class EndpointUpdate(BaseModel):
    url: Optional[str] = Field(None, max_length=wh.MAX_URL_LENGTH)
    description: Optional[str] = Field(None, max_length=255)
    event_types: Optional[List[str]] = Field(None, min_length=1, max_length=len(wh.EVENT_TYPES))
    active: Optional[bool] = None


def _endpoint_dict(ep: WebhookEndpoint, secret: Optional[str] = None) -> dict:
    out = {"endpoint_id": ep.endpoint_id, "url": ep.url, "description": ep.description,
           "event_types": ep.event_types or [], "active": bool(ep.active),
           "secret_hint": wh.secret_hint(ep.secret), "consecutive_failures": ep.consecutive_failures or 0,
           "disabled_reason": ep.disabled_reason, "disabled_at": ep.disabled_at,
           "last_success_at": ep.last_success_at, "last_failure_at": ep.last_failure_at,
           "created_at": ep.created_at, "updated_at": ep.updated_at}
    if secret is not None:
        out["secret"] = secret  # only on create / rotate
    return out


def _delivery_dict(d: WebhookDelivery, payload: bool = False) -> dict:
    out = {"delivery_id": d.delivery_id, "endpoint_id": d.endpoint_id, "event_id": d.event_id,
           "event_type": d.event_type, "status": d.status, "attempts": d.attempts or 0,
           "max_attempts": 1 if d.is_test else wh.MAX_ATTEMPTS, "response_code": d.response_code,
           "last_error": d.last_error, "next_attempt_at": d.next_attempt_at, "last_attempt_at": d.last_attempt_at,
           "delivered_at": d.delivered_at, "duration_ms": d.duration_ms, "is_test": bool(d.is_test),
           "redelivery_of": d.redelivery_of, "created_at": d.created_at}
    if payload:
        try:
            out["payload"] = json.loads(d.payload)
        except ValueError:
            out["payload"] = d.payload
    return out


def _get(db: Session, user: User, endpoint_id: str) -> WebhookEndpoint:
    ep = db.query(WebhookEndpoint).filter(WebhookEndpoint.endpoint_id == endpoint_id,
                                          WebhookEndpoint.tenant_id == user.tenant_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found")
    return ep


def _check_url(url: str) -> str:
    try:
        svc.check_url(url)
    except wh.WebhookUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return url.strip()


def _event_types(values: List[str]) -> List[str]:
    try:
        types = wh.clean_event_types(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not types:
        raise HTTPException(status_code=422, detail="Choose at least one event")
    return types


def _clean(text: Optional[str]) -> Optional[str]:
    text = (text or "").strip()
    return text[:255] or None


@router.get("/event-types")
def event_types(current_user: User = Depends(admin_user)):
    return [{"type": t, "label": label, "description": desc} for t, (label, desc) in wh.EVENT_TYPES.items()]


@router.get("")
def list_endpoints(db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    rows = db.query(WebhookEndpoint).filter(WebhookEndpoint.tenant_id == current_user.tenant_id) \
        .order_by(WebhookEndpoint.created_at.desc()).all()
    return {"items": [_endpoint_dict(ep) for ep in rows], "max_endpoints": wh.MAX_ENDPOINTS_PER_TENANT,
            "signature_header": wh.SIGNATURE_HEADER, "max_attempts": wh.MAX_ATTEMPTS,
            "disable_after_failures": wh.DISABLE_AFTER_FAILURES}


@router.post("", status_code=201)
def create_endpoint(payload: EndpointCreate, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    count = db.query(WebhookEndpoint).filter(WebhookEndpoint.tenant_id == current_user.tenant_id).count()
    if count >= wh.MAX_ENDPOINTS_PER_TENANT:
        raise HTTPException(status_code=409, detail=f"A workspace can have up to {wh.MAX_ENDPOINTS_PER_TENANT} webhook endpoints")
    url = _check_url(payload.url)
    secret = wh.new_secret()
    ep = WebhookEndpoint(endpoint_id=str(uuid.uuid4()), tenant_id=current_user.tenant_id, url=url,
                         description=_clean(payload.description), secret=secret,
                         event_types=_event_types(payload.event_types), active=payload.active,
                         consecutive_failures=0, created_by=current_user.user_id)
    db.add(ep)
    db.commit()
    db.refresh(ep)
    svc.forget_subscribers(db, current_user.tenant_id)
    return _endpoint_dict(ep, secret=secret)


@router.get("/{endpoint_id}")
def get_endpoint(endpoint_id: str, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    return _endpoint_dict(_get(db, current_user, endpoint_id))


@router.patch("/{endpoint_id}")
def update_endpoint(endpoint_id: str, payload: EndpointUpdate, db: Session = Depends(get_db),
                    current_user: User = Depends(admin_user)):
    ep = _get(db, current_user, endpoint_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("url") is not None:
        ep.url = _check_url(data["url"])
    if "description" in data:
        ep.description = _clean(data["description"])
    if data.get("event_types") is not None:
        ep.event_types = _event_types(data["event_types"])
    if data.get("active") is not None and bool(data["active"]) != bool(ep.active):
        if data["active"]:
            # Back on: a clean slate (failure count, auto-disable reason)
            ep.active, ep.consecutive_failures, ep.disabled_reason, ep.disabled_at = True, 0, None, None
        else:
            svc.disable_endpoint(db, ep, f"Switched off by {current_user.email}"[:255], notify_admins=False)
    db.commit()
    db.refresh(ep)
    svc.forget_subscribers(db, current_user.tenant_id)
    return _endpoint_dict(ep)


@router.delete("/{endpoint_id}")
def delete_endpoint(endpoint_id: str, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    ep = _get(db, current_user, endpoint_id)
    db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == ep.endpoint_id).delete(synchronize_session=False)
    db.delete(ep)
    db.commit()
    svc.forget_subscribers(db, current_user.tenant_id)
    return {"status": "deleted", "endpoint_id": endpoint_id}


@router.post("/{endpoint_id}/rotate-secret")
def rotate_secret(endpoint_id: str, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    ep = _get(db, current_user, endpoint_id)
    secret = wh.new_secret()
    ep.secret = secret
    db.commit()
    db.refresh(ep)
    return _endpoint_dict(ep, secret=secret)


@router.post("/{endpoint_id}/test")
def send_test(endpoint_id: str, db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    """Send a webhook.test event now (one attempt, does not count toward auto-disable)."""
    ep = _get(db, current_user, endpoint_id)
    event_id = str(uuid.uuid4())
    body = wh.build_body(event_id, wh.TEST_EVENT, utcnow(), current_user.tenant_id, {
        "endpoint_id": ep.endpoint_id, "message": "Test event from Outreach 360", "sent_by": current_user.email})
    d = WebhookDelivery(delivery_id=str(uuid.uuid4()), tenant_id=current_user.tenant_id, endpoint_id=ep.endpoint_id,
                        event_id=event_id, event_type=wh.TEST_EVENT, payload=body.decode(), status="PENDING",
                        attempts=0, next_attempt_at=None, is_test=True)
    db.add(d)
    svc.deliver_now(db, d, ep)
    db.refresh(d)
    return _delivery_dict(d, payload=True)


@router.get("/{endpoint_id}/deliveries")
def list_deliveries(endpoint_id: str, status: Optional[str] = Query(None, max_length=12),
                    event_type: Optional[str] = Query(None, max_length=50),
                    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=10000),
                    db: Session = Depends(get_db), current_user: User = Depends(admin_user)):
    ep = _get(db, current_user, endpoint_id)
    query = db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == ep.endpoint_id,
                                             WebhookDelivery.tenant_id == current_user.tenant_id)
    if status:
        if status.upper() not in DELIVERY_STATUSES:
            raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(DELIVERY_STATUSES)}")
        query = query.filter(WebhookDelivery.status == status.upper())
    if event_type:
        query = query.filter(WebhookDelivery.event_type == event_type)
    total = query.count()
    rows = query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.delivery_id) \
        .offset(offset).limit(limit).all()
    return {"items": [_delivery_dict(d) for d in rows], "total": total, "limit": limit, "offset": offset}


def _get_delivery(db: Session, user: User, ep: WebhookEndpoint, delivery_id: str) -> WebhookDelivery:
    d = db.query(WebhookDelivery).filter(WebhookDelivery.delivery_id == delivery_id,
                                         WebhookDelivery.endpoint_id == ep.endpoint_id,
                                         WebhookDelivery.tenant_id == user.tenant_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Delivery not found")
    return d


@router.get("/{endpoint_id}/deliveries/{delivery_id}")
def get_delivery(endpoint_id: str, delivery_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(admin_user)):
    ep = _get(db, current_user, endpoint_id)
    return _delivery_dict(_get_delivery(db, current_user, ep, delivery_id), payload=True)


@router.post("/{endpoint_id}/deliveries/{delivery_id}/redeliver", status_code=201)
def redeliver(endpoint_id: str, delivery_id: str, db: Session = Depends(get_db),
              current_user: User = Depends(admin_user)):
    """Queue the same event (same id and body) again as a new delivery; the worker sends it."""
    ep = _get(db, current_user, endpoint_id)
    original = _get_delivery(db, current_user, ep, delivery_id)
    if not ep.active:
        raise HTTPException(status_code=409, detail="Turn the endpoint on before redelivering")
    if original.status == "PENDING":
        raise HTTPException(status_code=409, detail="This delivery is still being retried")
    copy = WebhookDelivery(delivery_id=str(uuid.uuid4()), tenant_id=ep.tenant_id, endpoint_id=ep.endpoint_id,
                           event_id=original.event_id, event_type=original.event_type, payload=original.payload,
                           status="PENDING", attempts=0,
                           next_attempt_at=utcnow().replace(microsecond=0),  # never later than the job's now
                           is_test=False, redelivery_of=original.delivery_id)
    db.add(copy)
    db.commit()
    db.refresh(copy)
    return _delivery_dict(copy)
