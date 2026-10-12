"""Shared helpers for the BRD system/integration suites (run against a live stack).

Each suite creates its own workspace (tenant) through self sign-up, so suites never touch
each other's data. Base URLs come from env: API_URL (default http://localhost:8191) and
WEB_URL (default http://localhost:8190/vector); DB checks use `docker exec vector-mysql-1`.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = os.environ.get("API_URL", "http://localhost:8191")
WEB = os.environ.get("WEB_URL", "http://localhost:8190/vector")
MYSQL_CONTAINER = os.environ.get("MYSQL_CONTAINER", "vector-mysql-1")
DB = os.environ.get("MYSQL_DATABASE", "outreach_ai")

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_raw_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def run_id() -> str:
    return uuid.uuid4().hex[:6]


def req(method, path, body=None, token=None, raw=False, files=None, form=None, headers=None, follow=True):
    """HTTP call to the API. Returns (status, parsed JSON | text | bytes)."""
    h = {"Authorization": f"Bearer {token}"} if token else {}
    h.update(headers or {})
    data = None
    if files is not None:
        boundary = uuid.uuid4().hex
        parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                 for k, v in (form or {}).items()]
        for k, (fname, content) in files.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                         f'Content-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
        data = b"".join(parts) + f"--{boundary}--\r\n".encode()
        h["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(API + path, method=method, data=data, headers=h)
    opener = _opener if follow else _raw_opener
    try:
        with opener.open(r, timeout=120) as resp:
            content = resp.read()
            return resp.status, _decode(content, raw)
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read(), raw)


def _decode(content, raw):
    if raw:
        return content
    try:
        return json.loads(content or b"null")
    except Exception:
        return content.decode(errors="ignore")


def sql(query: str) -> str:
    """Run SQL in the stack's MySQL (read checks / test setup inside your own tenant only)."""
    out = subprocess.run(["docker", "exec", MYSQL_CONTAINER, "mysql", "-uvector", "-pvector", DB, "-N", "-e", query],
                         capture_output=True, text=True)
    return out.stdout.strip()


def login(email: str, password: str) -> str:
    s, b = req("POST", "/api/auth/login", {"email": email, "password": password})
    assert s == 200 and isinstance(b, dict) and b.get("access_token"), (email, s, b)
    return b["access_token"]


class Workspace:
    """A fresh tenant with its SUPER_ADMIN, created via /api/auth/register."""

    def __init__(self, label: str):
        self.rid = run_id()
        self.email = f"owner-{label}-{self.rid}@systest.example"
        self.password = f"Systest-{self.rid}-pw!"
        s, b = req("POST", "/api/auth/register", {"first_name": "Sys", "last_name": label.title(),
                                                  "email": self.email, "password": self.password,
                                                  "tenant_name": f"Systest {label} {self.rid}"})
        assert s == 201, ("register failed", s, b)
        self.token = b["access_token"]
        self.tenant_id = sql(f"SELECT tenant_id FROM users WHERE email='{self.email}'")
        self.user_id = sql(f"SELECT user_id FROM users WHERE email='{self.email}'")

    def add_user(self, role: str, first: str, **extra) -> dict:
        """Create a user in this tenant directly (invites need email); same password as the owner.
        extra: other users columns, e.g. manager_id=..., sales_level=2. Returns {email, token, user_id}."""
        email = f"{first.lower()}-{self.rid}@systest.example"
        h = sql(f"SELECT password_hash FROM users WHERE email='{self.email}'")
        uid = str(uuid.uuid4())
        cols = {"user_id": uid, "tenant_id": self.tenant_id, "first_name": first, "last_name": "Test",
                "email": email, "role": role, "status": "ACTIVE", "password_hash": h,
                "auth_provider": "local", "email_verified": 1, **extra}
        names = ",".join(cols)
        vals = ",".join("NULL" if v is None else (str(v) if isinstance(v, int) else "'" + str(v).replace("'", "''") + "'")
                        for v in cols.values())
        sql(f"INSERT INTO users ({names}, created_at) VALUES ({vals}, NOW())")
        return {"email": email, "user_id": uid, "token": login(email, self.password)}

    def set_sender_identity(self):
        return req("PUT", "/api/company-profiles/sender-identity",
                   {"company_name": f"Systest Co {self.rid}", "postal_address": "1 Test Street, Austin, TX 78701, USA"},
                   self.token)


class Results:
    """Collects test-case outcomes and writes results.json + results.md next to the suite."""

    def __init__(self, suite: str, out_dir: str):
        self.suite, self.out_dir, self.rows, self.started = suite, out_dir, [], time.time()

    def record(self, case_id: str, title: str, passed, detail: str = "", category: str = None):
        """passed: True | False | None (blocked/not implemented). category for failures:
        'defect' (product bug), 'not-implemented' (BRD requirement missing), 'test-issue'."""
        status = "PASS" if passed is True else ("BLOCKED" if passed is None else "FAIL")
        self.rows.append({"id": case_id, "title": title, "status": status,
                          "category": category or ("" if passed else "defect"), "detail": str(detail)[:500]})
        print(f"{status:7} {case_id} {title}" + ("" if passed is True else f"  -> {str(detail)[:200]}"))

    def check(self, case_id, title, cond, detail="", category=None):
        self.record(case_id, title, bool(cond), "" if cond else detail, category)
        return bool(cond)

    def write(self):
        os.makedirs(self.out_dir, exist_ok=True)
        counts = {k: sum(1 for r in self.rows if r["status"] == k) for k in ("PASS", "FAIL", "BLOCKED")}
        with open(os.path.join(self.out_dir, "results.json"), "w") as f:
            json.dump({"suite": self.suite, "counts": counts, "seconds": round(time.time() - self.started, 1),
                       "cases": self.rows}, f, indent=2)
        lines = [f"# {self.suite} results", "",
                 f"PASS {counts['PASS']} · FAIL {counts['FAIL']} · BLOCKED {counts['BLOCKED']}", "",
                 "| ID | Test case | Status | Category | Detail |", "|---|---|---|---|---|"]
        for r in self.rows:
            d = r["detail"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['id']} | {r['title']} | {r['status']} | {r['category']} | {d} |")
        with open(os.path.join(self.out_dir, "results.md"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return counts
