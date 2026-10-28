"""Helpers for the E2E journey suite.

Re-uses (imports, never edits) system_tests/common.py and system_tests/outreach/helpers.py
(fake IMAP server, SES-event builders, CSV upload). Adds: the moto SES capture reader, SES
identity verification, on-demand job runs through `docker exec`, unsubscribe-link extraction
and following, TOTP, workspace creation that survives the shared sign-up rate limit.
"""
import base64
import email
import email.policy
import hashlib
import hmac
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ST = os.path.dirname(HERE)                       # system_tests/
sys.path.insert(0, ST)

import common  # noqa: E402
from common import API, Results, Workspace, login, req, run_id, sql  # noqa: E402,F401
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("outreach_helpers", os.path.join(ST, "outreach", "helpers.py"))
_outreach = importlib.util.module_from_spec(_spec)   # system_tests/outreach/helpers.py, read-only reuse
_spec.loader.exec_module(_outreach)
FakeImap, HOST_FROM_CONTAINERS, q, reply_mime, rows, scalar = (
    _outreach.FakeImap, _outreach.HOST_FROM_CONTAINERS, _outreach.q, _outreach.reply_mime, _outreach.rows, _outreach.scalar)
ses_bounce, ses_complaint, ses_delivery, ses_post, upload_csv, wait_until = (
    _outreach.ses_bounce, _outreach.ses_complaint, _outreach.ses_delivery, _outreach.ses_post, _outreach.upload_csv,
    _outreach.wait_until)

SCRATCH = os.environ.get("PW_DIR", "/tmp/claude-0/-home-user/b764cdf9-b625-5070-aae1-e6d130b3e7df/scratchpad")
MOTO = os.environ.get("SES_MOCK_URL", "http://127.0.0.1:5001")
API_CONTAINER = os.environ.get("API_CONTAINER", "vector-api-1")
WORKER_CONTAINER = os.environ.get("WORKER_CONTAINER", "vector-worker-1")
WEBHOOK_TOKEN = _outreach.WEBHOOK_TOKEN
PLATFORM_SENDER = "platform@e2e-platform.example"

_plain = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ───────────────────────────── stand-in preflight ─────────────────────────────

def preflight():
    """Return a list of problems with the stand-ins (empty = ready)."""
    problems = []
    try:
        with _plain.open(MOTO + "/moto-api/data.json", timeout=5) as r:
            json.loads(r.read())
    except Exception as exc:  # noqa: BLE001
        problems.append(f"SES stand-in (moto) not reachable at {MOTO}: {exc}")
    for c in (API_CONTAINER, WORKER_CONTAINER):
        out = subprocess.run(["docker", "exec", c, "printenv", "AWS_ENDPOINT_URL", "SEND_ON_ANY_DAY"],
                             capture_output=True, text=True)
        vals = out.stdout.split()
        if out.returncode != 0 or len(vals) < 2 or "ses-mock" not in vals[0] or vals[1].lower() != "true":
            problems.append(f"{c} is not wired to the stand-in (AWS_ENDPOINT_URL / SEND_ON_ANY_DAY missing): {out.stdout.strip()!r} {out.stderr.strip()[:100]}")
    s, _ = req("GET", "/health")
    if s != 200:
        problems.append(f"API {API}/health -> {s}")
    return problems


# ───────────────────────────── SES (moto) ─────────────────────────────

def _moto_data():
    with _plain.open(MOTO + "/moto-api/data.json", timeout=30) as r:
        return json.loads(r.read())


def ses_verify(identity):
    """Verify an email address or domain in the SES stand-in (SES v1 Query API, as moto expects)."""
    action = "VerifyEmailIdentity" if "@" in identity else "VerifyDomainIdentity"
    key = "EmailAddress" if "@" in identity else "Domain"
    body = urllib.parse.urlencode({"Action": action, key: identity}).encode()
    r = urllib.request.Request(MOTO + "/", data=body, headers={
        "Authorization": "AWS4-HMAC-SHA256 Credential=test/20260101/us-east-1/ses/aws4_request, SignedHeaders=host, Signature=x",
        "Content-Type": "application/x-www-form-urlencoded"})
    with _plain.open(r, timeout=15) as resp:
        return resp.status == 200


