"""Async HTTP load generator for the Vector API (chunk V2). Only httpx (already a dependency) is used.

    python scripts/load/run_load.py --base-url http://127.0.0.1:8199 --concurrency 25 --duration 60 --ramp 10
    python scripts/load/run_load.py --base-url http://127.0.0.1:8199 --base-url http://127.0.0.1:8198   # 2 replicas

Signs in once per seeded user and replica, the way the SPA does (X-Auth-Mode: cookie: httpOnly access
cookie + readable csrf_token cookie echoed in X-CSRF-Token on writes), then runs a weighted mix of reads and
writes from --concurrency virtual users for --duration seconds, starting them evenly over --ramp seconds.
Requests round-robin over the --base-url values. Writes JSON + a markdown table to --results
(default reports/load/run_<timestamp>.{json,md}; reports/ is git-ignored). Exit code 1 when a threshold
is breached, 2 when sign-in or discovery fails.

Rate limits (app/core/rate_limit.py) are in-memory per API process: GLOBAL_PER_IP allows 1200 requests per
minute per client IP and sign-in 60 attempts per 5 minutes per IP. With no proxy in front of the API the
right-most X-Forwarded-For hop is the client address, so each virtual user sends its own address
(--no-xff turns that off, and then everything shares the runner's one address and hits 429 above
~20 req/s per replica).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loadlib  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PRIVILEGED = ("SUPER_ADMIN", "ADMIN", "MANAGER")
RUN_NONCE = uuid.uuid4().hex[:8]

DEFAULT_MIX: Dict[str, float] = {
    "contacts_list": 25, "contacts_search": 12, "contact_detail": 15, "contact_create": 4, "contact_update": 5,
    "campaigns_list": 6, "campaign_detail": 6, "inbox_list": 8, "inbox_search": 4,
    "reports_dashboard": 8, "sales_dashboard": 4, "export_small": 3,
}

# Requirement-backed defaults (docs/TRACEABILITY.md §6 rows; docs/OPERATIONS.md alert rules). Override with --target.
DEFAULT_TARGETS: List[loadlib.Target] = [
    loadlib.Target("contacts_list", "p95", 1000, "§6 Performance: lists < 1 s at 100k (SP-162)"),
    loadlib.Target("contacts_search", "p95", 1000, "§6: contact search p95 < 1 s (CM-091..093)"),
    loadlib.Target("contact_detail", "p95", 1000, "§6: record views p95 < 1 s (CM-092/093)"),
    loadlib.Target("reports_dashboard", "p95", 3000, "§6: dashboards < 3 s (RP-130)"),
    loadlib.Target("sales_dashboard", "p95", 3000, "§6: dashboards < 3 s (RP-130)"),
    loadlib.Target("ALL", "p95", 2000, "OPERATIONS.md alert: API p95 > 2 s"),
    loadlib.Target("ALL", "error_rate", 0.0, "§6 Scale: 0 % errors at 20+ concurrent users (RP-131)"),
]


@dataclass
class Session:
    """One signed-in user on one replica: its own client (own cookie jar), like one browser."""
    email: str
    role: str
    base_url: str
    client: httpx.AsyncClient
    xff: str
    contacts: List[Dict[str, Any]] = field(default_factory=list)
    campaigns: List[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def headers(self, method: str, xff: str) -> Dict[str, str]:
        h = {"X-Auth-Mode": "cookie", "Accept": "application/json"}
        if xff:
            h["X-Forwarded-For"] = xff
        if method not in ("GET", "HEAD", "OPTIONS"):
            h["X-CSRF-Token"] = self.client.cookies.get("csrf_token") or ""
        return h


@dataclass
class Runner:
    args: argparse.Namespace
    sessions: List[Session] = field(default_factory=list)
    samples: List[loadlib.Sample] = field(default_factory=list)
    search_terms: List[str] = field(default_factory=list)
    started: float = 0.0
    stop_at: float = 0.0
    refreshes: int = 0


# ── HTTP plumbing ───────────────────────────────────────────────────────────

async def call(runner: Runner, s: Session, name: str, method: str, path: str, xff: str,
               expect: Sequence[int] = (200,), record: bool = True, headers_extra: Optional[Dict[str, str]] = None,
               **kw: Any) -> Optional[httpx.Response]:
    """One request, timed; on 401 refresh the session once and retry (both inside one sample)."""
    t0 = time.perf_counter()
    try:
        resp = await s.client.request(method, path, headers={**s.headers(method, xff), **(headers_extra or {})}, **kw)
        if resp.status_code == 401 and await refresh(runner, s, xff):
            resp = await s.client.request(method, path, headers={**s.headers(method, xff), **(headers_extra or {})},
                                          **kw)
        status = resp.status_code
    except httpx.HTTPError:
        resp, status = None, 0
    if record and time.perf_counter() < runner.stop_at:
        runner.samples.append(loadlib.Sample(name, status, (time.perf_counter() - t0) * 1000,
                                             status in expect, s.base_url))
    return resp


async def refresh(runner: Runner, s: Session, xff: str) -> bool:
    """Rotate the session (access cookies last ACCESS_TOKEN_EXPIRE_MINUTES). Serialised per session:
    replaying an already-rotated refresh token revokes the whole session (ADR 0007)."""
    async with s.lock:
        before = s.client.cookies.get("access_token")
        probe = await s.client.get("/api/auth/me", headers=s.headers("GET", xff))
        if probe.status_code == 200 or s.client.cookies.get("access_token") != before:
            return True
        r = await s.client.post("/api/auth/refresh", headers={**s.headers("POST", xff), "X-User-Activity": "0"})
        runner.refreshes += 1
        return r.status_code == 200


async def login(base_url: str, email: str, password: str, role: str, xff: str, timeout: float,
                pool: int) -> Session:
    client = httpx.AsyncClient(base_url=base_url, timeout=timeout,
                               limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool))
    s = Session(email, role, base_url, client, xff)
    r = await client.post("/api/auth/login", json={"email": email, "password": password},
                          headers=s.headers("POST", xff))
    if r.status_code != 200 or not client.cookies.get("csrf_token"):
        await client.aclose()
        raise RuntimeError(f"sign-in failed for {email} on {base_url}: {r.status_code} {r.text[:200]}")
    if r.json().get("mfa_required"):
        await client.aclose()
        raise RuntimeError(f"{email} has two-factor sign-in on; seeded users must not")
    return s


def items_of(payload: Any) -> List[Dict[str, Any]]:
    """The row list of a paged response, whatever the key is called."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "contacts", "campaigns", "results", "data", "conversations"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


