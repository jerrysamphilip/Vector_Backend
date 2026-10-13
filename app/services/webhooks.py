"""
Outbound webhooks (BRD v2.0 BR-SF-20): other systems are told when things happen here.

emit_event(db, tenant_id, type, data) is the one entry point the rest of the code calls. It
writes one webhook_deliveries row per active endpoint of the workspace subscribed to the type,
in the caller's transaction (outbox): the event goes out if and only if the change commits.
The worker's "webhooks" loop (app/jobs.py) then POSTs due rows, signed with the endpoint secret
(X-Outreach-Signature: t=<ts>,v1=<hex HMAC-SHA256 over "<ts>.<body>">), retrying with backoff
(app/utils/webhooks.py) and switching an endpoint off after too many consecutive failures,
with an in-app notification to the workspace admins.

Where events are raised:
  contact.updated, lead.created, lead.qualified,
  deal.created, deal.stage_changed, deal.won, deal.lost   crm.record_changes() -> on_record_changes()
  contact.created                                         contacts_router.create_contact
  contact.deleted                                         crm.soft_delete_contacts
  email.bounced, email.unsubscribed                       suppression.suppress (HARD_BOUNCE / UNSUBSCRIBE, COMPLAINT)
  email.replied                                           imap_sync_service (inbound reply, not out-of-office)
  campaign.paused                                         campaign state changes (pause API, auto-pause)

campaign.completed is not offered: no code path marks a campaign COMPLETED today (the services'
complete() methods have no caller).

Every outbound request is SSRF-checked at send time: the host is resolved, every address must
be public (development / test also allow private ranges and http://), and the connection goes to
the checked address (no second lookup, no redirects), with a 10 s timeout.
"""
import http.client
import logging
import socket
import ssl
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.webhook import WebhookDelivery, WebhookEndpoint
from app.utils import webhooks as wh

logger = logging.getLogger(__name__)

USER_AGENT = "Outreach360-Webhooks/1.0"
RETENTION_DAYS = 30
BATCH_SIZE = 40
PER_ENDPOINT_BATCH = 10
SEND_THREADS = 8
_SUBSCRIBER_CACHE_SECONDS = 30


def allow_insecure() -> bool:
    """http:// and private addresses are accepted only in development / test (local receivers)."""
    return (settings.ENVIRONMENT or "").strip().lower() in ("development", "dev", "local", "test")


def check_url(url: str, resolver=None) -> wh.Target:
    return wh.validate_url(url, allow_insecure=allow_insecure(), resolver=resolver or wh.socket_resolver)


# ── Emitting (outbox) ──────────────────────────────────────────

