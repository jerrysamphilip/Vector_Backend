"""Outbound webhooks (BRD v2.0 BR-SF-20 / RP-106b), in-process API tests against MySQL.

Own workspaces. No network: the HTTP call (app.services.webhooks.post) is replaced by a recorder
and DNS (app.utils.webhooks.socket_resolver) by a fixed public address. The worker loop is not
running in tests, so the delivery job is driven by calling deliver_due() directly.
"""
import json
import uuid
from datetime import timedelta

import pytest

from tests.conftest import RUN, SessionLocal, auth, login, register_tenant, sql
from app.core.clock import utcnow

BASE = "/integrations/webhooks"
PUBLIC_IP = "93.184.216.34"


class Receiver:
    """Stands in for the remote endpoint: records requests, answers with `code`."""

    def __init__(self):
        self.calls, self.code = [], 204

    def __call__(self, target, body, headers, timeout=10):
        self.calls.append({"target": target, "body": body, "headers": headers, "timeout": timeout})
        if isinstance(self.code, Exception):
            raise self.code
        return self.code, ""

    def for_url(self, fragment):
        return [c for c in self.calls if fragment in c["target"].path]


@pytest.fixture
def rx(monkeypatch):
    from app.services import webhooks as svc
    from app.utils import webhooks as wh
    receiver = Receiver()
    monkeypatch.setattr(svc, "post", receiver)
    monkeypatch.setattr(wh, "socket_resolver", lambda host, port: [PUBLIC_IP])
    return receiver


def _member(client, tenant_id: str, label: str, role: str) -> dict:
    from app.core.security import hash_password
    from app.models.user import User
    email = f"{label}-{RUN}-{uuid.uuid4().hex[:4]}@wh{RUN}.example.com"
    password = f"{label}-pass-{RUN}"
    with SessionLocal() as db:
        user = User(tenant_id=tenant_id, first_name=label.capitalize(), last_name="Tester", email=email,
                    password_hash=hash_password(password), role=role, status="ACTIVE", auth_provider="local",
                    email_verified=True)
        db.add(user)
        db.commit()
        uid = user.user_id
    return {"user_id": uid, "headers": auth(login(client, email, password))}


@pytest.fixture(scope="module")
def ws(client):
    t = register_tenant(client, "wh")
    other = register_tenant(client, "whb")
    agent = _member(client, t["tenant_id"], "whagent", "AGENT")
    admin = _member(client, t["tenant_id"], "whadmin", "ADMIN")
    return {"owner": t, "h": t["headers"], "tid": t["tenant_id"], "agent": agent, "admin": admin, "other": other}


@pytest.fixture(autouse=True)
def _fresh_endpoints(client, ws):
    """Each test starts with no endpoints in the shared workspace: the module shares one tenant and
    the per-workspace cap (MAX_ENDPOINTS_PER_TENANT) would otherwise be hit part-way through."""
    yield
    for ep in client.get(BASE, headers=ws["h"]).json().get("items", []):
        client.delete(f"{BASE}/{ep['endpoint_id']}", headers=ws["h"])


_n = iter(range(10 ** 6))


def _url(tag):
    return f"https://hooks{RUN}.example.com/{tag}-{next(_n)}"