async def discover(runner: Runner, s: Session) -> None:
    """Ids this user can actually see (visibility follows the sales hierarchy)."""
    r = await call(runner, s, "discover", "GET", "/contacts", s.xff, record=False,
                   params={"page_size": 200, "sort_by": "updated_at"})
    if r is not None and r.status_code == 200:
        s.contacts = [{"id": c.get("prospect_id") or c.get("id"), "last_name": c.get("last_name"),
                       "email": c.get("email")} for c in items_of(r.json())]
    r = await call(runner, s, "discover", "GET", "/campaigns", s.xff, record=False, params={"page_size": 100})
    if r is not None and r.status_code == 200:
        s.campaigns = [c.get("campaign_id") or c.get("id") for c in items_of(r.json())]
    for c in s.contacts[:40]:
        for term in (c.get("last_name"), (c.get("email") or "").split("@")[-1].split(".")[0]):
            if term and len(term) >= 3:
                runner.search_terms.append(term.lower())


# ── Scenarios ───────────────────────────────────────────────────────────────

ScenarioFn = Callable[[Runner, Session, random.Random, str], Awaitable[None]]
LIFECYCLE = ("SUBSCRIBER", "LEAD", "MQL", "SQL", "OPPORTUNITY", "CUSTOMER")
LEAD_STATUS = ("NEW", "OPEN", "IN_PROGRESS", "CONNECTED")
SORTS = ("created_at", "updated_at", "name", "company", "score", "lifecycle_stage")
REGIONS = ("NA", "EMEA", "APAC", "LATAM")


def list_params(rng: random.Random) -> Dict[str, Any]:
    params: Dict[str, Any] = {"page": rng.choice((1, 1, 1, 2, 3, 10, 50)), "page_size": rng.choice((25, 50)),
                              "sort_by": rng.choice(SORTS), "sort_order": rng.choice(("asc", "desc"))}
    roll = rng.random()
    if roll < 0.25:
        params["lifecycle_stage"] = rng.choice(LIFECYCLE)
    elif roll < 0.4:
        params["lead_status"] = rng.choice(LEAD_STATUS)
    elif roll < 0.5:
        params["filters"] = json.dumps({"op": "AND", "conditions": [
            {"field": "custom.region", "operator": "is", "value": rng.choice(REGIONS)}]})
    elif roll < 0.6:
        params["owner"] = "me"
    return params


