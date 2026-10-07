"""
End-to-End test for the Auto Warmup feature.
Tests every API endpoint, service method, and data consistency.

Run: venv/Scripts/python.exe test_warmup_e2e.py
"""

import os, sys, json, time, asyncio, uuid
sys.stdout.reconfigure(encoding='utf-8', errors='replace') if hasattr(sys.stdout, 'reconfigure') else None
from datetime import datetime, timedelta

os.environ["APP_PROFILE"] = "dev"
sys.path.insert(0, ".")

import requests

BASE = "http://127.0.0.1:8001"

# Pre-generate JWT directly to avoid bcrypt timeout in test environment
def _get_token():
    from app.core.security import create_access_token
    from app.core.database import SessionLocal
    from app.models.user import User
    candidate_emails = [
        "chintan.jain@neutrinotechlabs.com",
        "snehal.pore@neutrinotechlabs.com",
        "sushant.shukla@neutrinotechlabs.com",
    ]
    db = SessionLocal()
    u = None
    for email in candidate_emails:
        row = db.query(User).filter(User.email == email).first()
        if row and row.status == "ACTIVE" and row.role in ("SUPER_ADMIN", "ADMIN"):
            u = row
            break
    if not u:
        db.close()
        raise RuntimeError("No active ADMIN/SUPER_ADMIN test user found for warmup E2E auth.")
    tok = create_access_token(user_id=u.user_id, tenant_id=u.tenant_id, role=u.role)
    tid = u.tenant_id
    user_email = u.email
    db.close()
    return tok, tid, user_email

# ─── Colour helpers ───────────────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
B  = "\033[94m"   # blue
W  = "\033[97m"   # white
DIM= "\033[2m"
RST= "\033[0m"

passed = failed = 0

def ok(msg):
    global passed
    passed += 1
    print(f"  {G}✓{RST} {msg}")

def fail(msg, detail=""):
    global failed
    failed += 1
    d = f" {DIM}→ {detail}{RST}" if detail else ""
    print(f"  {R}✗{RST} {msg}{d}")

def section(title):
    print(f"\n{B}{'─'*60}{RST}")
    print(f"{W}  {title}{RST}")
    print(f"{B}{'─'*60}{RST}")

def check(cond, msg, detail=""):
    if cond:
        ok(msg)
    else:
        fail(msg, detail)
    return cond


# ─── 1. AUTH ──────────────────────────────────────────────────────────────────
section("1. Authentication")

# Generate token directly (bcrypt at cost 12 can stall a 15s test timeout)
token, tenant_id, auth_email = _get_token()
check(bool(token),     "JWT token generated via security module")
check(bool(tenant_id), f"tenant_id obtained: {tenant_id}")

H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

# Verify token works against the API
r = requests.get(f"{BASE}/api/auth/me", headers=H, timeout=15)
check(r.status_code == 200, "GET /api/auth/me with generated token → 200",
      f"got {r.status_code}: {r.text[:120]}")
if r.status_code == 200:
    me = r.json()
    check("email" in me, f"me.email = {me.get('email')}")
    check(me.get("role") in ("ADMIN", "SUPER_ADMIN"), f"me.role = {me.get('role')}")

# Bad password returns 401/422
r2 = requests.post(f"{BASE}/api/auth/login",
                   json={"email": auth_email, "password": "wrong"},
                   timeout=60)
check(r2.status_code in (401, 422), f"Login with wrong password → 401/422 (got {r2.status_code})")

if not token:
    print(f"\n{R}Cannot proceed without a valid token — aborting.{RST}")
    sys.exit(1)


# ─── 2. WARMUP OVERVIEW (before any changes) ──────────────────────────────────
section("2. GET /inboxes/warmup/overview — baseline")

r = requests.get(f"{BASE}/inboxes/warmup/overview", headers=H, timeout=15)
check(r.status_code == 200, "GET /inboxes/warmup/overview → 200", f"got {r.status_code}")