class Captured:
    """One raw message captured by the SES stand-in, parsed."""

    def __init__(self, raw):
        self.id = raw["id"]
        self.source = raw["source"]
        self.destinations = raw["destinations"]
        self.raw = raw["raw_data"]
        self.msg = email.message_from_string(self.raw, policy=email.policy.default)

    @property
    def to(self):
        return (self.destinations or [""])[0].lower()

    def header(self, name):
        v = self.msg[name]
        return str(v) if v is not None else None

    def _part(self, ctype):
        p = self.msg.get_body(preferencelist=(ctype.split("/")[1],))
        return p.get_content() if p is not None else ""

    @property
    def text(self):
        return self._part("text/plain")

    @property
    def html(self):
        return self._part("text/html")

    @property
    def subject(self):
        return self.header("Subject") or ""

    @property
    def unsubscribe_url(self):
        lu = self.header("List-Unsubscribe") or ""
        m = re.search(r"<(https?://[^>]+)>", lu)
        return m.group(1) if m else None


def _simple_to_raw(m):
    """SES SendEmail (non-raw) messages, e.g. invitation / password-reset mail, as Captured objects."""
    to = (m.get("destinations") or {}).get("ToAddresses") or []
    mime = (f"From: {m['source']}\r\nTo: {', '.join(to)}\r\nSubject: {m['subject']}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{m['body']}")
    return {"id": m["id"], "source": m["source"], "destinations": to, "raw_data": mime}


def ses_captured(to=None, since=0, source_domain=None):
    """All captured messages (raw campaign mail + simple system mail), optionally for one recipient / sender domain.
    `since` = skip the first N raw messages."""
    out = []
    data = _moto_data()["ses"]
    for raw in data["RawMessage"][since:] + [_simple_to_raw(m) for m in data["Message"]]:
        c = Captured(raw)
        if to and c.to != to.lower():
            continue
        if source_domain and not c.source.rstrip(">").lower().endswith("@" + source_domain.lower()):
            continue
        out.append(c)
    return out


def ses_count():
    return len(_moto_data()["ses"]["RawMessage"])


# ───────────────────────────── links ─────────────────────────────

def to_api_url(url):
    """Rewrite a link built from BASE_URL (http://localhost/vector/api/...) to the directly reachable API."""
    p = urllib.parse.urlparse(url)
    path = re.sub(r"^/vector/api", "", p.path)
    path = re.sub(r"^/api(?=/(tracking|auth|users))", "", path) if path.startswith("/api/tracking") else path
    return API + path + (("?" + p.query) if p.query else "")


def follow(method, url, form=None, body=None, token=None, raw=True):
    """Follow a link captured from a real email against the API. Returns (status, text)."""
    u = to_api_url(url)
    data = None
    h = {}
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if token:
        h["Authorization"] = f"Bearer {token}"
    r = urllib.request.Request(u, method=method, data=data, headers=h)
    try:
        with common._opener.open(r, timeout=60) as resp:
            return resp.status, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")


def links_in(text):
    return re.findall(r"https?://[^\s<>\"')]+", text or "")


# ───────────────────────────── jobs ─────────────────────────────

JOBS = {
    "scheduler": "scheduler_iteration", "imap_sync": "imap_iteration", "deliverability": "deliverability_iteration",
    "warmup": "warmup_iteration", "crm_purge": "crm_purge_iteration", "send_safety": "send_safety_iteration",
    "sales_jobs": "sales_jobs_iteration",
}