def _create(client, h, events, url=None, **extra):
    r = client.post(BASE, headers=h, json={"url": url or _url("ep"), "event_types": events, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _deliveries(endpoint_id, event_type=None):
    from app.models.webhook import WebhookDelivery
    with SessionLocal() as db:
        q = db.query(WebhookDelivery).filter(WebhookDelivery.endpoint_id == endpoint_id)
        if event_type:
            q = q.filter(WebhookDelivery.event_type == event_type)
        rows = q.order_by(WebhookDelivery.created_at, WebhookDelivery.delivery_id).all()
        return [{"delivery_id": d.delivery_id, "event_type": d.event_type, "status": d.status, "attempts": d.attempts,
                 "event_id": d.event_id, "payload": json.loads(d.payload), "response_code": d.response_code,
                 "last_error": d.last_error, "next_attempt_at": d.next_attempt_at, "tenant_id": d.tenant_id,
                 "is_test": d.is_test, "redelivery_of": d.redelivery_of} for d in rows]


def _contact(client, h, local="c"):
    r = client.post("/contacts", headers=h, json={"email": f"{local}.{next(_n)}.{RUN}@acme{RUN}.example.com",
                                                  "first_name": "Wendy", "last_name": "Hook"})
    assert r.status_code == 201, r.text
    return r.json()


def _deliver():
    from app.services import webhooks as svc
    return svc.deliver_due(SessionLocal)


def _off(client, h, ep):
    client.patch(f"{BASE}/{ep['endpoint_id']}", headers=h, json={"active": False})


# ── Endpoint management ──────────────────────────────────────

def test_wh_f01_admins_only(client, ws):
    """WH-F01 only SUPER_ADMIN / ADMIN manage webhooks."""
    ah = ws["agent"]["headers"]
    assert client.get(BASE, headers=ah).status_code == 403
    assert client.get(f"{BASE}/event-types", headers=ah).status_code == 403
    assert client.post(BASE, headers=ah, json={"url": _url("x"), "event_types": ["contact.created"]}).status_code == 403
    assert client.get(BASE).status_code in (401, 403)
    assert client.get(BASE, headers=ws["admin"]["headers"]).status_code == 200
    types = client.get(f"{BASE}/event-types", headers=ws["h"]).json()
    assert {t["type"] for t in types} >= {"contact.created", "deal.won", "email.replied", "campaign.paused"}


def test_wh_f02_secret_shown_once_and_encrypted_at_rest(client, ws, rx):
    """WH-F02 create returns the secret once; reads show only a hint; stored encrypted."""
    ep = _create(client, ws["h"], ["deal.won", "contact.created", "deal.won"], description="  CRM sync  ")
    assert ep["secret"].startswith("whsec_") and len(ep["secret"]) >= 40
    assert ep["event_types"] == ["contact.created", "deal.won"] and ep["description"] == "CRM sync" and ep["active"]
    got = client.get(f"{BASE}/{ep['endpoint_id']}", headers=ws["h"]).json()
    listed = next(i for i in client.get(BASE, headers=ws["h"]).json()["items"] if i["endpoint_id"] == ep["endpoint_id"])
    for doc in (got, listed):
        assert "secret" not in doc and doc["secret_hint"].endswith(ep["secret"][-4:])
    stored = sql("SELECT secret FROM webhook_endpoints WHERE endpoint_id = :e", e=ep["endpoint_id"])
    assert stored.startswith("enc:v1:") and ep["secret"] not in stored
    _off(client, ws["h"], ep)


@pytest.mark.parametrize("payload", [
    {"url": "ftp://files.example.com/x", "event_types": ["contact.created"]},
    {"url": "http://169.254.169.254/latest/meta-data/", "event_types": ["contact.created"]},
    {"url": "https://user:pw@hooks.example.com/x", "event_types": ["contact.created"]},
    {"url": "https://metadata.google.internal/x", "event_types": ["contact.created"]},
    {"url": "https://hooks.example.com/x", "event_types": ["contact.exploded"]},
    {"url": "https://hooks.example.com/x", "event_types": []},
    {"url": "https://hooks.example.com/" + "a" * 2001, "event_types": ["contact.created"]},
    {"url": "https://hooks.example.com/x", "event_types": ["contact.created"], "description": "d" * 256},
    {"event_types": ["contact.created"]},
    {"url": "https://hooks.example.com/x", "event_types": ["contact.created"] * 50},
])
def test_wh_f03_invalid_endpoints_rejected(client, ws, rx, payload):
    """WH-F03 bad scheme, metadata address, credentials, unknown / empty events, oversized fields."""
    r = client.post(BASE, headers=ws["h"], json=payload)
    assert r.status_code == 422, r.text


def test_wh_f04_production_rules_https_and_public_addresses(client, ws, rx, monkeypatch):
    """WH-F04 outside development: https only, private / loopback targets refused (also via DNS)."""
    from app.core.config import settings
    from app.utils import webhooks as wh
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    h = ws["h"]
    assert client.post(BASE, headers=h, json={"url": "http://hooks.example.com/x", "event_types": ["contact.created"]}).status_code == 422
    assert client.post(BASE, headers=h, json={"url": "https://10.0.0.8/x", "event_types": ["contact.created"]}).status_code == 422
    assert client.post(BASE, headers=h, json={"url": "https://localhost/x", "event_types": ["contact.created"]}).status_code == 422
    monkeypatch.setattr(wh, "socket_resolver", lambda host, port: ["127.0.0.1"])
    r = client.post(BASE, headers=h, json={"url": "https://sneaky.example.com/x", "event_types": ["contact.created"]})
    assert r.status_code == 422 and "sneaky.example.com" in r.json()["detail"]
    monkeypatch.setattr(wh, "socket_resolver", lambda host, port: [PUBLIC_IP])
    ep = _create(client, h, ["contact.created"])
    # An endpoint whose DNS later points inside is blocked at send time, without connecting
    monkeypatch.setattr(wh, "socket_resolver", lambda host, port: ["10.1.1.1"])
    r = client.post(f"{BASE}/{ep['endpoint_id']}/test", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "FAILED" and r.json()["last_error"].startswith("Blocked")
    assert rx.calls == []
    client.delete(f"{BASE}/{ep['endpoint_id']}", headers=h)


def test_wh_f05_tenant_isolation(client, ws, rx):
    """WH-F05 another workspace cannot see, change, test or redeliver an endpoint."""
    ep = _create(client, ws["h"], ["contact.created"])
    test = client.post(f"{BASE}/{ep['endpoint_id']}/test", headers=ws["h"]).json()
    bh = ws["other"]["headers"]
    eid = ep["endpoint_id"]
    assert client.get(f"{BASE}/{eid}", headers=bh).status_code == 404
    assert client.patch(f"{BASE}/{eid}", headers=bh, json={"active": False}).status_code == 404
    assert client.post(f"{BASE}/{eid}/rotate-secret", headers=bh).status_code == 404
    assert client.post(f"{BASE}/{eid}/test", headers=bh).status_code == 404
    assert client.get(f"{BASE}/{eid}/deliveries", headers=bh).status_code == 404
    assert client.get(f"{BASE}/{eid}/deliveries/{test['delivery_id']}", headers=bh).status_code == 404
    assert client.post(f"{BASE}/{eid}/deliveries/{test['delivery_id']}/redeliver", headers=bh).status_code == 404
    assert client.delete(f"{BASE}/{eid}", headers=bh).status_code == 404
    assert eid not in [i["endpoint_id"] for i in client.get(BASE, headers=bh).json()["items"]]
    assert client.get(f"{BASE}/{eid}", headers=ws["h"]).json()["active"] is True
    _off(client, ws["h"], ep)


# ── Events written to the outbox ─────────────────────────────

def test_wh_f06_contact_created_goes_to_subscribed_active_endpoints_only(client, ws, rx):
    """WH-F06 contact.created: one PENDING delivery per subscribed active endpoint of the same workspace."""
    h = ws["h"]
    sub = _create(client, h, ["contact.created"])
    unsub = _create(client, h, ["deal.won"])
    off = _create(client, h, ["contact.created"], active=False)
    foreign = _create(client, ws["other"]["headers"], ["contact.created"])
    c = _contact(client, h)
    rows = _deliveries(sub["endpoint_id"], "contact.created")
    mine = [d for d in rows if d["payload"]["data"]["contact_id"] == c["prospect_id"]]
    assert len(mine) == 1
    d = mine[0]
    assert d["status"] == "PENDING" and d["attempts"] == 0 and d["tenant_id"] == ws["tid"]
    assert d["payload"]["type"] == "contact.created" and d["payload"]["id"] == d["event_id"]
    assert d["payload"]["workspace_id"] == ws["tid"] and d["payload"]["data"]["email"] == c["email"]
    for ep in (unsub, off, foreign):
        assert not [x for x in _deliveries(ep["endpoint_id"]) if x["payload"]["data"].get("contact_id") == c["prospect_id"]]
    for ep in (sub, unsub):
        _off(client, h, ep)
    _off(client, ws["other"]["headers"], foreign)


def test_wh_f07_contact_updated_and_deleted(client, ws, rx):
    """WH-F07 contact.updated carries the changed fields; contact.deleted on soft delete."""
    h = ws["h"]
    ep = _create(client, h, ["contact.updated", "contact.deleted"])
    c = _contact(client, h)
    assert client.patch(f"/contacts/{c['prospect_id']}", headers=h, json={"first_name": "Wanda"}).status_code == 200
    upd = [d for d in _deliveries(ep["endpoint_id"], "contact.updated") if d["payload"]["data"]["contact_id"] == c["prospect_id"]]
    assert len(upd) == 1 and upd[0]["payload"]["data"]["changes"]["first_name"] == {"from": "Wendy", "to": "Wanda"}
    assert client.delete(f"/contacts/{c['prospect_id']}", headers=h).status_code == 200
    dele = [d for d in _deliveries(ep["endpoint_id"], "contact.deleted") if d["payload"]["data"]["contact_id"] == c["prospect_id"]]
    assert len(dele) == 1 and dele[0]["payload"]["data"]["email"] == c["email"]
    _off(client, h, ep)


def test_wh_f08_lead_created_and_qualified(client, ws, rx):
    """WH-F08 lead.created on a new lead; lead.qualified when it reaches SQL."""
    h = ws["h"]
    ep = _create(client, h, ["lead.created", "lead.qualified"])
    c = _contact(client, h, "lead")
    r = client.post("/leads", headers=h, json={"prospect_id": c["prospect_id"]})
    assert r.status_code == 201, r.text
    lid = r.json()["lead_id"]
    created = [d for d in _deliveries(ep["endpoint_id"], "lead.created") if d["payload"]["data"]["lead_id"] == lid]
    assert len(created) == 1 and created[0]["payload"]["data"]["contact_id"] == c["prospect_id"]
    assert not [d for d in _deliveries(ep["endpoint_id"], "lead.qualified") if d["payload"]["data"]["lead_id"] == lid]
    r = client.patch(f"/leads/{lid}", headers=h, json={
        "qualification": {"budget": True, "authority": True, "need": True, "timeline": True}, "stage": "SQL"})
    assert r.status_code == 200, r.text
    q = [d for d in _deliveries(ep["endpoint_id"], "lead.qualified") if d["payload"]["data"]["lead_id"] == lid]
    assert len(q) == 1 and q[0]["payload"]["data"]["stage"] == "SQL"
    _off(client, h, ep)


def test_wh_f09_deal_created_stage_changed_won(client, ws, rx):
    """WH-F09 deal.created, deal.stage_changed and deal.won from the opportunity API."""
    h = ws["h"]
    ep = _create(client, h, ["deal.created", "deal.stage_changed", "deal.won", "deal.lost"])
    stages = client.get("/sales/stages", headers=h).json()
    open_stages = [s for s in stages if s["status"] == "OPEN"]
    won = next(s for s in stages if s["status"] == "WON")
    r = client.post("/opportunities", headers=h, json={"name": f"Deal {RUN}", "stage_id": open_stages[0]["stage_id"], "amount": 5000})
    assert r.status_code == 201, r.text
    oid = r.json()["opportunity_id"]

    def of(t):
        return [d for d in _deliveries(ep["endpoint_id"], t) if d["payload"]["data"]["deal_id"] == oid]
    assert len(of("deal.created")) == 1 and of("deal.created")[0]["payload"]["data"]["amount"] in ("5000.00", "5000")
    if len(open_stages) > 1:
        assert client.patch(f"/opportunities/{oid}", headers=h, json={"stage_id": open_stages[1]["stage_id"]}).status_code == 200
        sc = of("deal.stage_changed")
        assert len(sc) == 1 and sc[0]["payload"]["data"]["previous_stage"] == open_stages[0]["name"]
        assert sc[0]["payload"]["data"]["stage"] == open_stages[1]["name"]
    r = client.patch(f"/opportunities/{oid}", headers=h, json={"stage_id": won["stage_id"], "closed_reason": "Best fit"})
    assert r.status_code == 200, r.text
    assert len(of("deal.won")) == 1 and of("deal.won")[0]["payload"]["data"]["status"] == "WON"
    assert of("deal.lost") == []
    _off(client, h, ep)


def test_wh_f10_outbox_rows_roll_back_with_the_change(client, ws, rx):
    """WH-F10 the event is written in the caller's transaction: rollback leaves no delivery."""
    from app.services import webhooks as svc
    ep = _create(client, ws["h"], ["contact.created"])
    with SessionLocal() as db:
        assert svc.emit_event(db, ws["tid"], "contact.created", {"contact_id": "rolled-back"}) == 1
        db.rollback()
    assert not [d for d in _deliveries(ep["endpoint_id"]) if d["payload"]["data"].get("contact_id") == "rolled-back"]
    with SessionLocal() as db:
        svc.emit_event(db, ws["tid"], "contact.created", {"contact_id": "committed"})
        db.commit()
    assert [d for d in _deliveries(ep["endpoint_id"]) if d["payload"]["data"].get("contact_id") == "committed"]
    _off(client, ws["h"], ep)


def test_wh_f11_suppression_events(client, ws, rx):
    """WH-F11 email.unsubscribed once per new unsubscribe; email.bounced on a hard bounce."""
    from app.services.suppression import suppress
    ep = _create(client, ws["h"], ["email.unsubscribed", "email.bounced"])
    c = _contact(client, ws["h"], "unsub")
    with SessionLocal() as db:
        suppress(db, ws["tid"], c["email"], "Clicked unsubscribe", kind="UNSUBSCRIBE", source="test")
        db.commit()
    with SessionLocal() as db:
        suppress(db, ws["tid"], c["email"], "Clicked unsubscribe again", kind="UNSUBSCRIBE", source="test")
        db.commit()
    uns = [d for d in _deliveries(ep["endpoint_id"], "email.unsubscribed") if d["payload"]["data"]["email"] == c["email"].lower()]
    assert len(uns) == 1 and uns[0]["payload"]["data"]["contact_ids"] == [c["prospect_id"]]
    c2 = _contact(client, ws["h"], "bounce")
    with SessionLocal() as db:
        suppress(db, ws["tid"], c2["email"], "Hard bounce: General", kind="HARD_BOUNCE")
        db.commit()
    assert len([d for d in _deliveries(ep["endpoint_id"], "email.bounced") if d["payload"]["data"]["email"] == c2["email"].lower()]) == 1
    _off(client, ws["h"], ep)


def test_wh_f12_campaign_paused(client, ws, rx):
    """WH-F12 pausing a campaign raises campaign.paused."""
    h = ws["h"]
    ep = _create(client, h, ["campaign.paused"])
    r = client.post("/campaigns", headers=h, json={"campaign_name": f"WH campaign {RUN}"})
    assert r.status_code == 201, r.text
    cid = r.json()["campaign_id"]
    sql("UPDATE campaigns SET status = 'ACTIVE' WHERE campaign_id = :c", c=cid)
    r = client.post(f"/campaigns/{cid}/pause", headers=h, params={"reason": "Holiday"})
    assert r.status_code == 200, r.text
    rows = [d for d in _deliveries(ep["endpoint_id"], "campaign.paused") if d["payload"]["data"]["campaign_id"] == cid]
    assert len(rows) == 1 and rows[0]["payload"]["data"]["reason"] == "Holiday"
    _off(client, h, ep)


# ── Delivery ─────────────────────────────────────────────────

def test_wh_f13_worker_delivers_signed_request(client, ws, rx):
    """WH-F13 the job POSTs the stored body, signed with the endpoint secret; success is recorded."""
    from app.utils import webhooks as wh
    ep = _create(client, ws["h"], ["contact.created"], url=_url("signed"))
    c = _contact(client, ws["h"])
    _deliver()
    calls = rx.for_url("signed")
    assert len(calls) == 1
    call = calls[0]
    doc = json.loads(call["body"])
    assert doc["data"]["contact_id"] == c["prospect_id"] and call["target"].ip == PUBLIC_IP
    assert call["timeout"] == wh.REQUEST_TIMEOUT_SECONDS
    assert wh.verify(ep["secret"], call["headers"]["X-Outreach-Signature"], call["body"])
    assert not wh.verify(ep["secret"] + "x", call["headers"]["X-Outreach-Signature"], call["body"])
    assert call["headers"]["X-Outreach-Event"] == "contact.created" and call["headers"]["X-Outreach-Event-Id"] == doc["id"]
    d = _deliveries(ep["endpoint_id"], "contact.created")[-1]
    assert d["status"] == "SUCCEEDED" and d["attempts"] == 1 and d["response_code"] == 204
    assert client.get(f"{BASE}/{ep['endpoint_id']}", headers=ws["h"]).json()["last_success_at"]
    _deliver()
    assert len(rx.for_url("signed")) == 1  # not sent twice
    _off(client, ws["h"], ep)


def test_wh_f14_failure_is_retried_with_backoff(client, ws, rx):
    """WH-F14 a non-2xx answer schedules the next attempt ~1 minute later; nothing is resent before."""
    ep = _create(client, ws["h"], ["contact.created"], url=_url("flaky"))
    rx.code = 500
    _contact(client, ws["h"])
    before = utcnow()
    _deliver()
    d = _deliveries(ep["endpoint_id"])[-1]
    assert d["status"] == "PENDING" and d["attempts"] == 1 and d["response_code"] == 500 and "HTTP 500" in d["last_error"]
    assert before + timedelta(seconds=50) <= d["next_attempt_at"] <= utcnow() + timedelta(seconds=70)
    _deliver()
    assert len(rx.for_url("flaky")) == 1
    # When the time comes, it is sent again and succeeds
    sql("UPDATE webhook_deliveries SET next_attempt_at = UTC_TIMESTAMP() - INTERVAL 1 SECOND WHERE delivery_id = :d", d=d["delivery_id"])
    rx.code = 200
    _deliver()
    d = _deliveries(ep["endpoint_id"])[-1]
    assert d["status"] == "SUCCEEDED" and d["attempts"] == 2
    assert client.get(f"{BASE}/{ep['endpoint_id']}", headers=ws["h"]).json()["consecutive_failures"] == 0
    _off(client, ws["h"], ep)


def test_wh_f15_gives_up_after_the_last_attempt(client, ws, rx):
    """WH-F15 after MAX_ATTEMPTS the delivery is FAILED and no longer retried."""
    from app.utils import webhooks as wh
    ep = _create(client, ws["h"], ["contact.created"], url=_url("dead"))
    rx.code = ConnectionRefusedError("refused")
    _contact(client, ws["h"])
    d = _deliveries(ep["endpoint_id"])[-1]
    sql("UPDATE webhook_deliveries SET attempts = :a WHERE delivery_id = :d", a=wh.MAX_ATTEMPTS - 1, d=d["delivery_id"])
    _deliver()
    d = _deliveries(ep["endpoint_id"])[-1]
    assert d["status"] == "FAILED" and d["attempts"] == wh.MAX_ATTEMPTS and d["next_attempt_at"] is None
    assert "Connection failed" in d["last_error"]
    _off(client, ws["h"], ep)


def test_wh_f16_auto_disable_with_admin_notification(client, ws, rx):
    """WH-F16 N consecutive failures switch the endpoint off, cancel its queue and notify the admins."""
    from app.utils import webhooks as wh
    h = ws["h"]
    ep = _create(client, h, ["contact.created"], url=_url("broken"))
    eid = ep["endpoint_id"]
    rx.code = 503
    _contact(client, h)
    _contact(client, h)
    sql("UPDATE webhook_endpoints SET consecutive_failures = :n WHERE endpoint_id = :e", n=wh.DISABLE_AFTER_FAILURES - 1, e=eid)
    # One due now; the other is due later and must be cancelled
    first, second = _deliveries(eid)
    sql("UPDATE webhook_deliveries SET next_attempt_at = UTC_TIMESTAMP() + INTERVAL 1 HOUR WHERE delivery_id = :d", d=second["delivery_id"])
    _deliver()
    got = client.get(f"{BASE}/{eid}", headers=h).json()
    assert got["active"] is False and "consecutive" in got["disabled_reason"] and got["disabled_at"]
    rows = {d["delivery_id"]: d for d in _deliveries(eid)}
    assert rows[second["delivery_id"]]["status"] == "CANCELLED"
    for uid in (ws["admin"]["user_id"], sql("SELECT user_id FROM users WHERE email = :e", e=ws["owner"]["email"])):
        assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'WEBHOOK_DISABLED'", u=uid) >= 1
    assert sql("SELECT COUNT(*) FROM notifications WHERE user_id = :u AND kind = 'WEBHOOK_DISABLED'", u=ws["agent"]["user_id"]) == 0
    # Disabled: new events are not queued for it
    n = len(_deliveries(eid))
    _contact(client, h)
    assert len(_deliveries(eid)) == n
    # Turning it back on starts clean
    got = client.patch(f"{BASE}/{eid}", headers=h, json={"active": True}).json()
    assert got["active"] and got["consecutive_failures"] == 0 and got["disabled_reason"] is None
    _off(client, h, ep)


def test_wh_f17_test_event(client, ws, rx):
    """WH-F17 'send test' delivers webhook.test now; a failure does not count toward auto-disable."""
    h = ws["h"]
    ep = _create(client, h, ["deal.won"], url=_url("test"))
    r = client.post(f"{BASE}/{ep['endpoint_id']}/test", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "SUCCEEDED" and body["is_test"] and body["event_type"] == "webhook.test"
    assert body["payload"]["data"]["endpoint_id"] == ep["endpoint_id"] and body["max_attempts"] == 1
    rx.code = 404
    body = client.post(f"{BASE}/{ep['endpoint_id']}/test", headers=h).json()
    assert body["status"] == "FAILED" and body["response_code"] == 404 and body["attempts"] == 1
    assert client.get(f"{BASE}/{ep['endpoint_id']}", headers=h).json()["consecutive_failures"] == 0
    _deliver()
    assert len(rx.for_url("test")) == 2  # tests are not retried by the job
    _off(client, h, ep)


def test_wh_f18_rotate_secret(client, ws, rx):
    """WH-F18 rotating returns a new secret once; deliveries are signed with it."""
    from app.utils import webhooks as wh
    h = ws["h"]
    ep = _create(client, h, ["deal.won"], url=_url("rotate"))
    r = client.post(f"{BASE}/{ep['endpoint_id']}/rotate-secret", headers=h)
    assert r.status_code == 200
    new = r.json()["secret"]
    assert new != ep["secret"] and r.json()["secret_hint"].endswith(new[-4:])
    client.post(f"{BASE}/{ep['endpoint_id']}/test", headers=h)
    call = rx.for_url("rotate")[-1]
    assert wh.verify(new, call["headers"]["X-Outreach-Signature"], call["body"])
    assert not wh.verify(ep["secret"], call["headers"]["X-Outreach-Signature"], call["body"])
    _off(client, h, ep)


def test_wh_f19_delivery_log_and_redeliver(client, ws, rx):
    """WH-F19 the log lists deliveries (filters, bounds, payload); redeliver queues the same event again."""
    h = ws["h"]
    ep = _create(client, h, ["contact.created"], url=_url("log"))
    eid = ep["endpoint_id"]
    _contact(client, h)
    pending = _deliveries(eid)[-1]
    assert client.post(f"{BASE}/{eid}/deliveries/{pending['delivery_id']}/redeliver", headers=h).status_code == 409
    _deliver()
    log = client.get(f"{BASE}/{eid}/deliveries", headers=h).json()
    assert log["total"] >= 1 and log["items"][0]["status"] == "SUCCEEDED" and "payload" not in log["items"][0]
    assert client.get(f"{BASE}/{eid}/deliveries", headers=h, params={"status": "failed"}).json()["total"] == 0
    assert client.get(f"{BASE}/{eid}/deliveries", headers=h, params={"limit": 101}).status_code == 422
    assert client.get(f"{BASE}/{eid}/deliveries", headers=h, params={"status": "BOGUS"}).status_code == 422
    one = client.get(f"{BASE}/{eid}/deliveries/{pending['delivery_id']}", headers=h).json()
    assert one["payload"]["id"] == pending["event_id"]
    r = client.post(f"{BASE}/{eid}/deliveries/{pending['delivery_id']}/redeliver", headers=h)
    assert r.status_code == 201, r.text
    copy = r.json()
    assert copy["status"] == "PENDING" and copy["event_id"] == pending["event_id"] and copy["redelivery_of"] == pending["delivery_id"]
    _deliver()
    bodies = [json.loads(c["body"])["id"] for c in rx.for_url("log")]
    assert bodies.count(pending["event_id"]) == 2  # same event id: receivers can de-duplicate
    assert client.post(f"{BASE}/{eid}/deliveries/{uuid.uuid4()}/redeliver", headers=h).status_code == 404
    _off(client, h, ep)
    assert client.post(f"{BASE}/{eid}/deliveries/{pending['delivery_id']}/redeliver", headers=h).status_code == 409


def test_wh_f20_update_and_delete(client, ws, rx):
    """WH-F20 edit URL / events; manual switch-off cancels the queue; delete removes the log."""
    h = ws["h"]
    ep = _create(client, h, ["contact.created"])
    eid = ep["endpoint_id"]
    r = client.patch(f"{BASE}/{eid}", headers=h, json={"url": _url("moved"), "event_types": ["deal.lost", "contact.created"]})
    assert r.status_code == 200 and r.json()["event_types"] == ["contact.created", "deal.lost"] and "moved" in r.json()["url"]
    assert client.patch(f"{BASE}/{eid}", headers=h, json={"url": "ftp://x/"}).status_code == 422
    assert client.patch(f"{BASE}/{eid}", headers=h, json={"event_types": []}).status_code == 422
    _contact(client, h)
    r = client.patch(f"{BASE}/{eid}", headers=h, json={"active": False})
    assert r.json()["active"] is False
    assert {d["status"] for d in _deliveries(eid)} == {"CANCELLED"}
    assert sql("SELECT COUNT(*) FROM notifications WHERE link = '/app/webhooks' AND body LIKE :u", u=f"%{r.json()['url'][:60]}%") == 0
    assert client.delete(f"{BASE}/{eid}", headers=h).status_code == 200
    assert client.get(f"{BASE}/{eid}", headers=h).status_code == 404
    assert sql("SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id = :e", e=eid) == 0


def test_wh_f21_endpoint_limit_per_workspace(client, rx):
    """WH-F21 a workspace can have at most MAX_ENDPOINTS_PER_TENANT endpoints."""
    from app.utils import webhooks as wh
    t = register_tenant(client, "whlim")
    for _ in range(wh.MAX_ENDPOINTS_PER_TENANT):
        _create(client, t["headers"], ["contact.created"], active=False)
    r = client.post(BASE, headers=t["headers"], json={"url": _url("over"), "event_types": ["contact.created"]})
    assert r.status_code == 409


def test_wh_f22_documented_in_openapi(client):
    """WH-F22 RP-106b: the webhook subscription API is in the OpenAPI document."""
    r = client.get("/openapi.json")
    if r.status_code == 404:
        pytest.skip("OpenAPI disabled in this environment")
    paths = r.json()["paths"]
    assert "/integrations/webhooks" in paths and "/integrations/webhooks/{endpoint_id}/deliveries/{delivery_id}/redeliver" in paths