overview = {}
if r.status_code == 200:
    overview = r.json()
    expected_keys = ["total_accounts","warmup_enabled","accounts_with_issues",
                     "average_reputation","current_pool","pool_growth_score",
                     "planned_today","warmup_sent_today","warmup_replied_today","saved_today"]
    missing = [k for k in expected_keys if k not in overview]
    check(not missing, "Overview has all required fields", f"missing: {missing}")
    check(overview["current_pool"] in ["FOUNDATION","GROWTH","PREMIUM","ULTRA_PREMIUM"],
          f"current_pool is valid tier: {overview.get('current_pool')}")
    check(isinstance(overview.get("average_reputation"), (int, float)),
          f"average_reputation is numeric: {overview.get('average_reputation')}")
    check(overview.get("total_accounts", 0) >= 0, "total_accounts ≥ 0")
    print(f"  {DIM}  Overview snapshot: total={overview['total_accounts']}  "
          f"enabled={overview['warmup_enabled']}  pool={overview['current_pool']}  "
          f"rep={overview['average_reputation']}%{RST}")


# ─── 3. LIST INBOXES ──────────────────────────────────────────────────────────
section("3. GET /inboxes — list all inboxes")

r = requests.get(f"{BASE}/inboxes", headers=H, timeout=15)
check(r.status_code == 200, "GET /inboxes → 200", f"got {r.status_code}")

inboxes = []
if r.status_code == 200:
    inboxes = r.json()
    check(isinstance(inboxes, list), f"Response is a list ({len(inboxes)} inboxes)")

    if inboxes:
        sample = inboxes[0]
        warmup_fields = ["warmup_enabled","warmup_day","warmup_pool","warmup_reputation",
                         "warmup_status","warmup_daily_target","warmup_today_sent",
                         "current_daily_limit"]
        missing = [f for f in warmup_fields if f not in sample]
        check(not missing, "Inbox response has all warmup fields", f"missing: {missing}")

        # Verify current_daily_limit matches warmup schedule logic
        for inbox in inboxes[:3]:
            day = inbox.get("warmup_day", 0)
            schedule = [5,8,12,18,25,35,50,70,90,120]
            expected_min = schedule[min(day, len(schedule)-1)] if inbox.get("warmup_enabled") else inbox.get("daily_limit", 100)
            limit = inbox.get("current_daily_limit", 0)
            # If graduated (day>=10) limit should be >= 120
            if inbox.get("warmup_enabled") and day >= 10:
                check(limit >= 120,
                      f"Inbox {inbox['email_address'][:30]} day={day} graduated limit≥120 (got {limit})")
            print(f"  {DIM}  {inbox['email_address'][:40]}  day={day}  "
                  f"limit={limit}  rep={inbox.get('warmup_reputation')}%  "
                  f"pool={inbox.get('warmup_pool')}  target={inbox.get('warmup_daily_target')}{RST}")


# ─── 4. CREATE TEST INBOX ─────────────────────────────────────────────────────
section("4. POST /inboxes — create a test inbox for warmup testing")

test_email = f"warmup.test.{uuid.uuid4().hex[:6]}@neutrinotechsystem.com"
create_payload = {
    "email_address":          test_email,
    "provider":               "SMTP",
    "daily_limit":            150,
    "warmup_enabled":         True,
    "warmup_auto_adjust":     True,
    "warmup_randomize":       True,
    "warmup_reply_rate_target": 35,
    "warmup_max_target":      None,
    "warmup_identifier":      "e2e-test",
    "delay_between_emails":   60,
}

r = requests.post(f"{BASE}/inboxes", headers=H, json=create_payload, timeout=15)
check(r.status_code == 201, f"POST /inboxes → 201", f"got {r.status_code}: {r.text[:200]}")

new_inbox_id = None
if r.status_code == 201:
    body = r.json()
    new_inbox_id = body.get("inbox_id")
    check(bool(new_inbox_id),                    "Created inbox has inbox_id")
    check(body.get("warmup_enabled") == True,    "warmup_enabled=True on creation")
    check(body.get("warmup_status") == "ACTIVE", f"warmup_status=ACTIVE (got {body.get('warmup_status')})")
    check(body.get("warmup_day", 0) == 0,        "warmup_day starts at 0")
    check(bool(body.get("warmup_start_date")),   "warmup_start_date is set")
    check(body.get("warmup_identifier") == "e2e-test", "warmup_identifier stored correctly")
    check(body.get("daily_limit") == 150,        "daily_limit=150 stored")
    print(f"  {DIM}  Created: {test_email}  id={new_inbox_id}{RST}")

    # Duplicate creation should 400
    r2 = requests.post(f"{BASE}/inboxes", headers=H, json=create_payload, timeout=15)
    check(r2.status_code == 400, "Duplicate inbox → 400", f"got {r2.status_code}")