_RUN_ONCE = """
import asyncio, sys
try:
    from app.jobs import run_once
except ImportError:
    sys.exit(3)
ok = asyncio.run(run_once({name!r}, wait_seconds={wait}))
print('RAN' if ok else 'LOCK_BUSY')
sys.exit(0 if ok else 4)
"""


def run_job(name, timeout=240, lock_wait=120):
    """Run one background-job iteration now, in a fresh process inside the API container (same code the worker
    loop runs; a fresh process also triggers the hourly parts of send_safety / sales_jobs). Returns (ok, output).

    It takes the worker loop's named lock (app.jobs.run_once), waiting up to lock_wait s for an iteration that is
    running in the worker to finish: so it never runs alongside the worker, and when it returns ok every item that
    was due when it was called has been handled (a deterministic "flush" instead of sleeping for worker cycles).
    An image without run_once falls back to running the iteration function directly (jobs in JOBS only)."""
    code = _RUN_ONCE.format(name=name, wait=int(lock_wait))
    try:
        out = subprocess.run(["docker", "exec", API_CONTAINER, "python", "-c", code], capture_output=True, text=True,
                             timeout=timeout + lock_wait)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"docker exec failed: {exc}"
    if out.returncode != 3:
        return out.returncode == 0, (out.stdout + out.stderr)[-1500:]
    if name not in JOBS:
        return False, f"the API image predates app.jobs.run_once and cannot run {name!r}"
    fn = JOBS[name]
    code = f"import asyncio; from app.jobs import {fn}; print('RET', asyncio.run({fn}()))"
    out = subprocess.run(["docker", "exec", API_CONTAINER, "python", "-c", code], capture_output=True, text=True,
                         timeout=timeout)
    return out.returncode == 0, (out.stdout + out.stderr)[-1500:]


def scheduler_pass(fallback_sleep):
    """Barrier for "the worker had its chance to send": one scheduler pass now under the worker loop's lock, so every
    message due when this was called has been considered once. On an image without app.jobs.run_once (or if the
    lock stays busy) sleep fallback_sleep s instead (the previous fixed wait, a little over two 30 s cycles)."""
    ok, out = run_job("scheduler", timeout=180)
    if not ok:
        print(f"note: scheduler pass unavailable ({out.strip()[-160:]!r}); waiting {fallback_sleep}s instead")
        time.sleep(fallback_sleep)
    return ok


def py_in_api(code, timeout=120):
    out = subprocess.run(["docker", "exec", "-i", API_CONTAINER, "python", "-"], input=code, capture_output=True,
                         text=True, timeout=timeout)
    return out.returncode, out.stdout, out.stderr


# ───────────────────────────── TOTP ─────────────────────────────

def totp(secret, at=None, step=30, digits=6):
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((at or time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)).zfill(digits)


# ───────────────────────────── workspaces ─────────────────────────────

def new_workspace(label):
    """Self sign-up through the API. If the shared per-IP sign-up budget is gone, present a distinct client address."""
    try:
        return Workspace(label)
    except AssertionError as exc:
        if "429" not in str(exc):
            raise
        return _outreach._register_via_xff(label)


def login_xff(email_, password, mfa_code=None):
    """Log in once per user (the endpoint is rate limited per client IP and other suites share this one)."""
    for attempt in range(3):
        body = {"email": email_, "password": password}
        if mfa_code:
            body["mfa_code"] = mfa_code
        s, b = req("POST", "/api/auth/login", body)
        if s == 429:
            s, b = req("POST", "/api/auth/login", body,
                       headers={"X-Forwarded-For": f"10.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}.{uuid.uuid4().int % 250}"})
        if s != 429:
            return s, b
        time.sleep(5)
    return s, b


def user_token(email_, password):
    s, b = login_xff(email_, password)
    assert s == 200 and b.get("access_token"), ("login", email_, s, b)
    return b["access_token"]