def _subscribers(db: Session, tenant_id: str) -> List[Tuple[str, frozenset]]:
    """Active endpoints of the workspace as (endpoint_id, event types), cached briefly per session."""
    cache = db.info.setdefault("webhook_subscribers", {})
    hit = cache.get(tenant_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    rows = db.query(WebhookEndpoint.endpoint_id, WebhookEndpoint.event_types).filter(
        WebhookEndpoint.tenant_id == tenant_id, WebhookEndpoint.active.is_(True)).all()
    subs = [(eid, frozenset(types or [])) for eid, types in rows]
    cache[tenant_id] = (time.monotonic() + _SUBSCRIBER_CACHE_SECONDS, subs)
    return subs


def forget_subscribers(db: Session, tenant_id: Optional[str] = None) -> None:
    cache = db.info.get("webhook_subscribers")
    if cache is not None:
        cache.pop(tenant_id, None) if tenant_id else cache.clear()


def wants(db: Session, tenant_id: Optional[str], event_type: str) -> bool:
    if not tenant_id:
        return False
    try:
        return any(event_type in types for _, types in _subscribers(db, tenant_id))
    except Exception as exc:  # webhooks never break the action that raised the event
        logger.warning("[Webhooks] subscriber lookup failed: %s", exc)
        return False


def emit_event(db: Session, tenant_id: Optional[str], event_type: str, data: dict) -> int:
    """Queue the event for every active endpoint of the workspace subscribed to it. Caller commits."""
    if event_type not in wh.EVENT_TYPES:
        raise ValueError(f"Unknown webhook event type {event_type}")
    if not wants(db, tenant_id, event_type):
        return 0
    try:
        event_id = str(uuid.uuid4())
        now = datetime.utcnow()
        body = wh.build_body(event_id, event_type, now, tenant_id, data).decode()
        queued = 0
        for endpoint_id, types in _subscribers(db, tenant_id):
            if event_type in types:
                db.add(WebhookDelivery(tenant_id=tenant_id, endpoint_id=endpoint_id, event_id=event_id,
                                       event_type=event_type, payload=body, status="PENDING",
                                       attempts=0, next_attempt_at=now))
                queued += 1
        return queued
    except Exception as exc:
        logger.warning("[Webhooks] could not queue %s: %s", event_type, exc)
        return 0


# ── Payloads ───────────────────────────────────────────────────

def contact_data(p) -> dict:
    return {"contact_id": p.prospect_id, "email": p.email, "first_name": p.first_name, "last_name": p.last_name,
            "company_name": p.company_name, "job_title": p.designation, "phone": p.phone,
            "lifecycle_stage": p.lifecycle_stage, "lead_status": p.lead_status, "lead_source": p.lead_source,
            "consent_status": p.consent_status, "owner_id": p.owner_id, "account_id": p.account_id,
            "tags": p.tags or [], "created_at": p.created_at, "updated_at": p.updated_at}


def lead_data(db: Session, lead) -> dict:
    from app.models.prospect import Prospect
    p = db.get(Prospect, lead.prospect_id) if lead.prospect_id else None
    return {"lead_id": lead.lead_id, "contact_id": lead.prospect_id, "account_id": lead.account_id,
            "owner_id": lead.owner_id, "stage": lead.stage, "source": lead.source,
            "campaign_id": lead.campaign_id, "opportunity_id": lead.opportunity_id,
            "contact_email": p.email if p else None,
            "contact_name": " ".join(x for x in (p.first_name, p.last_name) if x) if p else None,
            "created_at": lead.created_at, "qualified_at": lead.qualified_at}


def deal_data(db: Session, opp) -> dict:
    from app.models.sales import SalesStage
    stage = db.get(SalesStage, opp.stage_id) if opp.stage_id else None
    return {"deal_id": opp.opportunity_id, "name": opp.name, "amount": opp.amount, "status": opp.status,
            "stage_id": opp.stage_id, "stage": stage.name if stage else None, "close_date": opp.close_date,
            "closed_reason": opp.closed_reason, "client_type": opp.client_type, "owner_id": opp.owner_id,
            "account_id": opp.account_id, "contact_id": opp.prospect_id, "lead_id": opp.lead_id,
            "campaign_id": opp.campaign_id, "created_at": opp.created_at, "closed_at": opp.closed_at}


def _diff(before: dict, after: dict) -> Dict[str, dict]:
    from app.services.crm import _fmt
    changes = {}
    for field, old in before.items():
        new = after.get(field)
        if field == "custom_fields":
            old, new = old or {}, new or {}
            for key in sorted(set(old) | set(new)):
                if _fmt(old.get(key)) != _fmt(new.get(key)):
                    changes[f"custom.{key}"] = {"from": _fmt(old.get(key)), "to": _fmt(new.get(key))}
        elif _fmt(old) != _fmt(new):
            changes[field] = {"from": _fmt(old), "to": _fmt(new)}
    return changes


_RECORD_EVENTS = {
    "CONTACT": ("contact.updated",),
    "LEAD": ("lead.created", "lead.qualified"),
    "DEAL": ("deal.created", "deal.stage_changed", "deal.won", "deal.lost"),
}


def on_record_changes(db: Session, tenant_id: str, object_type: str, object_id: str,
                      before: dict, after: dict, source: Optional[str]) -> None:
    """Called by crm.record_changes() for every tracked edit. Imports are left out (bulk loads)."""
    if source == "IMPORT" or object_type not in _RECORD_EVENTS:
        return
    if not any(wants(db, tenant_id, t) for t in _RECORD_EVENTS[object_type]):
        return
    try:
        changes = _diff(before, after)
        if not changes:
            return
        if object_type == "CONTACT":
            from app.models.prospect import Prospect
            p = db.get(Prospect, object_id)
            if p is not None and "created" not in changes:
                emit_event(db, tenant_id, "contact.updated", {**contact_data(p), "changes": changes})
        elif object_type == "LEAD":
            from app.models.sales import Lead
            lead = db.get(Lead, object_id)
            if lead is None:
                return
            if "created" in changes:
                emit_event(db, tenant_id, "lead.created", lead_data(db, lead))
            if (changes.get("stage") or {}).get("to") == "SQL":
                emit_event(db, tenant_id, "lead.qualified", {**lead_data(db, lead), "changes": changes})
        else:
            from app.models.sales import Opportunity
            opp = db.get(Opportunity, object_id)
            if opp is None:
                return
            data = deal_data(db, opp)
            if "created" in changes:
                emit_event(db, tenant_id, "deal.created", data)
            elif "stage_id" in changes:
                emit_event(db, tenant_id, "deal.stage_changed",
                           {**data, "previous_stage": changes["stage_id"]["from"], "changes": changes})
            else:
                return
            if opp.status == "WON":
                emit_event(db, tenant_id, "deal.won", data)
            elif opp.status == "LOST":
                emit_event(db, tenant_id, "deal.lost", data)
    except Exception as exc:
        logger.warning("[Webhooks] %s %s event skipped: %s", object_type, object_id, exc)


def contacts_deleted(db: Session, prospects: Iterable, user_id: Optional[str]) -> None:
    for p in prospects:
        emit_event(db, p.tenant_id, "contact.deleted",
                   {"contact_id": p.prospect_id, "email": p.email, "deleted_by": user_id,
                    "deleted_at": datetime.utcnow()})


def suppressed(db: Session, tenant_id: str, email: str, kind: str, reason: str, source: Optional[str],
               contact_ids: List[str], already: bool) -> None:
    """email.bounced for a hard bounce; email.unsubscribed for a new unsubscribe or complaint."""
    data = {"email": email, "contact_ids": contact_ids[:100], "reason": (reason or "")[:255], "source": source}
    if kind == "HARD_BOUNCE":
        emit_event(db, tenant_id, "email.bounced", data)
    elif kind in ("UNSUBSCRIBE", "COMPLAINT") and not already:
        emit_event(db, tenant_id, "email.unsubscribed", {**data, "kind": kind.lower()})


def email_replied(db: Session, tenant_id: str, reply, replied_to, prospect) -> None:
    emit_event(db, tenant_id, "email.replied", {
        "message_id": reply.message_id, "in_reply_to_message_id": replied_to.message_id if replied_to else None,
        "campaign_id": reply.campaign_id, "contact_id": prospect.prospect_id, "from_email": reply.from_email,
        "subject": (reply.subject or "")[:300], "snippet": (reply.body_text or "")[:300],
        "received_at": reply.sent_at})


def campaign_state_changed(db: Session, campaign_id: str, from_state: Optional[str], to_state: str,
                           reason: Optional[str] = None) -> None:
    event_type = {"PAUSED": "campaign.paused"}.get(to_state)
    if not event_type or from_state == to_state:
        return
    from app.models.campaign import Campaign
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return
    emit_event(db, campaign.tenant_id, event_type, {
        "campaign_id": campaign.campaign_id, "name": campaign.campaign_name, "from_status": from_state,
        "to_status": to_state, "reason": (reason or "")[:500] or None,
        "auto_paused": bool(getattr(campaign, "auto_paused", False))})


# ── Sending ────────────────────────────────────────────────────

def post(target: wh.Target, body: bytes, headers: Dict[str, str],
         timeout: float = wh.REQUEST_TIMEOUT_SECONDS) -> Tuple[int, str]:
    """POST to the validated address (target.ip) with Host / SNI of target.host. No redirects."""
    sock = socket.create_connection((target.ip or target.host, target.port), timeout=timeout)
    try:
        if target.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.host)
        conn = http.client.HTTPConnection(target.host, target.port, timeout=timeout)
        conn.sock = sock
        host = f"[{target.host}]" if ":" in target.host else target.host
        default_port = 443 if target.scheme == "https" else 80
        conn.putrequest("POST", target.path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", host if target.port == default_port else f"{host}:{target.port}")
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        snippet = resp.read(512).decode("utf-8", "replace")
        return resp.status, snippet
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _send(job: dict) -> dict:
    """One HTTP attempt (thread pool). No database access here."""
    started = time.monotonic()
    result = {"delivery_id": job["delivery_id"], "code": None, "error": None}
    try:
        target = check_url(job["url"])
        body = job["payload"].encode()
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT,
                   wh.SIGNATURE_HEADER: wh.sign(job["secret"] or "", body),
                   "X-Outreach-Event": job["event_type"], "X-Outreach-Event-Id": job["event_id"],
                   "X-Outreach-Delivery": job["delivery_id"]}
        code, snippet = post(target, body, headers)
        result["code"] = code
        if not wh.is_success(code):
            result["error"] = f"HTTP {code}" + (f": {snippet.strip()[:300]}" if snippet.strip() else "")
    except wh.WebhookUrlError as exc:
        result["error"] = f"Blocked: {exc}"
    except socket.timeout:
        result["error"] = f"Timed out after {wh.REQUEST_TIMEOUT_SECONDS}s"
    except ssl.SSLError as exc:
        result["error"] = f"TLS error: {exc}"
    except (OSError, http.client.HTTPException) as exc:
        result["error"] = f"Connection failed: {type(exc).__name__}: {exc}"
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _job(d: WebhookDelivery, ep: WebhookEndpoint) -> dict:
    return {"delivery_id": d.delivery_id, "url": ep.url, "secret": ep.secret, "payload": d.payload,
            "event_type": d.event_type, "event_id": d.event_id}