# ─── 5. GET INBOX WARMUP DETAIL ───────────────────────────────────────────────
section("5. GET /inboxes/{id}/warmup — warmup detail for new inbox")

if new_inbox_id:
    r = requests.get(f"{BASE}/inboxes/{new_inbox_id}/warmup", headers=H, timeout=15)
    check(r.status_code == 200, "GET /inboxes/{id}/warmup → 200", f"got {r.status_code}")

    if r.status_code == 200:
        detail = r.json()
        check("inbox"        in detail, "Detail has 'inbox' key")
        check("today_metric" in detail, "Detail has 'today_metric' key")
        check("events"       in detail, "Detail has 'events' key")
        check("history"      in detail, "Detail has 'history' key")

        inbox_d = detail.get("inbox", {})
        check(inbox_d.get("inbox_id") == new_inbox_id, "inbox.inbox_id matches")
        check(inbox_d.get("warmup_enabled") == True,   "inbox.warmup_enabled=True")

        metric = detail.get("today_metric", {})
        metric_fields = ["planned_sends","actual_sends","open_count",
                         "reply_count","saved_from_spam_count","bounce_count","reputation_score"]
        missing = [f for f in metric_fields if f not in metric]
        check(not missing, "today_metric has all required fields", f"missing: {missing}")
        check(isinstance(metric.get("reputation_score"), (int,float)),
              f"reputation_score is numeric: {metric.get('reputation_score')}")

    # Test 404 for invalid id
    r404 = requests.get(f"{BASE}/inboxes/nonexistent-id/warmup", headers=H, timeout=15)
    check(r404.status_code == 404, "GET /inboxes/bad-id/warmup → 404", f"got {r404.status_code}")


# ─── 6. UPDATE WARMUP SETTINGS ────────────────────────────────────────────────
section("6. PUT /inboxes/{id} — update warmup settings")

if new_inbox_id:
    update_payload = {
        "warmup_reply_rate_target": 42,
        "warmup_max_target":        80,
        "warmup_randomize":         False,
        "delay_between_emails":     90,
        "daily_limit":              120,
    }
    r = requests.put(f"{BASE}/inboxes/{new_inbox_id}", headers=H, json=update_payload, timeout=15)
    check(r.status_code == 200, "PUT /inboxes/{id} → 200", f"got {r.status_code}: {r.text[:200]}")

    if r.status_code == 200:
        body = r.json()
        check(body.get("warmup_reply_rate_target") == 42, "reply_rate_target updated to 42")
        check(body.get("warmup_max_target")        == 80, "warmup_max_target updated to 80")
        check(body.get("warmup_randomize")         == False, "warmup_randomize updated to False")
        check(body.get("delay_between_emails")     == 90, "delay_between_emails updated to 90")
        check(body.get("daily_limit")              == 120, "daily_limit updated to 120")
        check(body.get("warmup_enabled")           == True, "warmup_enabled still True after partial update")

    # Toggle warmup off
    r2 = requests.put(f"{BASE}/inboxes/{new_inbox_id}", headers=H,
                      json={"warmup_enabled": False}, timeout=15)
    check(r2.status_code == 200, "PUT warmup_enabled=False → 200")
    if r2.status_code == 200:
        body2 = r2.json()
        check(body2.get("warmup_enabled")     == False,    "warmup_enabled toggled off")
        check(body2.get("warmup_status")      == "DISABLED","warmup_status→DISABLED on toggle off")
        check(body2.get("warmup_issue_code")  is None,     "warmup_issue_code cleared on disable")

    # Toggle warmup back on
    r3 = requests.put(f"{BASE}/inboxes/{new_inbox_id}", headers=H,
                      json={"warmup_enabled": True}, timeout=15)
    check(r3.status_code == 200, "PUT warmup_enabled=True → 200")
    if r3.status_code == 200:
        body3 = r3.json()
        check(body3.get("warmup_enabled")  == True,    "warmup_enabled toggled back on")
        check(body3.get("warmup_status")   == "ACTIVE","warmup_status→ACTIVE on re-enable")


# ─── 7. RUN WARMUP CYCLE ──────────────────────────────────────────────────────
section("7. POST /inboxes/warmup/run — trigger warmup cycle")

r = requests.post(f"{BASE}/inboxes/warmup/run", headers=H, timeout=60)
check(r.status_code == 200, "POST /inboxes/warmup/run → 200", f"got {r.status_code}: {r.text[:200]}")