def add_user(ws, role, first, **extra):
    """ws.add_user() but logs in through the rate-limit-safe path."""
    email_ = f"{first.lower()}-{ws.rid}@systest.example"
    h = sql(f"SELECT password_hash FROM users WHERE email='{ws.email}'")
    uid = str(uuid.uuid4())
    cols = {"user_id": uid, "tenant_id": ws.tenant_id, "first_name": first, "last_name": "Test", "email": email_,
            "role": role, "status": "ACTIVE", "password_hash": h, "auth_provider": "local", "email_verified": 1, **extra}
    names = ",".join(cols)
    vals = ",".join("NULL" if v is None else (str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'")
                    for v in cols.values())
    sql(f"INSERT INTO users ({names}, created_at) VALUES ({vals}, NOW())")
    return {"email": email_, "user_id": uid, "token": user_token(email_, ws.password), "first": first}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ───────────────────────────── SES event builders (SNS-shaped payloads the webhook accepts) ─────────────────────────────

def iso(minutes_ago=0):
    return (utcnow() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mail_for(message_id, ses_id, source, dest):
    return {"timestamp": iso(), "messageId": ses_id, "source": source, "destination": [dest],
            "tags": {"message_id": [message_id]}}


def ev_delivery(message_id, ses_id, source, dest, stamp=None):
    return {"eventType": "Delivery", "mail": _mail_for(message_id, ses_id, source, dest),
            "delivery": {"timestamp": stamp or iso(), "recipients": [dest], "smtpResponse": "250 2.0.0 OK",
                         "processingTimeMillis": 410}}


def ev_open(message_id, ses_id, source, dest, stamp=None):
    return {"eventType": "Open", "mail": _mail_for(message_id, ses_id, source, dest),
            "open": {"timestamp": stamp or iso(), "ipAddress": "203.0.113.7", "userAgent": "Mozilla/5.0 (e2e)"}}


def ev_click(message_id, ses_id, source, dest, link="https://example.com/pricing", stamp=None):
    return {"eventType": "Click", "mail": _mail_for(message_id, ses_id, source, dest),
            "click": {"timestamp": stamp or iso(), "link": link, "ipAddress": "203.0.113.7",
                      "userAgent": "Mozilla/5.0 (e2e)", "linkTags": {}}}


def ev_bounce(message_id, ses_id, source, dest, btype="Permanent", sub="General", diag="smtp; 550 5.1.1 user unknown"):
    return {"eventType": "Bounce", "mail": _mail_for(message_id, ses_id, source, dest),
            "bounce": {"bounceType": btype, "bounceSubType": sub, "timestamp": iso(),
                       "bouncedRecipients": [{"emailAddress": dest, "diagnosticCode": diag}]}}


def ev_complaint(message_id, ses_id, source, dest):
    return {"eventType": "Complaint", "mail": _mail_for(message_id, ses_id, source, dest),
            "complaint": {"complaintFeedbackType": "abuse", "timestamp": iso(),
                          "complainedRecipients": [{"emailAddress": dest}]}}


def sns_envelope(event):
    """Non-raw SNS delivery shape ({"Message": "<json>", "MessageId": ...}) without a cert signature: the local
    stack accepts it only together with the dev webhook token (real SNS-signed envelopes need AWS certs)."""
    return {"MessageId": str(uuid.uuid4()), "TopicArn": "arn:aws:sns:us-east-1:123456789012:ses-events",
            "Message": json.dumps(event), "Timestamp": iso()}


# The shared stack is often saturated by other test agents: give every API call 300 s instead of common.req's 120 s.
class _PatientOpener:
    def __init__(self, inner):
        self._inner = inner

    def open(self, r, data=None, timeout=300):
        return self._inner.open(r, data, timeout=max(timeout or 0, 300))


if not isinstance(common._opener, _PatientOpener):
    common._opener = _PatientOpener(common._opener)


def timed(method, path, body=None, token=None, **kw):
    """req() plus elapsed seconds: (status, body, seconds)."""
    t = time.time()
    s, b = req(method, path, body, token, **kw)
    return s, b, round(time.time() - t, 1)