def _admin_ids(db: Session, tenant_id: str) -> List[str]:
    from app.models.user import User
    return [u for (u,) in db.query(User.user_id).filter(
        User.tenant_id == tenant_id, User.role.in_(("SUPER_ADMIN", "ADMIN")), User.status == "ACTIVE")]


def cancel_pending(db: Session, endpoint_id: str, reason: str) -> int:
    return db.query(WebhookDelivery).filter(
        WebhookDelivery.endpoint_id == endpoint_id, WebhookDelivery.status == "PENDING"
    ).update({WebhookDelivery.status: "CANCELLED", WebhookDelivery.last_error: reason[:500],
              WebhookDelivery.next_attempt_at: None}, synchronize_session=False)


def disable_endpoint(db: Session, ep: WebhookEndpoint, reason: str, notify_admins: bool) -> None:
    ep.active = False
    ep.disabled_at = datetime.utcnow()
    ep.disabled_reason = reason[:255]
    cancel_pending(db, ep.endpoint_id, "Endpoint disabled")
    forget_subscribers(db, ep.tenant_id)
    if notify_admins:
        from app.services.notifications import notify
        notify(db, ep.tenant_id, _admin_ids(db, ep.tenant_id), "WEBHOOK_DISABLED",
               "A webhook endpoint was switched off",
               f"{ep.url[:200]} failed {ep.consecutive_failures} times in a row and no longer receives events. "
               "Fix the receiver, then turn the endpoint back on in Webhooks.",
               "/app/webhooks", dedupe_key=f"webhook-disabled:{ep.endpoint_id}", dedupe_days=1)
        logger.warning("[Webhooks] endpoint %s disabled after %s consecutive failures",
                       ep.endpoint_id, ep.consecutive_failures)