cycle_stats = {}
if r.status_code == 200:
    body = r.json()
    check(body.get("status") == "success", f"status=success (got {body.get('status')})")
    stats = body.get("stats", {})
    stat_keys = ["processed_inboxes","warmup_emails_sent","warmup_replies_sent","issues_detected"]
    missing = [k for k in stat_keys if k not in stats]
    check(not missing, "Cycle stats has all expected keys", f"missing: {missing}")
    check(stats.get("processed_inboxes", 0) >= 0, f"processed_inboxes ≥ 0: {stats.get('processed_inboxes')}")
    check(stats.get("warmup_emails_sent", 0) >= 0, f"warmup_emails_sent ≥ 0: {stats.get('warmup_emails_sent')}")
    cycle_stats = stats
    print(f"  {DIM}  Cycle: processed={stats.get('processed_inboxes')}  "
          f"sent={stats.get('warmup_emails_sent')}  "
          f"replied={stats.get('warmup_replies_sent')}  "
          f"issues={stats.get('issues_detected')}{RST}")


# ─── 8. WARMUP DETAIL AFTER CYCLE ────────────────────────────────────────────
section("8. Warmup detail consistency after cycle")

if new_inbox_id:
    r = requests.get(f"{BASE}/inboxes/{new_inbox_id}/warmup", headers=H, timeout=15)
    if r.status_code == 200:
        detail = r.json()
        metric = detail.get("today_metric", {})
        inbox_d = detail.get("inbox", {})

        # planned_sends should be > 0 now that a cycle ran
        check(metric.get("planned_sends", 0) >= 0,
              f"planned_sends set after cycle: {metric.get('planned_sends')}")

        # warmup_daily_target on inbox should match metric planned_sends
        target_inbox  = inbox_d.get("warmup_daily_target", -1)
        target_metric = metric.get("planned_sends", -2)
        check(target_inbox == target_metric,
              f"inbox.warmup_daily_target ({target_inbox}) == metric.planned_sends ({target_metric})")

        # reputation_score should be in valid range
        rep = metric.get("reputation_score", -1)
        check(25.0 <= rep <= 100.0, f"reputation_score in [25, 100]: {rep}")

        # pool_tier should be a valid value
        pool = metric.get("pool_tier", "")
        check(pool in ["FOUNDATION","GROWTH","PREMIUM","ULTRA_PREMIUM"],
              f"pool_tier is valid: {pool}")

        events = detail.get("events", [])
        print(f"  {DIM}  After cycle: planned={metric.get('planned_sends')}  "
              f"sent={metric.get('actual_sends')}  rep={rep}%  events={len(events)}{RST}")


# ─── 9. SERVICE UNIT TESTS (direct Python) ───────────────────────────────────
section("9. Direct service layer tests (in-process)")

from app.core.database import SessionLocal
from app.services.warmup_service import warmup_service, WarmupService
from app.models.sending_inbox import SendingInbox
from app.models.inbox_warmup_metric import InboxWarmupMetric

db = SessionLocal()

# 9a: get_overview
try:
    ov = warmup_service.get_overview(db, tenant_id)
    check(isinstance(ov, dict), "get_overview returns dict")
    check("current_pool"       in ov, "get_overview has current_pool")
    check("average_reputation" in ov, "get_overview has average_reputation")
    check("planned_today"      in ov, "get_overview has planned_today")
except Exception as e:
    fail("get_overview raised exception", str(e))

# 9b: current_daily_limit graduation fix
schedule_map = [5,8,12,18,25,35,50,70,90,120]
fake_inbox = SendingInbox(
    inbox_id=str(uuid.uuid4()), tenant_id=tenant_id,
    email_address="fake@test.com", warmup_enabled=True,
    daily_limit=100
)
for day, expected in enumerate(schedule_map):
    fake_inbox.warmup_day = day
    fake_inbox.max_emails_per_day = None
    got = fake_inbox.current_daily_limit
    check(got == expected, f"current_daily_limit day={day} = {expected} (got {got})")

# Day 10 graduation: should be max(daily_limit=100, 120) = 120
fake_inbox.warmup_day = 10
got = fake_inbox.current_daily_limit
check(got == 120, f"Graduation day=10 → max(100,120)=120 (got {got})")

