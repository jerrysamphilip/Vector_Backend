"""Helpers for the reports/platform suite (kept here so system_tests/common.py stays untouched)."""
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common  # noqa: E402
from common import req, sql, Workspace  # noqa: E402,F401


def xff_headers(ip):
    return {"X-Forwarded-For": ip}


def fake_ip():
    """A TEST-NET address, unique per call (RFC 5737) - used only where noted in TEST_CASES.md."""
    n = uuid.uuid4().int
    return f"198.51.{n % 250 + 1}.{(n >> 8) % 250 + 1}"


def make_workspace(label):
    """common.Workspace, falling back to a register call from a distinct client address when the
    shared per-IP register limit (10/hour) is already used up by other suites on this host."""
    try:
        return common.Workspace(label)
    except AssertionError as e:
        if "429" not in str(e):
            raise
    ws = common.Workspace.__new__(common.Workspace)
    ws.rid = common.run_id()
    ws.email = f"owner-{label}-{ws.rid}@systest.example"
    ws.password = f"Systest-{ws.rid}-pw!"
    s, b = req("POST", "/api/auth/register", {"first_name": "Sys", "last_name": label.title(), "email": ws.email,
                                              "password": ws.password, "tenant_name": f"Systest {label} {ws.rid}"},
               headers=xff_headers(fake_ip()))
    assert s == 201, ("register failed", s, b)
    ws.token = b["access_token"]
    ws.tenant_id = sql(f"SELECT tenant_id FROM users WHERE email='{ws.email}'")
    ws.user_id = sql(f"SELECT user_id FROM users WHERE email='{ws.email}'")
    return ws


def add_user(ws, role, first, **extra):
    """ws.add_user, but if the shared per-IP login limit is hit, sign the new user in from a distinct
    client address instead (the user row is already created by then)."""
    try:
        return ws.add_user(role, first, **extra)
    except AssertionError:
        email = f"{first.lower()}-{ws.rid}@systest.example"
        uid = sql(f"SELECT user_id FROM users WHERE email='{email}'")
        assert uid, ("user insert failed", email)
        s, b = req("POST", "/api/auth/login", {"email": email, "password": ws.password}, headers=xff_headers(fake_ip()))
        assert s == 200, ("login failed", s, b)
        return {"email": email, "user_id": uid, "token": b["access_token"]}


# ── Independent fiscal arithmetic (FY April - March, named by the starting calendar year) ──

def fy_of(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def fq_of(d: date) -> int:
    # Apr-Jun 1, Jul-Sep 2, Oct-Dec 3, Jan-Mar 4
    return {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]


QUARTER_RANGES = {  # literal, per BRD "financial year runs April to March"
    1: ((4, 1), (6, 30), 0), 2: ((7, 1), (9, 30), 0), 3: ((10, 1), (12, 31), 0), 4: ((1, 1), (3, 31), 1),
}


def quarter_range(fy, q):
    (m0, d0), (m1, d1), off = QUARTER_RANGES[q]
    return date(fy + off, m0, d0), date(fy + off, m1, d1)


def feq(a, b, tol=0.006):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def diff_dict(actual: dict, expected: dict, prefix=""):
    """List of 'key: got X expected Y' for every expected key (floats compared to the cent)."""
    out = []
    for k, v in expected.items():
        a = actual.get(k) if isinstance(actual, dict) else None
        if isinstance(v, dict):
            out += diff_dict(a or {}, v, f"{prefix}{k}.")
        elif isinstance(v, float) or isinstance(a, float):
            if not feq(a, v):
                out.append(f"{prefix}{k}: got {a} expected {v}")
        elif a != v:
            out.append(f"{prefix}{k}: got {a!r} expected {v!r}")
    return out


MONEYISH = ("amount", "revenue", "pipeline", "weighted", "forecast", "target", "commit", "best_case", "gap",
            "attainment", "price", "subtotal", "discount_total", "line_total", "average_deal", "categories")
NOT_MONEY = ("count", "pipeline_count")


def leaked_money(data, path="", out=None, under_money=False):
    """Paths of numeric values that sit under an amount-like key (for BR-SF-12 checks)."""
    out = [] if out is None else out
    if isinstance(data, dict):
        for k, v in data.items():
            kl = str(k).lower()
            money_key = under_money or (any(m in kl for m in MONEYISH) and not kl.endswith(NOT_MONEY)) \
                or kl in ("won", "lost", "closed", "best_case")
            leaked_money(v, f"{path}.{k}", out, money_key)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            leaked_money(v, f"{path}[{i}]", out, under_money)
    elif under_money and isinstance(data, (int, float)) and not isinstance(data, bool):
        out.append(f"{path}={data}")
    return out
