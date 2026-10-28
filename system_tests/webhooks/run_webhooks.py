"""Outbound webhooks (BRD v2.0 BR-SF-20 / RP-106b) against the live stack.

Starts a small HTTP receiver on this host (0.0.0.0:WEBHOOK_RX_PORT, default 18765) that the API and
worker containers reach at http://WEBHOOK_RX_HOST:PORT (default 172.18.0.1, the compose network
gateway). The stack runs with ENVIRONMENT=development, where http:// and private receivers are
allowed; metadata / link-local / unspecified addresses are refused everywhere.

Event delivery is done by the worker's "webhooks" loop (every 15 s); waits are bounded. WH-007 waits for the
worker itself; the other cases flush the outbox with one "webhooks" iteration under the worker's own lock
(common.run_job -> app.jobs.run_once), so "nothing arrived" is checked after a full pass instead of a 35 s sleep.
Signatures are verified here independently of the app code (HMAC-SHA256 over "<t>.<body>").

    python3 system_tests/webhooks/run_webhooks.py
"""
import hashlib
import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import Results, Workspace, req, run_job, sql  # noqa: E402

RX_PORT = int(os.environ.get("WEBHOOK_RX_PORT", "18765"))
RX_HOST = os.environ.get("WEBHOOK_RX_HOST", "172.18.0.1")
RX = f"http://{RX_HOST}:{RX_PORT}"
WAIT = int(os.environ.get("WEBHOOK_WAIT_SECONDS", "90"))
BASE = "/integrations/webhooks"

# ── Receiver ─────────────────────────────────────────────────
RECEIVED = []          # {"path", "headers", "body", "at"}
FLIP = {}              # name -> status code for /flip/<name>
_lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 1024 * 1024))
        with _lock:
            RECEIVED.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                             "body": body, "at": time.time()})
        kind, _, name = self.path.strip("/").partition("/")
        status, extra = 200, {}
        if kind == "fail":
            status = 500
        elif kind == "flip":
            status = FLIP.get(name, 500)
        elif kind == "redirect":
            status, extra = 302, {"Location": f"{RX}/ok/redirected-{name}"}
        elif kind == "slow":
            time.sleep(13)
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("0.0.0.0", RX_PORT), _Handler)
server.daemon_threads = True
threading.Thread(target=server.serve_forever, daemon=True).start()


def got(path_part, event_type=None):
    with _lock:
        rows = [r for r in RECEIVED if path_part in r["path"]]
    if event_type:
        rows = [r for r in rows if r["headers"].get("x-outreach-event") == event_type]
    return rows


def wait_for(fn, timeout=WAIT, step=0.5):
    end = time.time() + timeout
    while True:
        value = fn()
        if value or time.time() > end:
            return value
        time.sleep(step)


def flush(fallback_sleep=0):
    """One webhooks pass now, under the worker loop's lock: when it returns True every delivery that was due when
    it was called has been attempted and recorded. On an image without app.jobs.run_once, sleep fallback_sleep s
    (the previous fixed wait: a little over two worker cycles) instead."""
    ok, out = run_job("webhooks", timeout=60)
    if not ok:
        print(f"note: webhooks flush unavailable ({out.strip()[-160:]!r}); waiting {fallback_sleep}s instead")
        time.sleep(fallback_sleep)
    return ok


def latest_delivery(eid, done, timeout=15):
    """Newest delivery of the endpoint once done(d) holds (bounded poll; the attempt is committed after the send)."""
    end = time.time() + timeout
    while True:
        s, log = deliveries(eid)
        d = (log or {}).get("items", [{}])[0] if s == 200 else {}
        if done(d) or time.time() > end:
            return d
        time.sleep(0.5)


def signed_ok(secret, row, tolerance=300):
    header = row["headers"].get("x-outreach-signature", "")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if "t" not in parts or "v1" not in parts or abs(time.time() - int(parts["t"])) > tolerance:
        return False
    mac = hmac.new(secret.encode(), parts["t"].encode() + b"." + row["body"], hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, parts["v1"])


def event(row):
    try:
        return json.loads(row["body"])
    except ValueError:
        return {}


# ── Setup ────────────────────────────────────────────────────
R = Results("webhooks", os.path.dirname(os.path.abspath(__file__)))
W = Workspace("wh")
OWNER = W.token
AGENT = W.add_user("AGENT", "Hookagent")
W2 = Workspace("wh-b")
rid = W.rid


def create(url, events, token=OWNER, **extra):
    return req("POST", BASE, {"url": url, "event_types": events, **extra}, token)


def contact(local, token=OWNER, **kw):
    s, b = req("POST", "/contacts", {"email": f"{local}.{rid}@wh-{rid}.io", "first_name": "Hook", **kw}, token)
    assert s == 201, (s, b)
    return b