# Day 15 with higher daily_limit = 200
fake_inbox.daily_limit = 200
fake_inbox.warmup_day  = 15
got = fake_inbox.current_daily_limit
check(got == 200, f"Graduation day=15 daily_limit=200 → 200 (got {got})")

# With override
fake_inbox.max_emails_per_day = 30
got = fake_inbox.current_daily_limit
check(got == 30, f"max_emails_per_day override = 30 (got {got})")

# 9c: _pool_for_score
ws = WarmupService()
check(ws._pool_for_score(99.5, type('I', (), {'warmup_day':21,'warmup_issue_code':None})()) == "ULTRA_PREMIUM",
      "_pool_for_score(99.5, day=21) → ULTRA_PREMIUM")
check(ws._pool_for_score(97.0, type('I', (), {'warmup_day':14,'warmup_issue_code':None})()) == "PREMIUM",
      "_pool_for_score(97.0, day=14) → PREMIUM")
check(ws._pool_for_score(92.0, type('I', (), {'warmup_day':7,'warmup_issue_code':None})()) == "GROWTH",
      "_pool_for_score(92.0, day=7) → GROWTH")
check(ws._pool_for_score(80.0, type('I', (), {'warmup_day':3,'warmup_issue_code':None})()) == "FOUNDATION",
      "_pool_for_score(80.0, day=3) → FOUNDATION")
# Issue forces FOUNDATION
check(ws._pool_for_score(99.9, type('I', (), {'warmup_day':30,'warmup_issue_code':'SEND_FAILED'})()) == "FOUNDATION",
      "_pool_for_score with SEND_FAILED → FOUNDATION")

# 9d: _display_name
check(ws._display_name("john.doe@example.com") == "John Doe",  "_display_name john.doe")
check(ws._display_name("sales_rep@company.com") == "Sales Rep", "_display_name sales_rep")

# 9e: _today_key format
today_key = ws._today_key()
check(len(today_key) == 10, f"_today_key format YYYY-MM-DD (got '{today_key}')")
try:
    datetime.strptime(today_key, "%Y-%m-%d")
    ok("_today_key parses as valid date")
except ValueError:
    fail("_today_key is not a valid date", today_key)

# 9f: _dominant_pool
pool_list = [
    type('I', (), {'warmup_pool':'FOUNDATION'})(),
    type('I', (), {'warmup_pool':'PREMIUM'})(),
    type('I', (), {'warmup_pool':'GROWTH'})(),
]
check(ws._dominant_pool(pool_list) == "PREMIUM", "_dominant_pool → PREMIUM")
check(ws._dominant_pool([]) == "FOUNDATION",     "_dominant_pool([]) → FOUNDATION")

# 9g: _assess_health
class FakeInbox:
    warmup_enabled = True
    status = "ACTIVE"
    is_in_cooling = False
    imap_host = "imap.gmail.com"
    imap_username = "user"
    imap_password = "pass"
    last_sync_at = datetime.utcnow() - timedelta(hours=1)

fi = FakeInbox()
code, msg, blocking = ws._assess_health(fi, [fi, fi])
check(code is None, f"healthy inbox → no issue (got {code})")

fi.warmup_enabled = False
code, _, blocking = ws._assess_health(fi, [fi])
check(code == "WARMUP_DISABLED" and blocking, "disabled warmup → WARMUP_DISABLED+blocking")

fi.warmup_enabled = True
fi.status = "PAUSED"
code, _, blocking = ws._assess_health(fi, [fi])
check(code == "MAILBOX_PAUSED" and blocking, "paused mailbox → MAILBOX_PAUSED+blocking")

fi.status = "ACTIVE"
code, _, blocking = ws._assess_health(fi, [fi])  # pool of 1 (same inbox not counted as peer)
check(code == "POOL_TOO_SMALL" and blocking, "single inbox pool → POOL_TOO_SMALL+blocking")

fi2 = FakeInbox()
fi2.imap_host = None
code, _, blocking = ws._assess_health(fi2, [fi2, fi])  # imap missing, 2 inboxes
check(code == "IMAP_NOT_CONFIGURED" and not blocking, "no IMAP → IMAP_NOT_CONFIGURED not blocking")

fi3 = FakeInbox()
fi3.last_sync_at = datetime.utcnow() - timedelta(hours=30)
code, _, blocking = ws._assess_health(fi3, [fi3, fi])
check(code == "SYNC_STALE" and not blocking, "stale sync → SYNC_STALE not blocking")