async def contacts_list(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    await call(runner, s, "contacts_list", "GET", "/contacts", xff, params=list_params(rng))


async def contacts_search(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    term = rng.choice(runner.search_terms or ["smith"])
    await call(runner, s, "contacts_search", "GET", "/contacts", xff, params={"q": term, "page_size": 25})


async def contact_detail(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    if not s.contacts:
        return await contacts_list(runner, s, rng, xff)
    await call(runner, s, "contact_detail", "GET", f"/contacts/{rng.choice(s.contacts)['id']}", xff)


async def contact_create(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    # RUN_NONCE keeps emails unique across runs (the per-VU rng repeats by design; uq_tenant_email)
    body = {"email": f"load-{RUN_NONCE}-{uuid.UUID(int=rng.getrandbits(128)).hex[:16]}@created.vector-load.example",
            "first_name": "Load", "last_name": f"Created{rng.randint(1, 9999)}", "lifecycle_stage": "LEAD",
            "custom_fields": {"region": rng.choice(REGIONS)}, "tags": ["load-created"]}
    r = await call(runner, s, "contact_create", "POST", "/contacts", xff, expect=(200, 201), json=body)
    if r is not None and r.status_code in (200, 201):
        data = r.json()
        s.contacts.append({"id": data.get("prospect_id") or data.get("id"), "last_name": body["last_name"]})


async def contact_update(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    """Load the record, then PATCH it with If-Match: the SPA's edit flow (409 = someone else won, expected)."""
    if not s.contacts:
        return await contact_create(runner, s, rng, xff)
    cid = rng.choice(s.contacts)["id"]
    r = await call(runner, s, "contact_detail", "GET", f"/contacts/{cid}", xff)
    if r is None or r.status_code != 200:
        return None
    version = r.json().get("row_version")
    headers_extra = {"If-Match": str(version)} if version else {}
    await call(runner, s, "contact_update", "PATCH", f"/contacts/{cid}", xff, expect=(200, 409),
               json={"designation": rng.choice(("VP Sales", "Head of Growth", "CTO", "Buyer"))},
               headers_extra=headers_extra)


async def campaigns_list(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    await call(runner, s, "campaigns_list", "GET", "/campaigns", xff,
               params={"page": 1, "page_size": rng.choice((10, 20, 50))})


async def campaign_detail(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    if not s.campaigns:
        return await campaigns_list(runner, s, rng, xff)
    cid = rng.choice(s.campaigns)
    path = rng.choice((f"/campaigns/{cid}", f"/campaigns/{cid}/metrics", f"/campaigns/{cid}/sequences"))
    await call(runner, s, "campaign_detail", "GET", path, xff)


async def inbox_list(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    await call(runner, s, "inbox_list", "GET", "/conversations", xff,
               params={"page": rng.choice((1, 1, 2, 3)), "page_size": 20})


async def inbox_search(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    """The unified inbox filters by campaign (GET /conversations has no free-text search)."""
    if not s.campaigns:
        return await inbox_list(runner, s, rng, xff)
    await call(runner, s, "inbox_search", "GET", "/conversations", xff,
               params={"campaign_id": rng.choice(s.campaigns), "page_size": 20})


async def reports_dashboard(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    """The analytics dashboard (view_analytics permission: admins and managers)."""
    today = datetime.now(timezone.utc).date()
    days = rng.choice((7, 30, 90))
    await call(runner, s, "reports_dashboard", "GET", "/reports/dashboard-summary", xff,
               params={"start_date": (today - timedelta(days=days)).isoformat(), "end_date": today.isoformat()})


async def sales_dashboard(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    await call(runner, s, "sales_dashboard", "GET", "/sales-reports/dashboard", xff)


async def export_small(runner: Runner, s: Session, rng: random.Random, xff: str) -> None:
    """CSV export of a narrow search (admins and managers only, BR-CM-31)."""
    term = rng.choice(runner.search_terms or ["smith"])
    await call(runner, s, "export_small", "GET", "/contacts/export", xff,
               params={"format": "csv", "q": term, "lifecycle_stage": rng.choice(LIFECYCLE)})


SCENARIOS: Dict[str, Tuple[ScenarioFn, Tuple[str, ...]]] = {
    "contacts_list": (contacts_list, ()), "contacts_search": (contacts_search, ()),
    "contact_detail": (contact_detail, ()), "contact_create": (contact_create, ()),
    "contact_update": (contact_update, ()), "campaigns_list": (campaigns_list, ()),
    "campaign_detail": (campaign_detail, ()), "inbox_list": (inbox_list, ()), "inbox_search": (inbox_search, ()),
    "reports_dashboard": (reports_dashboard, PRIVILEGED),  # view_analytics: not agents
    "sales_dashboard": (sales_dashboard, ()),
    "export_small": (export_small, PRIVILEGED),
}


def session_for(runner: Runner, vu: int, n: int, roles: Tuple[str, ...]) -> Session:
    """Virtual user vu's session for its n-th request: users fixed per VU, replicas round-robin."""
    replicas = runner.args.base_url
    base = replicas[(vu + n) % len(replicas)]
    pool = [s for s in runner.sessions if s.base_url == base and (not roles or s.role in roles)]
    users = sorted({s.email for s in runner.sessions if s.base_url == base})
    mine = users[vu % len(users)]
    return next((s for s in pool if s.email == mine), pool[vu % len(pool)])


def vu_address(vu: int) -> str:
    return f"10.77.{vu // 250}.{vu % 250 + 1}"


async def virtual_user(runner: Runner, vu: int, mix: loadlib.WeightedMix) -> None:
    rng = random.Random(runner.args.seed * 1000 + vu)
    await asyncio.sleep(runner.args.ramp * vu / max(1, runner.args.concurrency))
    xff = "" if runner.args.no_xff else vu_address(vu)
    n = 0
    while time.perf_counter() < runner.stop_at:
        name = mix.pick(rng)
        fn, roles = SCENARIOS[name]
        await fn(runner, session_for(runner, vu, n, roles), rng, xff)
        n += 1
        if runner.args.think > 0:
            await asyncio.sleep(rng.uniform(0, 2 * runner.args.think))


# ── Setup, run, report ──────────────────────────────────────────────────────

def load_users(args: argparse.Namespace) -> Tuple[List[Dict[str, str]], str]:
    """(users, password) from --users / --password, else from the seed manifest."""
    if args.users:
        role = "SUPER_ADMIN" if args.users_privileged else "AGENT"
        users = [{"email": e.strip(), "role": role} for e in args.users.split(",") if e.strip()]
        return users, args.password or ""
    data = json.loads(Path(args.manifest).read_text())
    return [{"email": u["email"], "role": u["role"]} for u in data["users"]], args.password or data["password"]


async def sign_in_all(runner: Runner, users: List[Dict[str, str]], password: str) -> None:
    """Once per user and replica, sequentially: ~10 sign-ins per replica, far below 60 / 5 min per IP."""
    a = runner.args
    for base in a.base_url:
        for i, u in enumerate(users):
            s = await login(base, u["email"], password, u["role"], "" if a.no_xff else f"10.78.0.{i + 1}",
                            a.timeout, a.concurrency + 4)
            runner.sessions.append(s)


async def cross_replica_check(runner: Runner) -> Optional[Dict[str, Any]]:
    """Does a session cookie issued by replica 1 work on replica 2? Only when all replicas share
    JWT_SECRET_KEY (without it each process signs with its own random secret)."""
    if len(runner.args.base_url) < 2:
        return None
    first = next(s for s in runner.sessions if s.base_url == runner.args.base_url[0])
    other = runner.args.base_url[1]
    async with httpx.AsyncClient(base_url=other, timeout=runner.args.timeout, cookies=first.client.cookies) as c:
        r = await c.get("/api/auth/me", headers={"X-Auth-Mode": "cookie", "X-Forwarded-For": first.xff})
    return {"issued_by": first.base_url, "used_on": other, "status": r.status_code,
            "shared_session": r.status_code == 200}


def default_results_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "reports" / "load" / f"run_{stamp}"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", action="append", default=None,
                   help="API base URL; repeat for several replicas (requests round-robin). Default http://127.0.0.1:8199")
    p.add_argument("--manifest", default=None, help="seed manifest (default reports/load/vtest_load_seed.json)")
    p.add_argument("--users", default="", help="comma-separated emails instead of the manifest")
    p.add_argument("--users-privileged", action="store_true", help="--users are admins (enables export_small)")
    p.add_argument("--password", default="", help="password for --users (default: the manifest's)")
    p.add_argument("--concurrency", type=int, default=25, help="virtual users (RP-131 uses 25)")
    p.add_argument("--duration", type=float, default=60, help="seconds, ramp included")
    p.add_argument("--ramp", type=float, default=10, help="seconds over which virtual users start")
    p.add_argument("--think", type=float, default=0.2, help="mean think time between a VU's requests (s)")
    p.add_argument("--timeout", type=float, default=30)
    p.add_argument("--mix", default="", help="override weights, e.g. contacts_list=40,export_small=0")
    p.add_argument("--target", action="append", default=[],
                   help="threshold endpoint.metric=value (metrics p50/p95/p99/max in ms, error_rate 0-1, rps "
                        "minimum); replaces the default for the same endpoint.metric. Repeatable")
    p.add_argument("--no-default-targets", action="store_true")
    p.add_argument("--min-samples", type=int, default=5, help="targets on endpoints with fewer requests SKIP")
    p.add_argument("--results", default=None, help="path prefix for .json and .md (default reports/load/run_<ts>)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--no-xff", action="store_true", help="do not send a per-VU X-Forwarded-For address")
    return p


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    a = build_parser().parse_args(argv)
    a.base_url = [u.rstrip("/") for u in (a.base_url or ["http://127.0.0.1:8199"])]
    a.manifest = a.manifest or str(ROOT / "reports" / "load" / "vtest_load_seed.json")
    if a.ramp >= a.duration:
        raise SystemExit("--ramp must be shorter than --duration")
    a.weights = loadlib.parse_mix(a.mix, DEFAULT_MIX, SCENARIOS)
    defaults = [] if a.no_default_targets else DEFAULT_TARGETS
    a.targets = loadlib.merge_targets(defaults, [loadlib.parse_target(t) for t in a.target])
    return a


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    runner = Runner(args)
    users, password = load_users(args)
    runner.stop_at = float("inf")
    await sign_in_all(runner, users, password)
    shared = await cross_replica_check(runner)
    print(f"signed in {len(runner.sessions)} sessions; discovering visible records ...", file=sys.stderr, flush=True)
    first = [s for s in runner.sessions if s.base_url == args.base_url[0]]
    await asyncio.gather(*(discover(runner, s) for s in first))  # once per user; replicas share the database
    seen = {s.email: s for s in first}
    for s in runner.sessions:
        if s.base_url != args.base_url[0]:
            s.contacts, s.campaigns = list(seen[s.email].contacts), list(seen[s.email].campaigns)
    if not any(s.contacts for s in runner.sessions):
        raise RuntimeError("no contacts visible to any user: is the database seeded?")
    if not any(s.role in PRIVILEGED for s in runner.sessions):
        args.weights.update({k: 0 for k, (_fn, roles) in SCENARIOS.items() if roles})  # e.g. export: no admin
    mix = loadlib.WeightedMix(args.weights)
    runner.started = time.perf_counter()
    runner.stop_at = runner.started + args.duration
    print(f"running {args.concurrency} virtual users for {args.duration:g}s ...", file=sys.stderr, flush=True)
    tasks = [asyncio.create_task(virtual_user(runner, vu, mix)) for vu in range(args.concurrency)]
    # hard stop: a request in flight at the deadline gets at most --timeout more seconds
    _done, pending = await asyncio.wait(tasks, timeout=args.duration + args.timeout + 5)
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    elapsed = time.perf_counter() - runner.started
    for s in runner.sessions:
        await s.client.aclose()
    return {"runner": runner, "elapsed": elapsed, "shared": shared, "users": len(users)}


def report(args: argparse.Namespace, result: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    runner: Runner = result["runner"]
    elapsed = result["elapsed"]
    summary = loadlib.summarize(runner.samples, elapsed)
    by_replica = loadlib.summarize(runner.samples, elapsed, key="base_url")
    verdicts = loadlib.evaluate(summary, args.targets, args.min_samples)
    meta = {
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base_urls": ", ".join(args.base_url),
        "concurrency": args.concurrency, "duration_s": round(elapsed, 1), "ramp_s": args.ramp,
        "think_s": args.think, "signed_in_users": result["users"], "sessions": len(runner.sessions),
        "per_vu_x_forwarded_for": not args.no_xff, "session_refreshes": runner.refreshes,
        "cross_replica_session": None if result["shared"] is None else
        f"{result['shared']['status']} (shared JWT secret: {result['shared']['shared_session']})",
        "mix": ", ".join(f"{k}={v:g}" for k, v in args.weights.items() if v),
    }
    data = {"meta": meta, "summary": summary, "by_replica": by_replica, "thresholds": verdicts,
            "passed": not loadlib.breached(verdicts)}
    return data, loadlib.markdown_report(meta, summary, by_replica, verdicts)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(run(args))
    except (RuntimeError, httpx.HTTPError, OSError, KeyError, ValueError) as e:
        print(f"load test could not start: {e}", file=sys.stderr)
        return 2
    data, md = report(args, result)
    prefix = Path(args.results) if args.results else default_results_path()
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(json.dumps(data, indent=2, default=str))
    prefix.with_suffix(".md").write_text(md)
    print(md)
    print(f"Results: {prefix.with_suffix('.json')} and {prefix.with_suffix('.md')}")
    return 0 if data["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