def record_result(db: Session, d: WebhookDelivery, ep: WebhookEndpoint, result: dict) -> None:
    """Apply one attempt's outcome to the delivery and its endpoint. Caller commits."""
    now = datetime.utcnow()
    d.attempts = (d.attempts or 0) + 1
    d.last_attempt_at = now
    d.response_code = result.get("code")
    d.duration_ms = result.get("duration_ms")
    if result.get("error") is None and wh.is_success(result.get("code")):
        d.status, d.delivered_at, d.last_error, d.next_attempt_at = "SUCCEEDED", now, None, None
        if not d.is_test:
            ep.consecutive_failures = 0
            ep.last_success_at = now
        return
    d.last_error = (result.get("error") or "Failed")[:500]
    delay = None if d.is_test else wh.next_delay(d.attempts)
    if delay is None:
        d.status, d.next_attempt_at = "FAILED", None
    else:
        d.status, d.next_attempt_at = "PENDING", now + timedelta(seconds=delay)
    if d.is_test:
        return
    ep.consecutive_failures = (ep.consecutive_failures or 0) + 1
    ep.last_failure_at = now
    if ep.active and wh.should_disable(ep.consecutive_failures):
        disable_endpoint(db, ep, f"Switched off after {ep.consecutive_failures} consecutive failed deliveries",
                         notify_admins=True)