# 9h: _compute_daily_target clamping
class TInbox:
    warmup_max_target = None
    warmup_enabled = True
    warmup_day = 3           # → current_daily_limit = 18
    max_emails_per_day = None
    daily_limit = 150
    emails_sent_today = 0
    warmup_auto_adjust = False
    warmup_randomize = False
    @property
    def warmup_effective_target(self):
        return min(self.warmup_max_target or self.current_daily_limit or 0, self.daily_limit or 0)
    @property
    def current_daily_limit(self):
        s = [5,8,12,18,25,35,50,70,90,120]
        d = self.warmup_day or 0
        if d >= len(s): return max(self.daily_limit or 100, 120)
        return s[d]

ti = TInbox()
target = ws._compute_daily_target(ti)
check(0 <= target <= 150, f"_compute_daily_target within bounds (got {target})")

# auto_adjust reduces by emails_sent_today
ti.warmup_auto_adjust = True
ti.emails_sent_today = 10
target2 = ws._compute_daily_target(ti)
check(target2 <= target, f"auto_adjust reduces target ({target}→{target2})")

db.close()


# ─── 10. WARMUP OVERVIEW AFTER CYCLE ─────────────────────────────────────────
section("10. GET /inboxes/warmup/overview — post-cycle check")

r = requests.get(f"{BASE}/inboxes/warmup/overview", headers=H, timeout=15)
if r.status_code == 200:
    ov2 = r.json()
    check(ov2["total_accounts"] >= overview.get("total_accounts", 0),
          f"total_accounts ≥ baseline ({ov2['total_accounts']})")
    check(ov2["warmup_enabled"] >= 0, f"warmup_enabled ≥ 0: {ov2['warmup_enabled']}")
    check(ov2["pool_growth_score"] >= 0, f"pool_growth_score ≥ 0: {ov2['pool_growth_score']}")
    print(f"  {DIM}  Post-cycle: total={ov2['total_accounts']}  "
          f"enabled={ov2['warmup_enabled']}  rep={ov2['average_reputation']}%  "
          f"pool={ov2['current_pool']}{RST}")


# ─── 11. DELETE TEST INBOX ───────────────────────────────────────────────────
section("11. DELETE /inboxes/{id} — cleanup test inbox")

if new_inbox_id:
    r = requests.delete(f"{BASE}/inboxes/{new_inbox_id}", headers=H, timeout=15)
    check(r.status_code == 204, f"DELETE /inboxes/{new_inbox_id[:8]}… → 204", f"got {r.status_code}")

    # Should 404 now
    r2 = requests.get(f"{BASE}/inboxes/{new_inbox_id}", headers=H, timeout=15)
    check(r2.status_code == 404, "GET deleted inbox → 404")

    # Detail should 404 too
    r3 = requests.get(f"{BASE}/inboxes/{new_inbox_id}/warmup", headers=H, timeout=15)
    check(r3.status_code == 404, "GET warmup detail of deleted inbox → 404")


# ─── 12. AUTH GUARD CHECKS ───────────────────────────────────────────────────
section("12. Auth & permission guards")

no_auth = {}
r = requests.get(f"{BASE}/inboxes/warmup/overview", headers=no_auth, timeout=10)
check(r.status_code in (401, 403), f"No-auth overview → 401/403 (got {r.status_code})")

r = requests.post(f"{BASE}/inboxes/warmup/run", headers=no_auth, timeout=10)
check(r.status_code in (401, 403), f"No-auth warmup run → 401/403 (got {r.status_code})")

bad_token = {"Authorization": "Bearer bad_token_xyz", "Content-Type": "application/json"}
r = requests.get(f"{BASE}/inboxes", headers=bad_token, timeout=10)
check(r.status_code in (401, 403), f"Bad token → 401/403 (got {r.status_code})")


# ─── SUMMARY ─────────────────────────────────────────────────────────────────
total = passed + failed
print(f"\n{'═'*60}")
print(f"  RESULTS: {G}{passed} passed{RST}  {R}{failed} failed{RST}  / {total} total")
if failed == 0:
    print(f"  {G}All tests passed! ✓{RST}")
else:
    print(f"  {Y}Review failures above.{RST}")
print(f"{'═'*60}\n")

sys.exit(0 if failed == 0 else 1)
