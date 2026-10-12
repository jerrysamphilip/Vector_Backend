"""Tenant/user fixtures for the sweep. Sign-up is limited to 10/hour per IP and sign-in to 60/5min per IP (shared by
every suite on this machine), so workspaces + users are cached in .state.json and reused while their tokens are valid
(30 min TTL); pass --fresh to the runner to force new tenants."""
import json
import os
import time
import uuid

from sweeplib import HERE, http, sql, login, run_id, Workspace

STATE = os.path.join(HERE, ".state.json")
ALL_PERMS = ["manage_campaigns", "manage_templates", "manage_inboxes", "manage_prospects", "view_analytics",
             "export_data", "manage_team"]


def q(s):
    return "'" + str(s).replace("\\", "\\\\").replace("'", "''") + "'"


def load():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save(st):
    json.dump(st, open(STATE, "w"), indent=1)


class SafeIds(dict):
    """Registry: a missing key yields a fresh random uuid (so a failed creation shows up as 404s, not a crash)."""

    def __missing__(self, key):
        return str(uuid.uuid4())


class Tenant:
    """One tenant: owner (SUPER_ADMIN) + role users. self.tok[ident] = bearer token."""

    def __init__(self, label):
        self.label = label
        self.rid = None
        self.tok = {}
        self.uid = {}
        self.email = {}
        self.password = None
        self.tenant_id = None
        self.register_response = None
        self.register_status = None
        self.ids = SafeIds()

    def register(self):
        rid = run_id()
        self.rid = rid
        self.password = f"Systest-{rid}-pw!"
        email = f"owner-{self.label}-{rid}@systest.example"
        body = {"first_name": "Sys", "last_name": self.label.title(), "email": email, "password": self.password,
                "tenant_name": f"Systest {self.label} {rid}"}
        s, b, h, _ = http("POST", "/api/auth/register", body)
        if s == 429:  # shared per-IP sign-up budget used up: same workaround the other suites use (distinct client hop)
            xff = f"10.251.{int(rid[:2], 16)}.{int(rid[2:4], 16)}"
            s, b, h, _ = http("POST", "/api/auth/register", body, headers={"X-Forwarded-For": xff})
        self.register_status, self.register_response = s, b
        assert s == 201, ("register failed", s, b)
        self.email["SUPER_ADMIN"] = email
        self.tok["SUPER_ADMIN"] = b["access_token"]
        self.tenant_id = sql(f"SELECT tenant_id FROM users WHERE email={q(email)}")
        self.uid["SUPER_ADMIN"] = sql(f"SELECT user_id FROM users WHERE email={q(email)}")

    def add_user(self, ident, role, first, **extra):
        ws = Workspace.__new__(Workspace)
        ws.rid, ws.email_, ws.password, ws.tenant_id = self.rid, self.email["SUPER_ADMIN"], self.password, self.tenant_id
        ws.email = self.email["SUPER_ADMIN"]
        u = ws.add_user(role, first, **extra)
        self.tok[ident], self.uid[ident], self.email[ident] = u["token"], u["user_id"], u["email"]
        return u

    def dump(self):
        return {"label": self.label, "rid": self.rid, "tok": self.tok, "uid": self.uid, "email": self.email,
                "password": self.password, "tenant_id": self.tenant_id, "ts": time.time()}

    @classmethod
    def load(cls, d):
        t = cls(d["label"])
        t.__dict__.update({k: d[k] for k in ("rid", "tok", "uid", "email", "password", "tenant_id")})
        return t


def build_tenant_a():
    """Tenant A identities: SUPER_ADMIN(owner) ADMIN MANAGER AGENT AGENT_NOPERM + one AGENT per single permission
    + hierarchy users (MGR2, AGENT_OTHER)."""
    t = Tenant("swa")
    t.register()
    # Hierarchy: MANAGER (level 2) <- AGENT (level 3); MGR2 (level 2) <- AGENT_OTHER (level 3): separate branches
    t.add_user("ADMIN", "ADMIN", "Swadmin")
    t.add_user("MANAGER", "MANAGER", "Swmanager", sales_level=2)
    t.add_user("AGENT", "AGENT", "Swagent", sales_level=3, manager_id=t.uid["MANAGER"])
    t.add_user("AGENT_NOPERM", "AGENT", "Swnoperm", custom_permissions="[]")
    t.add_user("MGR2", "MANAGER", "Swmgrtwo", sales_level=2)
    t.add_user("AGENT_OTHER", "AGENT", "Swother", sales_level=3, manager_id=t.uid["MGR2"])
    for p in ALL_PERMS:
        t.add_user(f"AGENT_ONLY_{p}", "AGENT", f"Swonly{p.replace('_', '')[:12]}", custom_permissions=json.dumps([p]))
    return t


def build_tenant_b():
    t = Tenant("swb")
    t.register()
    return t


def get_tenants(fresh=False):
    st = load()
    if not fresh and "A" in st and "B" in st and time.time() - st["A"]["ts"] < 25 * 60:
        A, B = Tenant.load(st["A"]), Tenant.load(st["B"])
        s, _, _, _ = http("GET", "/api/auth/me", token=A.tok["SUPER_ADMIN"])
        if s == 200 and sql(f"SELECT COUNT(*) FROM users WHERE tenant_id={q(A.tenant_id)}"):
            return A, B, False
    A, B = build_tenant_a(), build_tenant_b()
    save({"A": A.dump(), "B": B.dump()})
    return A, B, True