def deliver_now(db: Session, d: WebhookDelivery, ep: WebhookEndpoint) -> WebhookDelivery:
    """One synchronous attempt (test event). Commits."""
    job = _job(d, ep)
    db.commit()  # the row exists even if the request below hangs until its timeout
    record_result(db, d, ep, _send(job))
    db.commit()
    return d


def deliver_due(session_factory, now: Optional[datetime] = None, limit: int = BATCH_SIZE) -> int:
    """Send due PENDING deliveries of active endpoints (runs under the job's named lock). Returns attempts."""
    now = now or datetime.utcnow()
    with session_factory() as db:
        rows = db.query(WebhookDelivery, WebhookEndpoint).join(
            WebhookEndpoint, WebhookEndpoint.endpoint_id == WebhookDelivery.endpoint_id
        ).filter(WebhookDelivery.status == "PENDING", WebhookDelivery.next_attempt_at <= now,
                 WebhookEndpoint.active.is_(True)
                 ).order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.created_at).limit(limit * 3).all()
        per_endpoint: Dict[str, int] = {}
        jobs = []
        for d, ep in rows:
            if per_endpoint.get(ep.endpoint_id, 0) >= PER_ENDPOINT_BATCH or len(jobs) >= limit:
                continue
            per_endpoint[ep.endpoint_id] = per_endpoint.get(ep.endpoint_id, 0) + 1
            jobs.append(_job(d, ep))
    if not jobs:
        return 0
    with ThreadPoolExecutor(max_workers=min(SEND_THREADS, len(jobs)), thread_name_prefix="webhook") as pool:
        results = list(pool.map(_send, jobs))
    with session_factory() as db:
        for result in results:
            d = db.get(WebhookDelivery, result["delivery_id"])
            if d is None or d.status != "PENDING":
                continue  # endpoint deleted or disabled meanwhile
            ep = db.get(WebhookEndpoint, d.endpoint_id)
            if ep is None:
                continue
            record_result(db, d, ep, result)
        db.commit()
    return len(jobs)


def purge_old(db: Session, days: int = RETENTION_DAYS) -> int:
    """Delivery log retention: rows older than `days` that are no longer pending. Commits."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    n = db.query(WebhookDelivery).filter(WebhookDelivery.created_at < cutoff,
                                         WebhookDelivery.status != "PENDING").delete(synchronize_session=False)
    db.commit()
    return n