def deliveries(eid, token=OWNER, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return req("GET", f"{BASE}/{eid}/deliveries" + (f"?{q}" if q else ""), None, token)


# ── WH-001..006 API, validation, secret ──────────────────────
s, spec = req("GET", "/openapi.json")
paths = list((spec or {}).get("paths", {})) if isinstance(spec, dict) else []
hooks = [p for p in paths if "webhook" in p.lower() and "ses" not in p.lower()]
R.check("WH-001", "RP-106b: outbound webhook subscription API is documented (OpenAPI)",
        s == 200 and f"{BASE}" in hooks and f"{BASE}/{{endpoint_id}}/test" in hooks, (s, hooks[:10]))

s1, _ = req("GET", BASE, None, AGENT["token"])
s2, _ = create(f"{RX}/ok/agent", ["contact.created"], AGENT["token"])
R.check("WH-002", "Agents cannot list or register webhooks (403)", s1 == 403 and s2 == 403, (s1, s2))

s, ep = create(f"{RX}/ok/main-{rid}", ["contact.created", "contact.updated", "lead.created", "lead.qualified",
                                        "deal.created", "deal.stage_changed", "deal.won"], description="System test")
MAIN = ep if s == 201 else {}
SECRET = MAIN.get("secret", "")
s2, one = req("GET", f"{BASE}/{MAIN.get('endpoint_id')}", None, OWNER)
R.check("WH-003", "Admin registers an endpoint; the secret is returned once, reads show a hint only",
        s == 201 and SECRET.startswith("whsec_") and s2 == 200 and "secret" not in one
        and one.get("secret_hint", "").endswith(SECRET[-4:]), (s, ep, s2, one))

stored = sql(f"SELECT secret FROM webhook_endpoints WHERE endpoint_id='{MAIN.get('endpoint_id')}'")
R.check("WH-004", "Secret is encrypted at rest", stored.startswith("enc:v1:") and SECRET not in stored, stored[:20])

bad = {
    "metadata IP": "http://169.254.169.254/latest/meta-data/",
    "metadata host": "http://metadata.google.internal/x",
    "unspecified": "http://0.0.0.0:8000/x",
    "link-local v6": "http://[fe80::1]/x",
    "ftp scheme": "ftp://example.com/x",
    "credentials": f"http://user:pw@{RX_HOST}:{RX_PORT}/ok/x",
    "file scheme": "file:///etc/passwd",
}
codes = {k: create(u, ["contact.created"])[0] for k, u in bad.items()}
codes["unknown event"] = create(f"{RX}/ok/x", ["contact.exploded"])[0]
codes["no events"] = create(f"{RX}/ok/x", [])[0]
codes["2001-char url"] = create(f"{RX}/ok/" + "a" * 2001, ["contact.created"])[0]
R.check("WH-005", "SSRF / input checks: metadata, link-local, unspecified, non-http(s), credentials, bad events -> 422",
        all(c == 422 for c in codes.values()), codes)

s, t = req("POST", f"{BASE}/{MAIN.get('endpoint_id')}/test", None, OWNER)
rows = got(f"main-{rid}", "webhook.test")
R.check("WH-006", "Send test: delivered synchronously, signed with the secret, logged as a test delivery",
        s == 200 and t.get("status") == "SUCCEEDED" and t.get("is_test") and rows and signed_ok(SECRET, rows[-1]),
        (s, t, len(rows)))

# ── WH-007..011 real events through the worker ───────────────
c = contact("created")
rows = wait_for(lambda: [r for r in got(f"main-{rid}", "contact.created") if event(r).get("data", {}).get("contact_id") == c["prospect_id"]])
ok = bool(rows) and signed_ok(SECRET, rows[0])
ev = event(rows[0]) if rows else {}
R.check("WH-007", "contact.created reaches the receiver via the worker, signed, with event headers",
        ok and ev.get("type") == "contact.created" and rows[0]["headers"].get("x-outreach-event-id") == ev.get("id")
        and ev.get("data", {}).get("email") == c["email"] and rows[0]["headers"].get("content-type") == "application/json",
        (len(rows), ev))

req("PATCH", f"/contacts/{c['prospect_id']}", {"first_name": "Changed"}, OWNER)
flush()
rows = wait_for(lambda: [r for r in got(f"main-{rid}", "contact.updated") if event(r)["data"].get("contact_id") == c["prospect_id"]])
R.check("WH-008", "contact.updated carries the changed field (from / to)",
        rows and event(rows[0])["data"]["changes"].get("first_name") == {"from": "Hook", "to": "Changed"},
        rows and event(rows[0]))

s, lead = req("POST", "/leads", {"prospect_id": c["prospect_id"]}, OWNER)
lid = (lead or {}).get("lead_id")
s2, _ = req("PATCH", f"/leads/{lid}", {"qualification": {"budget": True, "authority": True, "need": True, "timeline": True},
                                        "stage": "SQL"}, OWNER)
flush()
rows_c = wait_for(lambda: [r for r in got(f"main-{rid}", "lead.created") if event(r)["data"].get("lead_id") == lid])
rows_q = wait_for(lambda: [r for r in got(f"main-{rid}", "lead.qualified") if event(r)["data"].get("lead_id") == lid])
R.check("WH-009", "lead.created and lead.qualified (SQL) are delivered", s == 201 and s2 == 200 and rows_c and rows_q,
        (s, s2, len(rows_c or []), len(rows_q or [])))

s, stages = req("GET", "/sales/stages", None, OWNER)
open_st = [x for x in stages if x["status"] == "OPEN"] if s == 200 else []
won = next((x for x in stages if x["status"] == "WON"), None) if s == 200 else None
s, opp = req("POST", "/opportunities", {"name": f"WH deal {rid}", "stage_id": open_st[0]["stage_id"], "amount": 1200}, OWNER)
oid = (opp or {}).get("opportunity_id")
s2, _ = req("PATCH", f"/opportunities/{oid}", {"stage_id": won["stage_id"], "closed_reason": "Fit"}, OWNER)


def deal_rows(t):
    return [r for r in got(f"main-{rid}", t) if event(r)["data"].get("deal_id") == oid]


flush()
rows_w = wait_for(lambda: deal_rows("deal.won"))
R.check("WH-010", "deal.created, deal.stage_changed and deal.won are delivered",
        s == 201 and s2 == 200 and deal_rows("deal.created") and deal_rows("deal.stage_changed") and rows_w
        and event(rows_w[0])["data"]["status"] == "WON", (s, s2, [len(deal_rows(t)) for t in ("deal.created", "deal.stage_changed", "deal.won")]))

before = len(got(f"main-{rid}"))
contact("deleted-unsubscribed")  # contact.created is subscribed; contact.deleted is not
s, _ = req("DELETE", f"/contacts/{c['prospect_id']}", None, OWNER)
flush(fallback_sleep=35)
R.check("WH-011", "Events the endpoint is not subscribed to are not sent (contact.deleted)",
        not got(f"main-{rid}", "contact.deleted") and len(got(f"main-{rid}")) > before, (s, len(got(f"main-{rid}")) - before))

# ── WH-012..016 failures, retries, auto-disable ──────────────
s, flaky = create(f"{RX}/flip/flaky-{rid}", ["contact.created"])
FID = flaky.get("endpoint_id")
contact("flaky")
flush()
first = wait_for(lambda: got(f"flaky-{rid}"))
d = latest_delivery(FID, lambda x: (x.get("attempts") or 0) >= 1)
R.check("WH-012", "A 500 answer is recorded and retried later (attempt 1 of 6, next attempt ~1 min)",
        first and d.get("status") == "PENDING" and d.get("attempts") == 1 and d.get("response_code") == 500
        and d.get("next_attempt_at"), d)

FLIP[f"flaky-{rid}"] = 200
sql(f"UPDATE webhook_deliveries SET next_attempt_at = UTC_TIMESTAMP() WHERE delivery_id = '{d.get('delivery_id')}'")
flush()
wait_for(lambda: len(got(f"flaky-{rid}")) >= 2)
d2 = latest_delivery(FID, lambda x: (x.get("attempts") or 0) >= 2)
R.check("WH-013", "The retry succeeds once the receiver recovers (same event id)",
        d2.get("status") == "SUCCEEDED" and d2.get("attempts") == 2
        and len({event(r).get("id") for r in got(f"flaky-{rid}")}) == 1, d2)

s, r1 = req("POST", f"{BASE}/{FID}/deliveries/{d2.get('delivery_id')}/redeliver", None, OWNER)
flush()
wait_for(lambda: len(got(f"flaky-{rid}")) >= 3)
R.check("WH-014", "Redeliver queues the same event again and the worker sends it",
        s == 201 and r1.get("redelivery_of") == d2.get("delivery_id") and len(got(f"flaky-{rid}")) >= 3
        and event(got(f"flaky-{rid}")[-1]).get("id") == event(got(f"flaky-{rid}")[0]).get("id"), (s, r1))

s, broken = create(f"{RX}/fail/broken-{rid}", ["contact.created"])
BID = broken.get("endpoint_id")
sql(f"UPDATE webhook_endpoints SET consecutive_failures = 19 WHERE endpoint_id = '{BID}'")
contact("broken")
flush()
wait_for(lambda: req("GET", f"{BASE}/{BID}", None, OWNER)[1].get("active") is False)
s, b = req("GET", f"{BASE}/{BID}", None, OWNER)
s2, notes = req("GET", "/notifications", None, OWNER)
items = notes.get("items", notes if isinstance(notes, list) else []) if isinstance(notes, (dict, list)) else []
R.check("WH-015", "After 20 consecutive failures the endpoint is switched off and admins are notified",
        b.get("active") is False and "consecutive" in (b.get("disabled_reason") or "")
        and any(i.get("kind") == "WEBHOOK_DISABLED" for i in items), (b, [i.get("kind") for i in items][:10]))

n = len(got(f"broken-{rid}"))
contact("after-disable")
flush(fallback_sleep=35)
R.check("WH-016", "A disabled endpoint receives nothing", len(got(f"broken-{rid}")) == n, len(got(f"broken-{rid}")) - n)

# ── WH-017..019 no redirects, timeout, rotation ──────────────
s, red = create(f"{RX}/redirect/r-{rid}", ["deal.lost"])
s, t = req("POST", f"{BASE}/{red.get('endpoint_id')}/test", None, OWNER)
time.sleep(2)
R.check("WH-017", "Redirects are not followed (302 = failed delivery, target never called)",
        t.get("status") == "FAILED" and t.get("response_code") == 302 and not got(f"redirected-r-{rid}"), (t, len(got("redirected"))))

s, slow = create(f"{RX}/slow/s-{rid}", ["deal.lost"])
started = time.time()
s, t = req("POST", f"{BASE}/{slow.get('endpoint_id')}/test", None, OWNER)
took = time.time() - started
R.check("WH-018", "A receiver slower than 10 s times out (failed, request bounded)",
        t.get("status") == "FAILED" and "Timed out" in (t.get("last_error") or "") and took < 20, (t, round(took, 1)))

s, rot = req("POST", f"{BASE}/{MAIN.get('endpoint_id')}/rotate-secret", None, OWNER)
NEW = rot.get("secret", "")
req("POST", f"{BASE}/{MAIN.get('endpoint_id')}/test", None, OWNER)
last = got(f"main-{rid}", "webhook.test")[-1]
R.check("WH-019", "Rotated secret: deliveries are signed with the new secret, not the old one",
        s == 200 and NEW and NEW != SECRET and signed_ok(NEW, last) and not signed_ok(SECRET, last), (s, bool(NEW)))

# ── WH-020..023 isolation, log, delete ───────────────────────
eid = MAIN.get("endpoint_id")
codes = {
    "get": req("GET", f"{BASE}/{eid}", None, W2.token)[0],
    "patch": req("PATCH", f"{BASE}/{eid}", {"active": False}, W2.token)[0],
    "test": req("POST", f"{BASE}/{eid}/test", None, W2.token)[0],
    "rotate": req("POST", f"{BASE}/{eid}/rotate-secret", None, W2.token)[0],
    "log": req("GET", f"{BASE}/{eid}/deliveries", None, W2.token)[0],
    "delete": req("DELETE", f"{BASE}/{eid}", None, W2.token)[0],
}
R.check("WH-020", "Another workspace gets 404 on every endpoint operation", all(v == 404 for v in codes.values()), codes)

n = len(got(f"main-{rid}", "contact.created"))
s, other_c = req("POST", "/contacts", {"email": f"other.{rid}@wh-b-{rid}.io"}, W2.token)
flush(fallback_sleep=35)
R.check("WH-021", "Events of another workspace never reach this workspace's endpoint",
        s == 201 and len(got(f"main-{rid}", "contact.created")) == n, (s, len(got(f"main-{rid}", "contact.created")) - n))

s, log = deliveries(eid, limit=5)
s2, _ = deliveries(eid, limit=101)
s3, _ = deliveries(eid, status="BOGUS")
s4, failed = deliveries(eid, status="FAILED")
first_id = (log or {}).get("items", [{}])[0].get("delivery_id")
s5, det = req("GET", f"{BASE}/{eid}/deliveries/{first_id}", None, OWNER)
R.check("WH-022", "Delivery log: newest first, bounded (limit<=100), status filter, payload on detail",
        s == 200 and len(log.get("items", [])) <= 5 and log.get("total", 0) >= 5 and s2 == 422 and s3 == 422
        and s4 == 200 and s5 == 200 and isinstance(det.get("payload"), dict), (s, s2, s3, s4, s5))

s, _ = req("DELETE", f"{BASE}/{eid}", None, OWNER)
left = sql(f"SELECT COUNT(*) FROM webhook_deliveries WHERE endpoint_id='{eid}'")
s2, _ = req("GET", f"{BASE}/{eid}", None, OWNER)
R.check("WH-023", "Deleting an endpoint removes it and its delivery log", s == 200 and left == "0" and s2 == 404, (s, left, s2))

for e in (FID, BID, red.get("endpoint_id"), slow.get("endpoint_id")):
    req("DELETE", f"{BASE}/{e}", None, OWNER)
server.shutdown()
R.write()
c = {k: sum(1 for r in R.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
print("COUNTS", c)
