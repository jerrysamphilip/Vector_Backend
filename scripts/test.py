#!/usr/bin/env python3
"""
Outreach 360 test runner: one command per layer, results captured and compared between runs.

    python3 scripts/test.py list [--layer L] [--json]
    python3 scripts/test.py doctor [--layer L]
    python3 scripts/test.py run smoke|core|full|all|unit|functional|system|e2e|ui [--suite ID[,ID]] [--only X]
                                   [--jobs N] [--skip ID[,ID]] [--timings RUN_DIR] [--report-dir DIR]
                                   [--keep-going] [--dry-run] [--restart-api-between] [--pause S]
                                   [--where auto|host|docker] [--keep-db] [--extra="ARGS"] [--force]
    python3 scripts/test.py tiers [--check] [--jobs N]    case counts / estimates per tier; validate TIERS.json
    python3 scripts/test.py report [RUN_DIR]          re-aggregate a run directory (summary + comparison)
    python3 scripts/test.py collect                   snapshot the suites' current results files (no tests run)
    python3 scripts/test.py env up|down|status [--e2e] [--build] [--execute]
    python3 scripts/test.py db create|drop|reset|status
    python3 scripts/test.py trace [--brd FILE] [--out docs/TRACEABILITY.md] [--from-run DIR] [--check]

Tiers (system_tests/TIERS.json): smoke (every deploy) < core (nightly / before UAT) < full (= all: every suite,
every case). A tier names suites and, for suites it cuts down, case-id patterns passed to them as TEST_CASES.
Layers: unit (pure logic) -> functional (TestClient + MySQL) -> system (live stack) -> e2e (worker + SES
stand-in + fake IMAP) -> ui (Playwright). Suites come from tests/registry.json plus discovery of new
tests/unit/test_*.py, tests/test_*.py, system_tests/*/run_*.py and system_tests/ui_features/*.mjs.

Results go to reports/test-results/<timestamp>/ (summary.md, summary.json, logs/, raw/) and
reports/test-results/latest points at the newest run. Exit code: 0 all passed, 1 a case failed or a suite
errored, 2 suites were skipped for missing prerequisites (nothing failed), 3 another run holds the lock,
130 interrupted. Python 3 standard library only. See docs/TESTING.md.
"""
import argparse
import datetime as dt
import fnmatch
import glob
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tierlib  # noqa: E402  (scripts/tierlib.py: tier selection, standard library only)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_FILE = Path(os.environ.get("TEST_REGISTRY") or ROOT / "tests" / "registry.json")
REPORTS_DIR = ROOT / "reports"
RESULTS_DIR = REPORTS_DIR / "test-results"
WORK_DIR = REPORTS_DIR / ".work"
LOCK_FILE = REPORTS_DIR / ".runner.lock"
LEDGER_FILE = REPORTS_DIR / ".ratelimit.json"
TIERS_FILE = Path(os.environ.get("TEST_TIERS") or ROOT / "system_tests" / "TIERS.json")
LAYERS = ["unit", "functional", "system", "e2e", "ui"]
LIVE_LAYERS = {"system", "e2e", "ui"}
STATUSES = ("PASS", "FAIL", "BLOCKED")


def env(name, default=""):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# ── Configuration (all overridable through environment variables) ──
CFG = {
    "API_URL": env("API_URL", "http://localhost:8191").rstrip("/"),
    "WEB_URL": env("WEB_URL", "http://localhost:8190/vector").rstrip("/"),
    "SES_MOCK_URL": env("SES_MOCK_URL", "http://127.0.0.1:" + env("SES_MOCK_PORT", "5001")).rstrip("/"),
    "MYSQL_CONTAINER": env("MYSQL_CONTAINER", "vector-mysql-1"),
    "API_CONTAINER": env("TEST_API_CONTAINER", "vector-api-1"),
    "WORKER_CONTAINER": env("TEST_WORKER_CONTAINER", "vector-worker-1"),
    "SES_CONTAINER": env("TEST_SES_CONTAINER", "vector-ses-mock-1"),
    "APP_DB": env("MYSQL_DATABASE", "outreach_ai"),
    "TEST_DB": env("TEST_DB_NAME", "vtest"),
    "DB_ROOT_PASSWORD": env("TEST_DB_ROOT_PASSWORD", env("LOCAL_MYSQL_ROOT_PASSWORD", "root")),
    "PY_IMAGE": env("TEST_PY_IMAGE", "vector-api:latest"),
    "DOCKER_NETWORK": env("TEST_DOCKER_NETWORK", "vector_default"),
    "CA_CERT": env("TEST_CA_CERT", ""),
    "FRONTEND_DIR": str((ROOT / env("FRONTEND_DIR", "../Vector_Frontend")).resolve()),
    "COMPOSE_OVERRIDE": env("TEST_COMPOSE_OVERRIDE", ""),
    "SANDBOX_DOCKERFILE": env("TEST_SANDBOX_DOCKERFILE", "Dockerfile"),
    "CERTS_DIR": env("TEST_CERTS_DIR", ""),
    "CHROMIUM": env("CHROMIUM", "/opt/pw-browsers/chromium"),
    "PLAYWRIGHT_VERSION": env("TEST_PLAYWRIGHT_VERSION", "1.63.0"),
}

# ── Small helpers ──

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(text, color):
    if not USE_COLOR:
        return text
    codes = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "dim": "2", "bold": "1"}
    return f"\033[{codes[color]}m{text}\033[0m"


def say(msg=""):
    print(msg, flush=True)


def die(msg, code=2):
    say(c("error: ", "red") + msg)
    sys.exit(code)


def rel(p):
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def sh(cmd, timeout=30, cwd=None, env_=None):
    """Run a command quietly. Returns (exit code, stdout+stderr). Never raises for a missing binary."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env_)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    except OSError as exc:
        return 126, str(exc)


def http_get(url, timeout=5):
    """GET without proxies. Returns (status, body text) or (0, error)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as r:
            return r.status, r.read(200_000).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - any network problem is "unreachable"
        return 0, str(exc)


def fmt_secs(s):
    if s is None:
        return "-"
    s = int(round(s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else (f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def git_info(path):
    if not (Path(path) / ".git").exists():
        return {}
    _, sha = sh(["git", "-C", str(path), "rev-parse", "--short", "HEAD"])
    _, branch = sh(["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"])
    _, status = sh(["git", "-C", str(path), "status", "--porcelain"])
    return {"commit": sha.strip(), "branch": branch.strip(), "dirty_files": len([x for x in status.splitlines() if x.strip()])}


# ════════════════════════════════════════════════════════════════════════
#  Registry and discovery
# ════════════════════════════════════════════════════════════════════════

def load_registry():
    if not REGISTRY_FILE.exists():
        die(f"suite registry not found: {rel(REGISTRY_FILE)}")
    try:
        with open(REGISTRY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except ValueError as exc:
        die(f"{rel(REGISTRY_FILE)} is not valid JSON: {exc}")


def _subst(value, ctx):
    if isinstance(value, str):
        def repl(m):
            key = m.group(1)
            return str(ctx[key]) if key in ctx else m.group(0)
        return re.sub(r"\{(\w+)\}", repl, value)
    if isinstance(value, list):
        return [_subst(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _subst(v, ctx) for k, v in value.items()}
    return value


def discover(reg):
    """Explicit suites + suites discovered from new files. Returns a list of suite dicts (resolved)."""
    suites = []
    claimed = set()
    for s in reg.get("suites", []):
        s = dict(s)
        s.setdefault("order", 50)
        s["discovered"] = False
        if s.get("script"):
            claimed.add(str((ROOT / s["script"]).resolve()))
        suites.append(s)
    ids = {s["id"] for s in suites}
    for rule in reg.get("discovery", []):
        if rule.get("covered_by"):
            continue
        files = sorted(glob.glob(str(ROOT / rule["glob"])))
        files = [f for f in files if not any(fnmatch.fnmatch(os.path.basename(f), x) for x in rule.get("exclude", []))]
        files = [f for f in files if str(Path(f).resolve()) not in claimed]
        per_dir = {}
        for f in files:
            per_dir.setdefault(os.path.dirname(f), []).append(f)
        for f in files:
            p = Path(f)
            ctx = {"dir": rel(p.parent), "name": p.name, "stem": p.stem, "parent": p.parent.name}
            several = len(per_dir[str(p.parent)]) > 1
            sid = _subst(rule.get("suite_id_if_several") if several and rule.get("suite_id_if_several")
                         else rule["suite_id"], ctx)
            if sid in ids:
                sid = f"{sid}.{p.stem}"
            ids.add(sid)
            s = {k: v for k, v in rule.items() if k not in ("glob", "exclude", "suite_id", "suite_id_if_several", "id")}
            s = _subst(s, ctx)
            s.update({"id": sid, "script": rel(p), "discovered": True, "discovery_rule": rule.get("id")})
            suites.append(s)
    return suites


def ordered(suites):
    """Execution order: layer order, then 'order', with run_last suites at the very end."""
    lo = {name: i for i, name in enumerate(LAYERS)}
    return sorted(suites, key=lambda s: (1 if s.get("run_last") else 0, lo.get(s["layer"], 9), s.get("order", 50), s["id"]))


def suite_files(reg, suite):
    """Files a pytest suite covers (for counts in `list`)."""
    if suite.get("kind") != "pytest":
        return []
    out = []
    for rule in reg.get("discovery", []):
        if rule.get("covered_by") == suite["id"]:
            out += sorted(glob.glob(str(ROOT / rule["glob"])))
    return out


TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.M)


def count_pytest_functions(path):
    try:
        return len(TEST_DEF.findall(Path(path).read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return 0


def result_paths(suite, report_dir=None):
    res = suite.get("result", {})
    paths = res.get("path")
    if paths is None:
        return []
    paths = paths if isinstance(paths, list) else [paths]
    ctx = {"report_dir": str(report_dir) if report_dir else str(RESULTS_DIR / "_dry")}
    return [Path(_subst(p, ctx)) if os.path.isabs(_subst(p, ctx)) else ROOT / _subst(p, ctx) for p in paths]


def last_results(suite):
    """Counts from the suite's current results file (what the last run wrote), or None."""
    if suite.get("result", {}).get("written_by_runner"):
        return None
    for p in result_paths(suite):
        if p.exists():
            data = load_json(p)
            if isinstance(data, dict) and isinstance(data.get("cases"), list):
                return {"path": rel(p), "mtime": p.stat().st_mtime, "cases": len(data["cases"]),
                        "counts": normalize_counts(data["cases"])}
    return None


def normalize_counts(cases):
    counts = {k: 0 for k in STATUSES}
    for case in cases:
        st = str(case.get("status", "")).upper()
        counts[st if st in counts else "FAIL"] += 1
    return counts


# ════════════════════════════════════════════════════════════════════════
#  Prerequisite checks (doctor and per-suite gating)
# ════════════════════════════════════════════════════════════════════════

class Check:
    def __init__(self, key, level, message, fix=""):
        self.key, self.level, self.message, self.fix = key, level, message, fix   # level: ok | warn | missing

    @property
    def ok(self):
        return self.level != "missing"


_CHECK_CACHE = {}


def check(key):
    if key not in _CHECK_CACHE:
        fn = CHECKS.get(key)
        if fn is None:
            _CHECK_CACHE[key] = Check(key, "warn", f"unknown prerequisite '{key}' in the registry (ignored)")
        else:
            try:
                _CHECK_CACHE[key] = fn()
            except Exception as exc:  # noqa: BLE001 - a broken check must not crash the runner
                _CHECK_CACHE[key] = Check(key, "missing", f"check failed: {exc!r}")
    return _CHECK_CACHE[key]


def python_for_tests():
    explicit = env("TEST_PYTHON")
    if explicit:
        return explicit
    for cand in (ROOT / "venv" / "bin" / "python", ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return sys.executable or "python3"


PY_DEPS = "import pytest, fastapi, sqlalchemy, pymysql, pydantic_settings, holidays, httpx"


def host_pytest_ok():
    rc, out = sh([python_for_tests(), "-c", PY_DEPS], timeout=60, cwd=str(ROOT))
    return rc == 0, out.strip().splitlines()[-1] if out.strip() else ""


def docker_ok():
    if not shutil.which("docker"):
        return False, "docker CLI not installed"
    rc, out = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)
    return rc == 0, out.strip()


def image_exists(image):
    rc, _ = sh(["docker", "image", "inspect", image], timeout=15)
    return rc == 0


_CONTAINER_ENV = {}


def container_env(name):
    """Selected settings of a running container (secrets reported only as set/empty)."""
    if name in _CONTAINER_ENV:
        return _CONTAINER_ENV[name]
    script = ('echo "ENVIRONMENT=$ENVIRONMENT"; echo "ALLOW_SELF_SIGNUP=$ALLOW_SELF_SIGNUP"; '
              'echo "SEND_ON_ANY_DAY=$SEND_ON_ANY_DAY"; echo "AWS_ENDPOINT_URL=$AWS_ENDPOINT_URL"; '
              'echo "OPENAI_API_KEY=${OPENAI_API_KEY:+set}"; echo "OPENAI_MAX_USD_PER_DAY=$OPENAI_MAX_USD_PER_DAY"')
    rc, out = sh(["docker", "exec", name, "sh", "-c", script], timeout=15)
    vals = None
    if rc == 0:
        vals = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    _CONTAINER_ENV[name] = vals
    return vals


def container_running(name):
    rc, out = sh(["docker", "inspect", "-f", "{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.StartedAt}}", name], timeout=15)
    if rc != 0:
        return None
    running, health, started = (out.strip().split("|") + ["", "", ""])[:3]
    return {"running": running == "true", "health": health, "started": started}


def parse_docker_time(value):
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})", value or "")
    if not m:
        return None
    return dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00").timestamp()


FIX_DOCKER = ("start the Docker daemon. If `docker info` says it cannot connect although dockerd was running before "
              "(typical after a container/VM restart): `pgrep -a dockerd || (rm -f /var/run/docker.pid "
              "/var/run/docker/containerd/containerd.pid && (dockerd > /tmp/dockerd.log 2>&1 &))`, then `docker info`")
FIX_STACK = "python3 scripts/test.py env up --execute   (or: docker compose up -d, see docker/LOCAL_DOCKER.md)"


def chk_pytest_runtime():
    ok, detail = host_pytest_ok()
    if ok:
        return Check("pytest_runtime", "ok", f"host Python has the app + test dependencies ({python_for_tests()})")
    dok, _ = docker_ok()
    if dok and image_exists(CFG["PY_IMAGE"]):
        return Check("pytest_runtime", "ok",
                     f"host Python lacks the dependencies ({detail}); will run pytest in a container from {CFG['PY_IMAGE']} "
                     f"(installs requirements-dev.txt; set TEST_CA_CERT if pip needs a CA bundle)")
    return Check("pytest_runtime", "missing", f"no Python with the app dependencies ({detail}) and no usable docker image "
                 f"{CFG['PY_IMAGE']}", "python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt "
                 "(the runner picks up ./venv automatically), or build the image: docker compose build api")


def chk_docker():
    ok, detail = docker_ok()
    if ok:
        return Check("docker", "ok", f"docker daemon {detail}")
    return Check("docker", "missing", f"docker not usable: {detail[:200]}", FIX_DOCKER)


def _db_via_container():
    return env("TEST_DB_MANAGE", "1") != "0" and docker_ok()[0] and (container_running(CFG["MYSQL_CONTAINER"]) or {}).get("running")


def chk_test_db():
    if _db_via_container():
        rc, out = db_exec(f"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{CFG['TEST_DB']}'")
        if rc != 0:
            return Check("test_db", "missing", f"cannot run SQL in {CFG['MYSQL_CONTAINER']}: {out.strip()[:200]}",
                         "check TEST_DB_ROOT_PASSWORD (default root) and that the mysql container is healthy")
        note = "exists" if CFG["TEST_DB"] in out else "does not exist yet (the functional run creates it)"
        return Check("test_db", "ok", f"MySQL in {CFG['MYSQL_CONTAINER']} reachable; throwaway schema '{CFG['TEST_DB']}' {note}")
    host, port = env("MYSQL_HOST", "127.0.0.1"), int(env("MYSQL_PORT", "3316" if not env("MYSQL_HOST") else "3306"))
    try:
        with socket.create_connection((host, port), timeout=3):
            pass
        return Check("test_db", "ok", f"MySQL reachable at {host}:{port} (database {env('MYSQL_DATABASE', CFG['TEST_DB'])}, "
                     "must be an empty throwaway schema)")
    except OSError as exc:
        return Check("test_db", "missing", f"no MySQL for the functional tests ({host}:{port}: {exc})",
                     f"{FIX_STACK}; then python3 scripts/test.py db create")


def chk_api():
    s, body = http_get(CFG["API_URL"] + "/health")
    if s == 200:
        return Check("api", "ok", f"API healthy at {CFG['API_URL']}")
    return Check("api", "missing", f"API not healthy at {CFG['API_URL']}/health ({s or body[:120]})", FIX_STACK)


def chk_openapi():
    s, _ = http_get(CFG["API_URL"] + "/openapi.json", timeout=15)
    if s == 200:
        return Check("openapi", "ok", "OpenAPI document served")
    return Check("openapi", "missing", f"{CFG['API_URL']}/openapi.json -> {s}",
                 "the API must run with ENVIRONMENT other than production (or ENABLE_DOCS=true)")


def chk_web():
    s, _ = http_get(CFG["WEB_URL"] + "/")
    if s == 200:
        return Check("web", "ok", f"web app at {CFG['WEB_URL']}")
    return Check("web", "missing", f"web app not reachable at {CFG['WEB_URL']}/ ({s})",
                 "FRONTEND_DIR=../Vector_Frontend docker compose up -d web")


def chk_mysql_container():
    if not docker_ok()[0]:
        return Check("mysql_container", "missing", "docker not usable", FIX_DOCKER)
    rc, out = sh(["docker", "exec", CFG["MYSQL_CONTAINER"], "mysqladmin", "ping", "-uvector", "-pvector", "--silent"], timeout=15)
    if rc == 0:
        return Check("mysql_container", "ok", f"{CFG['MYSQL_CONTAINER']} answers (suites run SQL checks through docker exec)")
    return Check("mysql_container", "missing", f"{CFG['MYSQL_CONTAINER']}: {out.strip()[:160]}", FIX_STACK)


def chk_worker():
    st = container_running(CFG["WORKER_CONTAINER"]) if docker_ok()[0] else None
    if st and st["running"]:
        return Check("worker", "ok" if st["health"] in ("", "healthy") else "warn",
                     f"{CFG['WORKER_CONTAINER']} running ({st['health'] or 'no healthcheck'})")
    return Check("worker", "missing", f"{CFG['WORKER_CONTAINER']} is not running (scheduler / IMAP sync cases need it)",
                 "docker compose up -d worker")


def chk_signup():
    vals = container_env(CFG["API_CONTAINER"]) if docker_ok()[0] else None
    if vals is None:
        s, _ = http_get(CFG["API_URL"] + "/health")
        return Check("signup", "warn" if s == 200 else "missing",
                     "cannot read the API container settings; assuming self sign-up is allowed", FIX_STACK)
    envname = (vals.get("ENVIRONMENT") or "").lower()
    allow = (vals.get("ALLOW_SELF_SIGNUP") or "").lower()
    if allow != "true":  # off unless explicitly enabled (I-12)
        return Check("signup", "missing", f"self sign-up is off (ENVIRONMENT={envname or '-'}, ALLOW_SELF_SIGNUP={allow or '-'})",
                     "run the stack with ALLOW_SELF_SIGNUP=true")
    return Check("signup", "ok", f"self sign-up allowed (ENVIRONMENT={envname or 'default'})")


def chk_e2e_stack():
    problems = []
    s, _ = http_get(CFG["SES_MOCK_URL"] + "/moto-api/")
    if s != 200:
        problems.append(f"SES stand-in not reachable at {CFG['SES_MOCK_URL']}")
    for name in (CFG["API_CONTAINER"], CFG["WORKER_CONTAINER"]):
        vals = container_env(name) if docker_ok()[0] else None
        if not vals or "ses-mock" not in (vals.get("AWS_ENDPOINT_URL") or "") or (vals.get("SEND_ON_ANY_DAY") or "").lower() != "true":
            problems.append(f"{name} not wired to the stand-in (AWS_ENDPOINT_URL / SEND_ON_ANY_DAY)")
    if problems:
        return Check("e2e_stack", "missing", "; ".join(problems), "python3 scripts/test.py env up --e2e --execute "
                     "(needs TEST_COMPOSE_OVERRIDE / TEST_SANDBOX_DOCKERFILE / TEST_CERTS_DIR, see docs/TESTING.md)")
    return Check("e2e_stack", "ok", "SES stand-in up; api + worker wired (AWS_ENDPOINT_URL, SEND_ON_ANY_DAY=true)")


def chk_node():
    rc, out = sh(["node", "--version"])
    m = re.match(r"v(\d+)", out.strip())
    if rc == 0 and m and int(m.group(1)) >= 18:
        return Check("node", "ok", f"node {out.strip()}")
    return Check("node", "missing", f"node 18+ not found ({out.strip()[:80]})", "install Node.js 18 or newer")


def chk_playwright():
    pkg = ROOT / "system_tests" / "node_modules" / "playwright" / "package.json"
    if pkg.exists():
        version = (load_json(pkg) or {}).get("version", "?")
        return Check("playwright", "ok", f"playwright {version} in system_tests/node_modules")
    return Check("playwright", "missing", "playwright is not installed in system_tests/node_modules",
                 f"cd system_tests && npm install --no-save --no-package-lock playwright@{CFG['PLAYWRIGHT_VERSION']}")


def chk_chromium():
    path = CFG["CHROMIUM"]
    if os.path.exists(path):
        hard = "/opt/pw-browsers/chromium"
        note = "" if os.path.exists(hard) else f" (contacts/sales UI scripts hard-code {hard}; symlink it)"
        return Check("chromium", "ok" if not note else "warn", f"chromium at {path}{note}",
                     f"mkdir -p /opt/pw-browsers && ln -sf {path} {hard}" if note else "")
    return Check("chromium", "missing", f"no Chromium at {path}",
                 "cd system_tests && npx playwright install chromium && export CHROMIUM=$(node -e "
                 "\"console.log(require('playwright').chromium.executablePath())\") && mkdir -p /opt/pw-browsers && "
                 "ln -sf \"$CHROMIUM\" /opt/pw-browsers/chromium")


def chk_frontend_src():
    p = Path(CFG["FRONTEND_DIR"]) / "src"
    if p.is_dir():
        return Check("frontend_src", "ok", f"frontend source at {p}")
    return Check("frontend_src", "missing", f"frontend source not found at {p}", "set FRONTEND_DIR to the Vector_Frontend checkout")


def chk_openssl():
    if shutil.which("openssl"):
        return Check("openssl", "ok", "openssl available (fake IMAP server certificate)")
    return Check("openssl", "missing", "openssl not found (the fake IMAP server makes a self-signed certificate)",
                 "apt-get install -y openssl")


def chk_ruff():
    if shutil.which("ruff"):
        return Check("ruff", "ok", "ruff available")
    return Check("ruff", "warn", "ruff not on PATH (CI lint gate)", "pip install ruff==0.16.8")


def chk_rate_limits():
    st = container_running(CFG["API_CONTAINER"]) if docker_ok()[0] else None
    started = parse_docker_time(st["started"]) if st else None
    since = max(started or 0, time.time() - 3600)
    ledger = load_json(LEDGER_FILE, []) or []
    regs = sum(e.get("registers", 0) for e in ledger if e.get("ts", 0) >= since)
    lock = [e for e in ledger if e.get("locks_logins") and e.get("ts", 0) >= max(started or 0, time.time() - 900)]
    msgs, level = [], "ok"
    mult = rate_limit_multiplier()
    budget = 10 * mult - regs
    msgs.append(f"~{regs} sign-ups by this runner since the API started / in the last hour (limit {10 * mult} per hour per IP"
                + (f", {RATE_LIMIT_SETTING}={mult}" if mult > 1 else "") + "; other tools and agents on this machine share it)")
    if budget <= 3:
        level = "warn"
    if lock and mult == 1:
        level = "warn"
        msgs.append("reports_platform ran in the last 15 minutes: logins from this IP may be locked")
    if started:
        msgs.append(f"{CFG['API_CONTAINER']} up for {fmt_secs(time.time() - started)}")
    return Check("rate_limits", level, "; ".join(msgs),
                 f"docker restart {CFG['API_CONTAINER']} clears the in-memory limiter (or use run --restart-api-between)"
                 if level == "warn" else "")


def us_weekend_now():
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 - no tz database: approximate with UTC-5
        now = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)
    return now.weekday() >= 5, now


def chk_weekday():
    weekend, now = us_weekend_now()
    vals = container_env(CFG["API_CONTAINER"]) if docker_ok()[0] else None
    any_day = (vals or {}).get("SEND_ON_ANY_DAY", "").lower() == "true"
    if weekend and not any_day:
        return Check("weekday", "warn", f"it is {now:%A} in New York: the scheduler holds sends outside US business days, so "
                     "sending cases in outreach / sales_pipeline are BLOCKED", "run on a weekday, or use the e2e stack "
                     "(SEND_ON_ANY_DAY=true, never in production)")
    return Check("weekday", "ok", f"{now:%A} in New York" + (" (SEND_ON_ANY_DAY=true on the stack)" if any_day else ""))


def chk_openai():
    vals = container_env(CFG["API_CONTAINER"]) if docker_ok()[0] else None
    if vals is None:
        return Check("openai", "ok", "API container not inspected; the runner always gives test processes OPENAI_API_KEY=\"\"")
    if vals.get("OPENAI_API_KEY") == "set":
        return Check("openai", "warn", f"OPENAI_API_KEY is set in {CFG['API_CONTAINER']}: AI features exercised by the suites "
                     f"(wizard generation, reply classification) would call OpenAI (cap ${vals.get('OPENAI_MAX_USD_PER_DAY') or '0.20'}/day)",
                     "recreate the stack without a key for test runs: OPENAI_API_KEY= docker compose up -d api worker")
    return Check("openai", "ok", "no OpenAI key in the API container (LLM paths use their fallbacks)")


CHECKS = {
    "pytest_runtime": chk_pytest_runtime, "docker": chk_docker, "test_db": chk_test_db, "api": chk_api,
    "openapi": chk_openapi, "web": chk_web, "mysql_container": chk_mysql_container, "worker": chk_worker,
    "signup": chk_signup, "e2e_stack": chk_e2e_stack, "node": chk_node, "playwright": chk_playwright,
    "chromium": chk_chromium, "frontend_src": chk_frontend_src, "openssl": chk_openssl, "ruff": chk_ruff,
    "rate_limits": chk_rate_limits, "weekday": chk_weekday, "openai": chk_openai,
}

DOCTOR_LAYERS = {
    "general": ["ruff", "docker"],
    "unit": ["pytest_runtime"],
    "functional": ["pytest_runtime", "test_db"],
    "system": ["api", "openapi", "mysql_container", "worker", "signup", "openssl", "frontend_src", "web",
               "rate_limits", "weekday", "openai"],
    "e2e": ["e2e_stack"],
    "ui": ["web", "node", "playwright", "chromium"],
}


def cmd_doctor(args):
    groups = DOCTOR_LAYERS if not args.layer else {"general": DOCTOR_LAYERS["general"], args.layer: DOCTOR_LAYERS[args.layer]}
    missing = 0
    say(c("Outreach 360 test prerequisites", "bold") + c(f"   (API {CFG['API_URL']}, web {CFG['WEB_URL']})", "dim"))
    for group, keys in groups.items():
        say("\n" + c(group.upper(), "bold"))
        for key in keys:
            ch = check(key)
            tag = {"ok": c("  OK     ", "green"), "warn": c("  WARN   ", "yellow"), "missing": c("  MISSING", "red")}[ch.level]
            say(f"{tag} {key:16} {ch.message}")
            if ch.fix and ch.level != "ok":
                say(c(f"           fix: {ch.fix}", "dim"))
            missing += ch.level == "missing"
    say("")
    say("Order constraints: suites run one at a time unless --jobs N (parallel-safe suites from system_tests/TIERS.json);"
        " reports_platform alone and last unless the stack runs with RATE_LIMIT_TEST_MULTIPLIER > 1."
        " Use `run ... --restart-api-between` when the sign-up budget is short (not needed with the multiplier).")
    say(c(f"{missing} prerequisite(s) missing." if missing else "All prerequisites present.", "yellow" if missing else "green"))
    return 1 if missing else 0


# ════════════════════════════════════════════════════════════════════════
#  Test database (functional layer)
# ════════════════════════════════════════════════════════════════════════

def _safe_db_name(name):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name or "") or "test" not in name.lower():
        die(f"refusing to manage database '{name}': TEST_DB_NAME must be a throwaway name containing 'test'")
    if name == CFG["APP_DB"] or name.lower() == "outreach_ai":
        die(f"refusing to manage the application database '{name}'")
    return name


def uses_container_db(where):
    """The functional run uses (and may reset) the throwaway schema inside the compose MySQL container when
    pytest runs in docker, or on the host without an explicit MYSQL_HOST (127.0.0.1:3316 = that container)."""
    return _db_via_container() and (where == "docker" or not os.environ.get("MYSQL_HOST"))


def db_exec(sql):
    return sh(["docker", "exec", CFG["MYSQL_CONTAINER"], "mysql", "-uroot", f"-p{CFG['DB_ROOT_PASSWORD']}", "-N", "-e", sql], timeout=60)


def db_action(action, quiet=False):
    name = _safe_db_name(CFG["TEST_DB"])
    if not _db_via_container():
        msg = (f"MySQL container {CFG['MYSQL_CONTAINER']} not running (or TEST_DB_MANAGE=0): manage '{name}' yourself, "
               "e.g. a CI service container")
        if action == "status":
            say(msg)
            return 0
        say(c("warning: ", "yellow") + msg)
        return 2
    if action == "status":
        rc, out = db_exec(f"SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='{name}'; "
                          f"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='{name}'")
        nums = re.findall(r"^\s*(\d+)\s*$", out, re.M)
        if rc != 0 or len(nums) < 2:
            say(c("error: ", "red") + out.strip()[:300])
            return 1
        say(f"database '{name}' in {CFG['MYSQL_CONTAINER']}: " + (f"exists, {nums[1]} tables" if nums[0] != "0" else "does not exist"))
        return 0
    sqls = {
        # The one collation every table declares (app/core/db_charset.py, D1), like the CI mysql service
        "create": [f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci",
                   f"GRANT ALL PRIVILEGES ON `{name}`.* TO 'vector'@'%'"],
        "drop": [f"DROP DATABASE IF EXISTS `{name}`"],
    }
    steps = sqls["drop"] + sqls["create"] if action == "reset" else sqls[action]
    for sql in steps:
        rc, out = db_exec(sql)
        if rc != 0:
            lines = [ln for ln in out.splitlines() if "Using a password" not in ln]
            say(c("error: ", "red") + f"{sql}: {' '.join(lines)[:300]}")
            return 1
    if not quiet:
        say(f"{action}: database '{name}' in {CFG['MYSQL_CONTAINER']} done")
    return 0


def cmd_db(args):
    return db_action(args.action)


# ════════════════════════════════════════════════════════════════════════
#  Stack (env up / down)
# ════════════════════════════════════════════════════════════════════════

def compose_base(e2e=False):
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    if CFG["COMPOSE_OVERRIDE"]:
        cmd += ["-f", CFG["COMPOSE_OVERRIDE"]]
    if e2e:
        cmd += ["-f", "system_tests/e2e/docker-compose.e2e.yml"]
    return cmd


def env_plan(action, e2e, build):
    """Commands for env up/down as (description, argv, extra env)."""
    fenv = {"FRONTEND_DIR": os.path.relpath(CFG["FRONTEND_DIR"], ROOT)}
    prebuilt = bool(CFG["COMPOSE_OVERRIDE"])          # the sandbox override resets build: images are prebuilt
    certs = ["--build-context", f"certs={CFG['CERTS_DIR']}"] if CFG["CERTS_DIR"] else []
    steps = []
    if action == "up" and not e2e:
        flag = ["--build"] if build else (["--no-build"] if prebuilt else [])
        steps.append(("start the plain stack (mysql, migrate, api, worker, web)", compose_base() + ["up", "-d"] + flag, fenv))
    elif action == "up" and e2e:
        if build or not image_exists("vector-api:e2e"):
            steps.append(("build the app image from the current tree as vector-api:e2e",
                          ["docker", "build", "-f", CFG["SANDBOX_DOCKERFILE"]] + certs + ["-t", "vector-api:e2e", "."], {}))
        if build or not image_exists("vector-ses-mock"):
            steps.append(("build the SES stand-in (moto) image vector-ses-mock (FROM vector-api:latest; needs TEST_CERTS_DIR)",
                          ["docker", "build", "-f", "system_tests/e2e/Dockerfile.ses-mock"] + certs
                          + ["-t", "vector-ses-mock", "system_tests/e2e"], {}))
        steps.append(("start ses-mock and re-create api + worker wired to it (SEND_ON_ANY_DAY=true)",
                      compose_base(e2e=True) + ["up", "-d", "--no-build", "ses-mock", "api", "worker"], fenv))
    elif action == "down" and e2e:
        steps.append(("re-create api + worker without the stand-in wiring", compose_base() + ["up", "-d", "--no-build", "api", "worker"], fenv))
        steps.append(("remove the SES stand-in", ["docker", "rm", "-f", CFG["SES_CONTAINER"]], {}))
    elif action == "down":
        steps.append(("stop the stack (data volumes are kept; add -v by hand to wipe MySQL)", compose_base() + ["down"], fenv))
    return steps


def cmd_env(args):
    if args.action == "status":
        rc, out = sh(["docker", "ps", "-a", "--filter", "name=vector-", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
        say(out.strip() if rc == 0 else f"docker not usable: {out.strip()}")
        for key in ("api", "web", "e2e_stack"):
            ch = check(key)
            say(f"{ch.level.upper():8} {key:10} {ch.message}")
        return 0
    if args.e2e and args.action == "up" and not CFG["CERTS_DIR"] and (args.build or not image_exists("vector-ses-mock")):
        say(c("note: ", "yellow") + "building the SES stand-in needs TEST_CERTS_DIR=<dir containing ca.crt> (a CA bundle for pip, "
            "e.g. a copy of /etc/ssl/certs/ca-certificates.crt named ca.crt).")
    steps = env_plan(args.action, args.e2e, args.build)
    say(c(f"# env {args.action}{' --e2e' if args.e2e else ''}   (cd {ROOT})", "bold"))
    for desc, argv, extra in steps:
        prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in extra.items())
        say(c(f"# {desc}", "dim"))
        say((prefix + " " if prefix else "") + " ".join(shlex.quote(a) for a in argv))
    if not args.execute:
        say(c("\n(printed only; add --execute to run these commands)", "dim"))
        return 0
    for desc, argv, extra in steps:
        say(c(f"\n>> {desc}", "blue"))
        rc = subprocess.call(argv, cwd=str(ROOT), env={**os.environ, **extra})
        if rc != 0:
            say(c("error: ", "red") + f"step failed with exit code {rc}")
            return rc
    if args.action == "up":
        say("waiting for the API to become healthy ...")
        if not wait_api(240):
            say(c("warning: ", "yellow") + "API not healthy after 240 s; check `docker compose logs api`")
            return 1
        say(c("API healthy.", "green"))
    return 0


def wait_api(seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if http_get(CFG["API_URL"] + "/health", timeout=3)[0] == 200:
            return True
        time.sleep(2)
    return False


def restart_api():
    say(c(f"   restarting {CFG['API_CONTAINER']} to clear the in-memory rate limiter ...", "dim"))
    rc, out = sh(["docker", "restart", CFG["API_CONTAINER"]], timeout=180)
    if rc != 0:
        say(c("   warning: ", "yellow") + f"restart failed: {out.strip()[:200]}")
        return False
    ok = wait_api(180)
    _CONTAINER_ENV.pop(CFG["API_CONTAINER"], None)
    say(c("   API healthy again." if ok else "   warning: API not healthy 180 s after the restart", "dim" if ok else "yellow"))
    return ok


# ════════════════════════════════════════════════════════════════════════
#  Result formats
# ════════════════════════════════════════════════════════════════════════

def parse_results_json(path):
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError(f"{rel(path)} has no 'cases' list")
    cases = []
    for r in data["cases"]:
        st = str(r.get("status", "")).upper()
        cases.append({"id": str(r.get("id", "?")), "title": str(r.get("title", ""))[:300],
                      "status": st if st in STATUSES else "FAIL", "category": r.get("category") or "",
                      "detail": str(r.get("note") or r.get("detail") or "")[:500],
                      **({"carried_over": True} if r.get("carried_over") else {})})
    return cases, data.get("seconds"), data.get("suite")


def parse_junit(path):
    root = ET.parse(path).getroot()
    cases, seconds = [], 0.0
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    for ts in suites:
        try:
            seconds += float(ts.get("time") or 0)
        except ValueError:
            pass
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "")
        status, detail, category = "PASS", "", ""
        for tag in ("failure", "error"):
            el = tc.find(tag)
            if el is not None:
                status, category = "FAIL", "defect" if tag == "failure" else "test-error"
                detail = (el.get("message") or el.text or "")[:500]
        sk = tc.find("skipped")
        if status == "PASS" and sk is not None:
            status, category, detail = "BLOCKED", "skipped", (sk.get("message") or "")[:500]
        cases.append({"id": f"{cls}::{name}" if cls else name, "title": name, "status": status,
                      "category": category, "detail": detail})
    return cases, round(seconds, 1), None


def parse_contract(path):
    data = load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"{rel(path)} is not a contract report")
    missing = data.get("missing") or []
    cases = [{"id": "CONTRACT-SUMMARY", "title": "Every frontend API call has a backend route with the same method",
              "status": "PASS" if not missing else "FAIL", "category": "" if not missing else "defect",
              "detail": f"calls {data.get('calls')}, matched {data.get('matched')}, unmatched {len(missing)}, "
                        f"dynamic {len(data.get('dynamic') or [])}"}]
    for m in missing:
        cases.append({"id": f"CONTRACT-{m.get('method')} {m.get('path')}", "title": f"{m.get('method')} {m.get('path')}",
                      "status": "FAIL", "category": "defect", "detail": f"{m.get('file')}:{m.get('line')} - {m.get('note')}"})
    return cases, None, "contract"


PARSERS = {"results_json": parse_results_json, "junit": parse_junit, "contract": parse_contract}


def suite_status(cases, error):
    if error:
        return "ERROR"
    counts = normalize_counts(cases)
    if counts["FAIL"]:
        return "FAIL"
    if not cases:
        return "ERROR"
    if counts["PASS"] == 0:
        return "BLOCKED"
    return "PASS"


# ════════════════════════════════════════════════════════════════════════
#  Running suites
# ════════════════════════════════════════════════════════════════════════

def pw_workdir():
    """Writable scratch dir for suites (PW_DIR/PW_BASE/PW_MODULES) that resolves the shared Playwright install."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    link = WORK_DIR / "node_modules"
    target = ROOT / "system_tests" / "node_modules"
    if target.is_dir() and not link.exists():
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            pass
    return str(WORK_DIR)


def child_env(suite, only):
    e = dict(os.environ)
    work = pw_workdir()
    e.update({"API_URL": CFG["API_URL"], "WEB_URL": CFG["WEB_URL"], "MYSQL_CONTAINER": CFG["MYSQL_CONTAINER"],
              "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "OPENAI_API_KEY": "",
              "FRONTEND_DIR": CFG["FRONTEND_DIR"]})
    for key in ("PW_DIR", "PW_BASE", "PW_MODULES"):
        e.setdefault(key, work)
    if os.path.exists(CFG["CHROMIUM"]):
        e.setdefault("CHROMIUM", CFG["CHROMIUM"])
        e.setdefault("PW_CHROMIUM", CFG["CHROMIUM"])
    o = suite.get("only") or {}
    if only and o.get("mode") == "env":
        e[o["env"]] = only
    return e


def pytest_plan(suite, args, report_dir, where):
    """argv, cwd, env and junit path for a pytest suite (host or docker)."""
    junit_name = f"junit-{suite['id']}.xml"
    pyargs = list(suite.get("pytest_args", [])) + ["-q", "-p", "no:cacheprovider"]
    if args.only:
        pyargs += ["-k", args.only]
    if args.extra:
        pyargs += shlex.split(args.extra)
    functional = suite.get("needs_test_db")
    if where == "host":
        junit = Path(report_dir) / "raw" / junit_name
        e = child_env(suite, None)
        e.update({"ENVIRONMENT": "test", "ALLOW_SELF_SIGNUP": "true", "RUN_BACKGROUND_JOBS": "false",
                  "OUTREACH_IGNORE_DOTENV": "1"})  # never the developer's .env keys (app.core.config)
        if functional:
            if not os.environ.get("TEST_ALLOW_DATABASE_URL"):
                e.pop("DATABASE_URL", None)       # would win over MYSQL_* in app.core.config: never test
                e.pop("Database_URL", None)       # against a database the runner did not choose
            e.setdefault("MYSQL_HOST", "127.0.0.1")
            e.setdefault("MYSQL_PORT", "3316" if e["MYSQL_HOST"] in ("127.0.0.1", "localhost") and not os.environ.get("MYSQL_PORT") else "3306")
            e.setdefault("MYSQL_USER", "vector")
            e.setdefault("MYSQL_PASSWORD", "vector")
            if not os.environ.get("MYSQL_DATABASE") or os.environ.get("MYSQL_DATABASE") == CFG["APP_DB"]:
                e["MYSQL_DATABASE"] = CFG["TEST_DB"]
        argv = [python_for_tests(), "-m", "pytest"] + pyargs + [f"--junitxml={junit}"]
        return argv, str(ROOT), e, junit, junit
    # docker: the repo is mounted at /src; the junit file is written inside the repo, then moved
    inside = WORK_DIR / junit_name
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    denv = {"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "ENVIRONMENT": "test", "ALLOW_SELF_SIGNUP": "true",
            "OUTREACH_IGNORE_DOTENV": "1",  # the repo (and its .env) is mounted at /src
            "RUN_BACKGROUND_JOBS": "false", "OPENAI_API_KEY": "", "AWS_EC2_METADATA_DISABLED": "true",
            "JWT_SECRET_KEY": "local-test-only-0123456789abcdef0123456789",
            "CREDENTIALS_ENCRYPTION_KEY": "local-test-only-credentials-key"}
    if functional:
        denv.update({"MYSQL_HOST": "mysql", "MYSQL_PORT": "3306", "MYSQL_USER": "vector", "MYSQL_PASSWORD": "vector",
                     "MYSQL_DATABASE": CFG["TEST_DB"]})
    # Proxies on the host's loopback are unreachable from a container: pass only routable ones.
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "PIP_INDEX_URL"):
        v = os.environ.get(k)
        if v and env("TEST_DOCKER_PROXY", "1") != "0" and not re.search(r"//(localhost|127\.|\[?::1)", v):
            denv[k] = v
            denv.setdefault("NO_PROXY", "mysql,localhost,127.0.0.1")
    network = CFG["DOCKER_NETWORK"]
    if not functional and sh(["docker", "network", "inspect", network], timeout=15)[0] != 0:
        network = "bridge"
    argv = ["docker", "run", "--rm", "--network", network, "-u", "0"]
    for k, v in denv.items():
        argv += ["-e", f"{k}={v}"]
    argv += ["-v", f"{ROOT}:/src", "-w", "/src"]
    pip_cert = ""
    ca = CFG["CA_CERT"] or (os.environ.get("PIP_CERT") if os.path.isfile(os.environ.get("PIP_CERT") or "") else "")
    if ca:
        argv += ["-v", f"{Path(ca).resolve()}:/tmp/ca.crt:ro"]
        pip_cert = "--cert /tmp/ca.crt "
    # The app image has the runtime dependencies; add pytest/httpx/pytest-cov unless the image already has them.
    argv += ["--entrypoint", "sh", CFG["PY_IMAGE"], "-c",
             "python -c 'import pytest, httpx, pytest_cov' 2>/dev/null || "
             f"pip install -q --disable-pip-version-check {pip_cert}-r requirements-dev.txt >/tmp/pip.log 2>&1 "
             f"|| {{ cat /tmp/pip.log; exit 3; }}; python -m pytest "
             + " ".join(shlex.quote(a) for a in pyargs) + f" --junitxml=/src/{rel(inside)}"]
    return argv, str(ROOT), child_env(suite, None), inside, Path(report_dir) / "raw" / junit_name


def command_plan(suite, args, report_dir):
    result_file = result_paths(suite, report_dir)[0] if result_paths(suite, report_dir) else None
    ctx = {"root": str(ROOT), "API_URL": CFG["API_URL"], "WEB_URL": CFG["WEB_URL"], "FRONTEND_DIR": CFG["FRONTEND_DIR"],
           "report_dir": str(report_dir), "result": str(result_file) if result_file else "", "only": args.only or ""}
    argv = _subst(list(suite["command"]), ctx)
    o = suite.get("only") or {}
    if args.only and o.get("mode") == "arg":
        argv += shlex.split(_subst(o["template"], ctx))
    if args.extra:
        argv += shlex.split(args.extra)
    cwd = str(ROOT / _subst(suite.get("cwd", "."), ctx))
    return argv + list((suite.get("_tier") or {}).get("args") or []), cwd, tier_env(suite, child_env(suite, args.only))


def tier_env(suite, e):
    """A tier entry's env plus its case selection as TEST_CASES (system_tests/common.py CaseFilter, ui_common caseFilter)."""
    entry = suite.get("_tier") or {}
    e.update(entry.get("env") or {})
    if entry.get("cases"):
        e[tierlib.CASES_ENV] = ",".join(entry["cases"])
    else:
        e.pop(tierlib.CASES_ENV, None)  # never inherit a stale selection from the caller's shell
    return e


def stream(argv, cwd, env_, log_path, timeout, quiet):
    """Run argv in its own process group, tee output to log_path. Returns (exit code, timed_out)."""
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write(f"$ (cd {cwd} && {' '.join(shlex.quote(a) for a in argv)})\n")
        log.flush()
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env_, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, errors="replace", bufsize=1, start_new_session=True)
        except (FileNotFoundError, PermissionError) as exc:
            log.write(f"cannot start: {exc}\n")
            return 127, False
        RUNNING.add(proc)
        try:
            return _wait_streamed(proc, log, timeout, quiet)
        finally:
            RUNNING.discard(proc)


RUNNING = set()  # child processes of suites that are running (parallel pools have several)


def kill_running():
    for proc in list(RUNNING):
        _kill(proc)


def _wait_streamed(proc, log, timeout, quiet):
    def pump():
        for line in proc.stdout:
            log.write(line)
            if not quiet:
                sys.stdout.write("   " + line)
                sys.stdout.flush()
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
    except KeyboardInterrupt:
        _kill(proc)
        t.join(5)
        raise
    t.join(10)
    if timed_out:
        log.write(f"\n[runner] killed after the {timeout}s timeout\n")
    return proc.returncode, timed_out


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(10)
    except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def resolve_where(args):
    if args.where != "auto":
        return args.where
    ok, _ = host_pytest_ok()
    return "host" if ok else "docker"


def run_one(suite, args, report_dir, where):
    """Run a suite and return its record for summary.json."""
    rec = {"id": suite["id"], "layer": suite["layer"], "discovered": suite.get("discovered", False),
           "started": now_iso(), "status": "ERROR", "counts": {k: 0 for k in STATUSES}, "cases": [],
           "seconds": None, "exit_code": None, "error": None, "warnings": [],
           "log": f"logs/{suite['id']}.log"}
    log_path = Path(report_dir) / rec["log"]
    t0 = time.time()
    if suite.get("kind") == "pytest":
        argv, cwd, e, produced, final = pytest_plan(suite, args, report_dir, where)
        result_candidates = [produced]
        if produced.exists():
            produced.unlink()
    else:
        argv, cwd, e = command_plan(suite, args, report_dir)
        result_candidates = result_paths(suite, report_dir)
        if suite.get("result", {}).get("written_by_runner"):
            for p in result_candidates:
                p.parent.mkdir(parents=True, exist_ok=True)
        final = None
    rec["command"] = " ".join(shlex.quote(a) for a in argv)
    rec["cwd"] = rel(cwd)
    timeout = int(suite.get("timeout_s") or max(900, 4 * int(suite.get("duration_s") or 300)))
    rc, timed_out = stream(argv, cwd, e, log_path, timeout, args.quiet)
    rec["exit_code"], rec["seconds"] = rc, round(time.time() - t0, 1)
    fresh = [p for p in result_candidates if p.exists() and p.stat().st_mtime >= t0 - 2]
    if not fresh:
        stale = [p for p in result_candidates if p.exists()]
        rec["error"] = ("timed out after %ss; " % timeout if timed_out else "") + (
            f"exit code {rc}; no fresh results file (" + (f"stale: {rel(stale[0])}" if stale else
                                                          "expected " + ", ".join(rel(p) for p in result_candidates)) + f"); see {rec['log']}")
    else:
        src = fresh[0]
        fmt = suite.get("result", {}).get("format", "results_json")
        try:
            cases, secs, _ = PARSERS[fmt](src)
            rec["cases"] = cases
            rec["result_file"] = rel(src)
            raw = final or Path(report_dir) / "raw" / f"{suite['id']}{src.suffix or '.json'}"
            raw.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != raw.resolve():
                shutil.copy2(src, raw)
                if suite.get("kind") == "pytest":
                    src.unlink()
            rec["raw"] = rel(raw) if str(raw).startswith(str(ROOT)) else str(raw)
            if timed_out:
                rec["error"] = f"timed out after {timeout}s (partial results)"
            elif suite.get("kind") == "pytest" and rc not in (0, 1):
                rec["error"] = f"pytest exit code {rc} (collection/usage error); see {rec['log']}"
        except Exception as exc:  # noqa: BLE001 - a malformed file is a suite error, not a crash
            rec["error"] = f"cannot read {rel(src)}: {exc}"
    rec["counts"] = normalize_counts(rec["cases"])
    rec["status"] = suite_status(rec["cases"], rec["error"])
    rec["finished"] = now_iso()
    return rec


def acquire_lock():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    info = load_json(LOCK_FILE)
    if info and info.get("pid"):
        try:
            os.kill(int(info["pid"]), 0)
            say(c("error: ", "red") + f"another test run is in progress (pid {info['pid']}, started {info.get('started')}: "
                f"{info.get('cmd')}). System/e2e/UI suites must never run in parallel. If that process is gone, delete {rel(LOCK_FILE)}.")
            sys.exit(3)
        except (ProcessLookupError, ValueError):
            pass
        except PermissionError:
            die(f"lock {rel(LOCK_FILE)} is held by pid {info['pid']} (another user)", 3)
    write_json(LOCK_FILE, {"pid": os.getpid(), "started": now_iso(), "cmd": " ".join(sys.argv)})


def release_lock():
    info = load_json(LOCK_FILE)
    if info and info.get("pid") == os.getpid():
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def ledger_add(suite):
    if suite["layer"] not in LIVE_LAYERS:
        return
    ledger = [e for e in (load_json(LEDGER_FILE, []) or []) if e.get("ts", 0) > time.time() - 7200]
    ledger.append({"ts": time.time(), "suite": suite["id"], "registers": int(suite.get("registers") or 0),
                   "locks_logins": bool(suite.get("locks_logins"))})
    write_json(LEDGER_FILE, ledger)


def select_suites(all_suites, layer, wanted):
    picked = [s for s in all_suites if layer == "all" or s["layer"] == layer]
    if wanted:
        names = [w.strip() for part in wanted for w in part.split(",") if w.strip()]
        known = {s["id"]: s for s in all_suites}
        unknown = [n for n in names if n not in known and not any(fnmatch.fnmatch(i, n) for i in known)]
        if unknown:
            die(f"unknown suite(s): {', '.join(unknown)}. Known: {', '.join(sorted(known))}")
        picked = [s for s in all_suites if any(s["id"] == n or fnmatch.fnmatch(s["id"], n) for n in names)]
        if layer != "all":
            other = [s["id"] for s in picked if s["layer"] != layer]
            if other:
                say(c("note: ", "yellow") + f"running {', '.join(other)} although not in layer '{layer}' (named with --suite)")
    return ordered(picked)


def new_run_dir(args, label=""):
    if args.report_dir:
        d = Path(args.report_dir)
        if not d.is_absolute():
            d = (Path.cwd() / d).resolve()
        return d
    stamp = time.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
    d = RESULTS_DIR / stamp
    n = 2
    while d.exists():
        d = RESULTS_DIR / f"{stamp}-{n}"
        n += 1
    return d


def plan_warnings(suites, args):
    warns = []
    live = [s for s in suites if s["layer"] in LIVE_LAYERS]
    if not live:
        return warns
    regs = sum(int(s.get("registers") or 0) for s in live)
    budget_check = check("rate_limits")
    used = int(re.search(r"~(\d+)", budget_check.message).group(1)) if re.search(r"~(\d+)", budget_check.message) else 0
    budget = 10 * rate_limit_multiplier()
    if regs + used > budget and not args.restart_api_between:
        warns.append(f"the selected suites make ~{regs} sign-ups (+~{used} already this hour); the API allows {budget} per hour "
                     f"per IP. Add --restart-api-between (restarts {CFG['API_CONTAINER']} between live suites) or run fewer suites.")
    if any(s.get("weekday_sensitive") for s in live):
        ch = check("weekday")
        if ch.level != "ok":
            warns.append(ch.message)
    if any(s.get("locks_logins") for s in live) and live[-1].get("locks_logins") is not True and not rate_limits_relaxed():
        warns.append("reports_platform is not last in this plan; later suites may hit the login lockout")
    if check("openai").level == "warn":
        warns.append(check("openai").message)
    return warns + parallel_warnings(live, args)


def parallel_warnings(live, args):
    if args.jobs <= 1 or len(live) < 2 or rate_limits_relaxed():
        return []
    return [f"--jobs {args.jobs} runs live suites at the same time, and the API's per-IP limits (60 logins / 5 min, "
            f"10 sign-ups / hour) are shared by all of them. Run the stack with {RATE_LIMIT_SETTING}=100 "
            f"(docs/TESTING.md section 7) or expect 429s; --restart-api-between only "
            "restarts between steps, never while a pool is running."]


# ── Rate-limit multiplier of the stack ──
# A test stack may run the API with RATE_LIMIT_TEST_MULTIPLIER > 1 (app/core/rate_limit.py; refused in production):
# the per-client-IP limits (logins 60 / 5 min, sign-ups 10 / hour) are multiplied, per-email lockouts never change.
# Then the runner needs no --restart-api-between / --pause (skipped), scales the sign-up budget, runs --jobs pools
# without the 429 warning and does not force reports_platform last (its RP-140..142 send X-Test-Default-Rate-Limits
# and count against the real limits in a bucket of their own). Multiplier 1 (the default) keeps the old behaviour.
# TEST_RATE_LIMIT_MULTIPLIER=<n> states the value for a stack the runner cannot inspect (e.g. a remote one).
RATE_LIMIT_SETTING = "RATE_LIMIT_TEST_MULTIPLIER"
_RELAXED = {}


def rate_limit_multiplier():
    if "m" not in _RELAXED:
        _RELAXED["m"] = _probe_multiplier()
    return _RELAXED["m"]


def rate_limits_relaxed():
    return rate_limit_multiplier() > 1


def _probe_multiplier():
    raw = env("TEST_RATE_LIMIT_MULTIPLIER")
    if not raw and docker_ok()[0]:
        rc, out = sh(["docker", "exec", CFG["API_CONTAINER"], "printenv", RATE_LIMIT_SETTING], timeout=15)
        raw = out.strip() if rc == 0 else ""
    try:
        return max(1, int(raw or 1))
    except ValueError:
        return 1


# ── Tiers (system_tests/TIERS.json, scripts/tierlib.py) ──

def load_tiers(required):
    """The tiers file, or None when it is missing and not required (layer runs then plan everything serial)."""
    if not TIERS_FILE.exists() and not required:
        return None
    try:
        return tierlib.load(TIERS_FILE)
    except tierlib.TierError as exc:
        die(str(exc))


def is_tier(name):
    return name in tierlib.TIER_NAMES or name in tierlib.ALIASES


def tier_suites(args, all_suites, data):
    """(suites of the tier with their entry in s['_tier'], ids left out of it). Dies on an invalid tier."""
    try:
        sel = tierlib.resolve(data, args.layer, all_suites)
    except tierlib.TierError as exc:
        die(str(exc))
    picked = [dict(s, _tier=sel[s["id"]]) for s in ordered(all_suites) if s["id"] in sel]
    bad = [s["id"] for s in picked if s.get("kind") == "pytest" and s["_tier"].get("cases")]
    if bad:
        die(f"{rel(TIERS_FILE)}: pytest suites cannot be cut by case ids ({', '.join(bad)}); list them whole")
    left_out = [s["id"] for s in ordered(all_suites) if s["id"] not in sel]
    return picked, left_out


def skip_suites(suites, skip):
    names = [w.strip() for part in (skip or []) for w in part.split(",") if w.strip()]
    return [s for s in suites if not any(s["id"] == n or fnmatch.fnmatch(s["id"], n) for n in names)]


def unpin_last(suites):
    """With relaxed per-IP limits reports_platform's lockout probes use their own bucket: no need to run it last."""
    if not rate_limits_relaxed():
        return suites
    return ordered([{**s, "run_last": False} for s in suites])


def tier_patterns(suite):
    return (suite.get("_tier") or {}).get("cases")


def default_timing_dirs():
    return sorted((p for p in RESULTS_DIR.glob("*") if p.is_dir() and not p.is_symlink() and (p / "summary.json").exists()),
                  key=lambda p: p.name, reverse=True) if RESULTS_DIR.is_dir() else []


def load_timings(dirs):
    """{suite id: {"seconds", "cases": [ids], "filtered", "run"}} from run directories (or summary.json files):
    each suite takes its newest run where it finished (not SKIPPED / NOT_RUN / ERROR)."""
    runs = []
    for d in dirs:
        p = Path(d)
        data = load_json(p if p.is_file() else p / "summary.json")
        if isinstance(data, dict):
            runs.append(data)
    runs.sort(key=lambda r: str(r.get("finished") or ""), reverse=True)
    out = {}
    for data in runs:
        for r in data.get("suites", []):
            if r["id"] in out or not r.get("seconds") or r.get("status") in ("SKIPPED", "NOT_RUN", "ERROR"):
                continue
            out[r["id"]] = {"seconds": r["seconds"], "cases": [cs["id"] for cs in r.get("cases", [])],
                            "filtered": bool((r.get("tier") or {}).get("patterns")), "run": data.get("run_id")}
    return out


def case_catalog(suite, timing):
    """Every case id known for a suite: the timing run's cases plus the suite's results files (carried-over rows
    included, so a file written by a cut run still lists the whole suite)."""
    ids = list((timing or {}).get("cases") or [])
    if suite.get("kind") != "pytest" and not suite.get("result", {}).get("written_by_runner"):
        for p in result_paths(suite):
            data = load_json(p) if p.exists() else None
            if isinstance(data, dict) and isinstance(data.get("cases"), list):
                ids += [str(cs.get("id")) for cs in data["cases"] if cs.get("id")]
    return list(dict.fromkeys(ids))


def suite_safety(data, suite):
    if not data:
        return {"parallel": False, "locks": [], "reason": f"no {rel(TIERS_FILE)}"}
    return tierlib.safety(data, suite["id"], suite.get("_tier"))


def plan_rows(suites, data, timings):
    """Per suite: safety, catalog, selected / excluded case ids and the estimated seconds."""
    rows = {}
    for s in suites:
        timing = timings.get(s["id"])
        catalog = case_catalog(s, timing)
        selected, excluded = tierlib.split_cases(catalog, tier_patterns(s))
        secs, src = tierlib.estimate(s.get("_tier") or {}, timing, catalog, s.get("duration_s"))
        rows[s["id"]] = {"safety": suite_safety(data, s), "catalog": catalog, "selected": selected,
                         "excluded": excluded, "est": secs, "est_src": src}
    return rows


def estimate_steps(steps, rows, jobs):
    total = 0.0
    for st in steps:
        items = [(rows[s["id"]]["est"], rows[s["id"]]["safety"]["locks"]) for s in st["suites"]]
        st["est"] = tierlib.simulate(items, jobs) if st["parallel"] else sum(i[0] for i in items)
        total += st["est"]
    return total


def build_plan(args):
    reg = load_registry()
    all_suites = discover(reg)
    tier = is_tier(args.layer)
    data = load_tiers(required=tier)
    if tier:
        suites, left_out = tier_suites(args, all_suites, data)
        suites = select_suites(suites, "all", args.suite) if args.suite else suites
    else:
        suites, left_out = select_suites(all_suites, args.layer, args.suite), []
    suites = unpin_last(skip_suites(suites, args.skip))
    if not suites:
        die(f"no suites selected for '{args.layer}'")
    timings = load_timings(list(args.timings or []) + default_timing_dirs())
    rows = plan_rows(suites, data, timings)
    steps = tierlib.plan_steps(suites, lambda s: rows[s["id"]]["safety"], args.jobs, lambda s: rows[s["id"]]["est"])
    plan = {"tier": tierlib.ALIASES.get(args.layer, args.layer) if tier else None, "data": data, "suites": suites,
            "left_out": left_out, "rows": rows, "steps": steps, "all_suites": all_suites, "timings": timings,
            "index": {s["id"]: i for i, s in enumerate(suites, 1)}}
    plan["est_total"] = estimate_steps(steps, rows, args.jobs)
    return plan


def resolve_pytest_where(args, suites):
    where = resolve_where(args) if any(s.get("kind") == "pytest" for s in suites) else None
    if where == "host" and not host_pytest_ok()[0]:
        _CHECK_CACHE["pytest_runtime"] = Check("pytest_runtime", "missing", f"--where host, but {python_for_tests()} cannot "
                                               f"import the app/test dependencies", "pip install -r requirements.txt -r "
                                               "requirements-dev.txt (or set TEST_PYTHON), or use --where docker")
    elif where == "docker" and not (docker_ok()[0] and image_exists(CFG["PY_IMAGE"])):
        _CHECK_CACHE["pytest_runtime"] = Check("pytest_runtime", "missing", f"--where docker, but docker or the image "
                                               f"{CFG['PY_IMAGE']} is not available", FIX_DOCKER + "; docker compose build api")
    return where


def _cases_cell(row, suite):
    if tier_patterns(suite) is None:
        return f"all {len(row['catalog'])}" if row["catalog"] else "all"
    return f"{len(row['selected'])}/{len(row['catalog'])}"


def print_plan(plan, args, report_dir):
    head = f"Plan: {plan['tier'] or args.layer}" + (" tier" if plan["tier"] else "") + f", {len(plan['suites'])} suite(s), "
    say(c(head + f"estimated {fmt_secs(plan['est_total'])} with --jobs {args.jobs}", "bold") +
        c(f"   -> {rel(report_dir)}" if not args.dry_run else "   (dry run)", "dim"))
    say(c(f"  {'#':>2}  {'layer':10} {'suite':28} {'mode':22} {'cases':>10} {'estimate':>9}  source", "dim"))
    for s in plan["suites"]:
        row, sf = plan["rows"][s["id"]], plan["rows"][s["id"]]["safety"]
        mode = ("parallel" + (f" [{','.join(sf['locks'])}]" if sf["locks"] else "")) if sf["parallel"] else "serial"
        flags = " ".join(x for x in ["[discovered]" if s.get("discovered") else "", "[last]" if s.get("run_last") else ""] if x)
        say(f"  {plan['index'][s['id']]:2}. {s['layer']:10} {s['id']:28} {mode:22} {_cases_cell(row, s):>10} "
            f"{fmt_secs(row['est']):>9}  {row['est_src']} {flags}".rstrip())
    print_steps(plan, args)
    if plan["left_out"]:
        say(c(f"  not in this tier (they run in full): {', '.join(plan['left_out'])}", "dim"))
    for w in plan_warnings(plan["suites"], args):
        say(c("  warning: ", "yellow") + w)
    if args.only:
        for s in plan["suites"]:
            if not (s.get("only") or {}).get("mode"):
                say(c("  note: ", "yellow") + f"{s['id']} has no case filter; --only is ignored for it")


def print_steps(plan, args):
    say(c("  steps:", "dim"))
    for i, st in enumerate(plan["steps"], 1):
        ids = [s["id"] for s in st["suites"]]
        kind = f"parallel pool (up to {args.jobs} at once)" if st["parallel"] else "serial"
        say(f"    {i:2}. {kind:30} ~{fmt_secs(st['est']):>7}  {', '.join(ids)}")


def print_tier_overview(plan, args):
    """Case counts and estimates of every tier (so a dry run shows what smoke / core / full cost side by side)."""
    data = plan["data"]
    if not data:
        return
    say(c("\nTiers overview", "bold") + c(f"   (estimates with --jobs {args.jobs}; per-suite times from earlier runs scaled "
                                          "by the share of cases selected)", "dim"))
    for name in tierlib.TIER_NAMES:
        try:
            sel = tierlib.resolve(data, name, plan["all_suites"])
        except tierlib.TierError as exc:
            say(f"  {name:6} invalid: {exc}")
            continue
        suites = skip_suites([dict(s, _tier=sel[s["id"]]) for s in ordered(plan["all_suites"]) if s["id"] in sel], args.skip)
        rows = plan_rows(suites, data, plan["timings"])
        steps = tierlib.plan_steps(suites, lambda s: rows[s["id"]]["safety"], args.jobs, lambda s: rows[s["id"]]["est"])
        total = estimate_steps(steps, rows, args.jobs)
        n_sel = sum(len(r["selected"]) for r in rows.values())
        n_all = sum(len(r["catalog"]) for r in rows.values())
        target = data["tiers"][name].get("target_s")
        say(f"  {name:6} {len(suites):3} suites  {n_sel:6} of {n_all} known cases  ~{fmt_secs(total):>7}"
            + (f"  (target {fmt_secs(target)})" if target else ""))


def print_dry_details(plan, args, report_dir, where):
    say("")
    for s in plan["suites"]:
        say(c(f"== {s['id']}", "bold") + c(f"  ({s['layer']}{', discovered' if s.get('discovered') else ''})", "dim"))
        if s.get("kind") == "pytest":
            argv, cwd, _, _, final = pytest_plan(s, args, report_dir, where)
            say(f"   runs on: {where}")
            if s.get("needs_test_db") and not args.keep_db and uses_container_db(where):
                say(f"   before:  python3 scripts/test.py db reset   (database '{CFG['TEST_DB']}' in {CFG['MYSQL_CONTAINER']})")
            results = [final]
        else:
            argv, cwd, _ = command_plan(s, args, report_dir)
            results = result_paths(s, report_dir)
        say(f"   cwd:     {rel(cwd)}")
        say(f"   command: {' '.join(shlex.quote(a) for a in argv)}")
        if tier_patterns(s) is not None:
            say(f"   cases:   {tierlib.CASES_ENV}={','.join(tier_patterns(s))[:300]}")
        say(f"   results: {', '.join(rel(p) for p in results)}  ({s.get('result', {}).get('format')})")
        say(f"   safety:  {plan['rows'][s['id']]['safety']['reason']}")
        for key in s.get("prereqs", []) + s.get("soft_prereqs", []):
            ch = check(key)
            soft = key in s.get("soft_prereqs", [])
            tag = "ok" if ch.level == "ok" else ("warn" if ch.level == "warn" or soft else "MISSING")
            say(f"   prereq:  {tag:7} {key:16} {ch.message[:110]}")
        if s.get("notes"):
            say(c(f"   notes:   {s['notes']}", "dim"))


# ── Execution: steps run one after another; a parallel step is a pool of suites ──

_PRINT_LOCK = threading.Lock()
_LEDGER_LOCK = threading.Lock()


def run_checked(s, plan, args, state, report_dir, where):
    """Prerequisites, test-db reset, the suite itself, ledger and the result line. Thread-safe."""
    n = f"[{plan['index'][s['id']]}/{len(plan['suites'])}]"
    missing = [check(k) for k in s.get("prereqs", []) if not check(k).ok]
    with _PRINT_LOCK:
        say(c(f"\n{n} {s['id']} ({s['layer']})", "bold"))
        for ch in [check(k) for k in s.get("soft_prereqs", []) if check(k).level != "ok"]:
            say(c("   warning: ", "yellow") + f"{ch.key}: {ch.message} (part of the suite will be BLOCKED or fail)")
        for ch in (missing if not args.force else []):
            say(c("   missing: ", "red") + f"{ch.key}: {ch.message}" + (f"\n            fix: {ch.fix}" if ch.fix else ""))
    if missing and not args.force:
        state["records"][s["id"]] = {"id": s["id"], "layer": s["layer"], "status": "SKIPPED", "counts": {k: 0 for k in STATUSES},
                                     "cases": [], "error": None,
                                     "skipped": [{"prereq": ch.key, "message": ch.message, "fix": ch.fix} for ch in missing]}
        return
    if s.get("needs_test_db") and not args.keep_db and uses_container_db(where):
        if db_action("reset", quiet=True) != 0:
            say(c("   warning: ", "yellow") + "could not reset the test database; running against it as it is")
    rec = apply_tier_filter(s, run_one(s, args, report_dir, where), plan)
    if s["layer"] in LIVE_LAYERS:
        with _LEDGER_LOCK:
            ledger_add(s)
    state["records"][s["id"]] = rec
    report_result(s, rec, n, args, state)


def apply_tier_filter(s, rec, plan):
    """Keep only the cases the tier selected (a suite may have merged older rows into its results file) and note
    which known cases the tier excluded."""
    patterns = tier_patterns(s)
    if patterns is None:
        return rec
    kept, dropped = tierlib.filter_cases(rec["cases"], patterns)
    row = plan["rows"][s["id"]]
    rec["cases"] = kept
    rec["counts"] = normalize_counts(kept)
    rec["status"] = suite_status(kept, rec.get("error"))
    rec["tier"] = {"tier": plan["tier"], "patterns": patterns, "known_cases": len(row["catalog"]),
                   "excluded": row["excluded"], "dropped_rows": len(dropped)}
    return rec


def report_result(s, rec, n, args, state):
    col = {"PASS": "green", "FAIL": "red", "ERROR": "red", "BLOCKED": "yellow"}.get(rec["status"], "yellow")
    with _PRINT_LOCK:
        say(c(f"   {n} {s['id']} => {rec['status']}", col) + f"  PASS {rec['counts']['PASS']} FAIL {rec['counts']['FAIL']} "
            f"BLOCKED {rec['counts']['BLOCKED']}  ({fmt_secs(rec['seconds'])})" + (f"  {rec['error']}" if rec.get("error") else ""))
    if rec["status"] in ("FAIL", "ERROR") and not args.keep_going and not state["stop"]:
        state["stop"] = f"stopped after {s['id']} ({rec['status']}); use --keep-going to run the remaining suites"
        with _PRINT_LOCK:
            say(c("   " + state["stop"], "yellow"))


def between_steps(step, args, state):
    """--restart-api-between / --pause, only between steps (never while a pool runs) and only when needed."""
    live = any(s["layer"] in LIVE_LAYERS for s in step["suites"])
    if not (live and state["last_live"] and (args.restart_api_between or args.pause)):
        state["last_live"] = state["last_live"] or live
        return
    if rate_limits_relaxed():
        if not state.get("relaxed_noted"):
            say(c(f"   rate limits relaxed on the stack ({RATE_LIMIT_SETTING}={rate_limit_multiplier()}): "
                  "--restart-api-between / --pause are not needed and are skipped", "dim"))
            state["relaxed_noted"] = True
        return
    if args.restart_api_between:
        restart_api()
    if args.pause:
        say(c(f"   pausing {args.pause}s between live suites", "dim"))
        time.sleep(args.pause)


def run_pool(step, plan, args, state, report_dir, where):
    pool_args = argparse.Namespace(**{**vars(args), "quiet": True})  # interleaved output is unreadable: logs only
    pending, running = list(step["suites"]), {}
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        try:
            while pending or running:
                if not state["stop"]:
                    launch_ready(pending, running, ex, plan, pool_args, state, report_dir, where)
                if not running:
                    break
                done, _ = futures_wait(list(running), timeout=1, return_when=FIRST_COMPLETED)
                for f in done:
                    running.pop(f)
                    f.result()
        except KeyboardInterrupt:
            kill_running()
            raise


def launch_ready(pending, running, ex, plan, args, state, report_dir, where):
    for s in list(pending):
        held = [lk for locks in running.values() for lk in locks]
        locks = plan["rows"][s["id"]]["safety"]["locks"]
        if len(running) >= max(1, args.jobs):
            return
        if tierlib.can_start(locks, held):
            pending.remove(s)
            running[ex.submit(run_checked, s, plan, args, state, report_dir, where)] = locks


def execute(plan, args, report_dir, where):
    state = {"records": {}, "stop": None, "last_live": False}
    for k in sorted({k for s in plan["suites"] for k in s.get("prereqs", []) + s.get("soft_prereqs", [])}):
        check(k)  # warm the cache once, before suites start in parallel
    try:
        for step in plan["steps"]:
            if state["stop"]:
                break
            between_steps(step, args, state)
            if step["parallel"] and len(step["suites"]) > 1:
                run_pool(step, plan, args, state, report_dir, where)
            else:
                run_checked(step["suites"][0], plan, args, state, report_dir, where)
    except KeyboardInterrupt:
        kill_running()
        state["stop"] = "interrupted (Ctrl-C)"
        say(c("\n" + state["stop"], "yellow"))
    return state


def tier_meta(plan):
    if not plan["tier"]:
        return None
    return {"name": plan["tier"], "file": rel(TIERS_FILE), "left_out": plan["left_out"],
            "estimate_s": round(plan["est_total"]), "steps": [{"parallel": st["parallel"], "suites": [s["id"] for s in st["suites"]]}
                                                             for st in plan["steps"]]}


def cmd_run(args):
    plan = build_plan(args)
    where = resolve_pytest_where(args, plan["suites"])
    report_dir = new_run_dir(args, label="" if args.layer == "all" else args.layer)
    print_plan(plan, args, report_dir)
    if args.dry_run:
        print_tier_overview(plan, args)
        print_dry_details(plan, args, report_dir, where)
        return 0
    acquire_lock()
    started, t0 = now_iso(), time.time()
    try:
        state = execute(plan, args, report_dir, where)
    finally:
        release_lock()
    records = [state["records"].get(s["id"]) or {"id": s["id"], "layer": s["layer"], "status": "NOT_RUN",
                                                 "counts": {k: 0 for k in STATUSES}, "cases": [], "error": None}
               for s in plan["suites"]]
    meta = {"mode": "run", "layer": args.layer, "started": started, "finished": now_iso(), "seconds": round(time.time() - t0, 1),
            "argv": sys.argv[1:], "where": where, "stop_reason": state["stop"], "jobs": args.jobs, "tier": tier_meta(plan)}
    summary = write_summary(report_dir, records, meta)
    return 130 if state["stop"] == "interrupted (Ctrl-C)" else summary["exit_code"]


# ── tiers: overview and static check of system_tests/TIERS.json ──

def tier_check_problems(data, all_suites):
    """Static problems: unknown suites, pytest suites cut by case, patterns that select no known case, a tier that
    is not a superset of the tiers it includes, suites without a parallel-safety entry."""
    problems, warnings = [], []
    timings = load_timings(default_timing_dirs())
    resolved = {}
    for name in data["tiers"]:
        try:
            resolved[name] = tierlib.resolve(data, name, all_suites)
        except tierlib.TierError as exc:
            problems.append(str(exc))
    by_id = {s["id"]: s for s in all_suites}
    for name, sel in resolved.items():
        problems += _dead_patterns(name, sel, by_id, timings)
        for inc in data["tiers"][name].get("include", []):
            problems += _superset_problems(name, inc, sel, resolved.get(inc, {}), by_id, timings)
    for s in all_suites:
        if (data.get("suites") or {}).get(s["id"]) is None and tierlib.safety(data, s["id"])["reason"].startswith("not classified"):
            warnings.append(f"{s['id']}: no parallel-safety entry in 'suites' (runs serial)")
    return problems, warnings


def _dead_patterns(name, sel, by_id, timings):
    out = []
    for sid, entry in sel.items():
        if by_id[sid].get("kind") == "pytest" and entry.get("cases"):
            out.append(f"tier {name} / {sid}: pytest suites cannot be cut by case ids")
            continue
        catalog = case_catalog(by_id[sid], timings.get(sid))
        for p in entry.get("cases") or []:
            if catalog and not any(fnmatch.fnmatchcase(cid, p) for cid in catalog):
                out.append(f"tier {name} / {sid}: pattern {p!r} selects none of the {len(catalog)} known case ids")
    return out


def _superset_problems(name, inc, sel, inc_sel, by_id, timings):
    out = []
    for sid, entry in inc_sel.items():
        if sid not in sel:
            out.append(f"tier {name} includes {inc} but leaves out suite {sid}")
            continue
        catalog = case_catalog(by_id[sid], timings.get(sid))
        mine, _ = tierlib.split_cases(catalog, sel[sid].get("cases"))
        theirs, _ = tierlib.split_cases(catalog, entry.get("cases"))
        missing = sorted(set(theirs) - set(mine))
        if missing:
            out.append(f"tier {name} / {sid}: misses {len(missing)} case(s) of {inc}: {', '.join(missing[:5])}")
    return out


def cmd_tiers(args):
    data = load_tiers(required=True)
    all_suites = discover(load_registry())
    run_args = argparse.Namespace(layer="full", suite=None, skip=args.skip, timings=args.timings, jobs=args.jobs)
    plan = {"data": data, "all_suites": all_suites, "timings": load_timings(list(args.timings or []) + default_timing_dirs())}
    print_tier_overview(plan, run_args)
    if not args.check:
        return 0
    problems, warnings = tier_check_problems(data, all_suites)
    for w in warnings:
        say(c("  warning: ", "yellow") + w)
    for p in problems:
        say(c("  problem: ", "red") + p)
    say(f"tiers --check: {len(problems)} problem(s), {len(warnings)} warning(s) in {rel(TIERS_FILE)}")
    return 1 if problems else 0


# ════════════════════════════════════════════════════════════════════════
#  Aggregation: summary.json / summary.md / latest / comparison
# ════════════════════════════════════════════════════════════════════════

def previous_run(report_dir):
    """The newest other run, among reports/test-results/* and the siblings of a custom --report-dir."""
    cur = Path(report_dir).resolve()
    candidates = {}
    for base in {RESULTS_DIR.resolve(), cur.parent}:
        if base.is_dir():
            for p in base.iterdir():
                if p.is_dir() and not p.is_symlink() and (p / "summary.json").exists() and p.resolve() != cur:
                    candidates[p.resolve()] = p
    runs = sorted(candidates.values(), key=lambda p: (load_json(p / "summary.json", {}) or {}).get("finished") or p.name)
    return runs[-1] if runs else None


def compare(records, prev_summary):
    out = {"previous": None, "regressions": [], "fixed": [], "newly_blocked": [], "unblocked": [], "new_failures": [],
           "suites_compared": []}
    if not prev_summary:
        return out
    out["previous"] = prev_summary.get("run_id")
    prev = {s["id"]: s for s in prev_summary.get("suites", [])}
    for r in records:
        p = prev.get(r["id"])
        if not p or not r.get("cases") or not p.get("cases"):
            continue
        out["suites_compared"].append(r["id"])
        before = {cs["id"]: cs["status"] for cs in p["cases"]}
        for cs in r["cases"]:
            b, a = before.get(cs["id"]), cs["status"]
            item = {"suite": r["id"], "id": cs["id"], "title": cs.get("title", ""), "before": b, "after": a}
            if b is None:
                if a == "FAIL":
                    out["new_failures"].append(item)
            elif b != a:
                if a == "FAIL":
                    out["regressions"].append(item)
                elif b == "FAIL" and a == "PASS":
                    out["fixed"].append(item)
                elif a == "BLOCKED":
                    out["newly_blocked"].append(item)
                elif b == "BLOCKED":
                    out["unblocked"].append(item)
    return out


def write_summary(report_dir, records, meta):
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    prev_dir = previous_run(report_dir)
    prev_summary = load_json(prev_dir / "summary.json") if prev_dir else None
    cmp_ = compare(records, prev_summary)
    layers = {}
    for r in records:
        L = layers.setdefault(r["layer"], {"suites": 0, "PASS": 0, "FAIL": 0, "BLOCKED": 0, "suite_status": {}})
        L["suites"] += 1
        for k in STATUSES:
            L[k] += r["counts"].get(k, 0)
        L["suite_status"][r["status"]] = L["suite_status"].get(r["status"], 0) + 1
    totals = {k: sum(r["counts"].get(k, 0) for r in records) for k in STATUSES}
    failing = any(r["status"] in ("FAIL", "ERROR") for r in records)
    skipped = any(r["status"] in ("SKIPPED", "NOT_RUN") for r in records)
    exit_code = 1 if failing else (2 if skipped and meta.get("mode") == "run" else 0)
    overall = "FAIL" if failing else ("INCOMPLETE" if skipped else "PASS")
    summary = {"run_id": report_dir.name, "overall": overall, "exit_code": exit_code, **meta,
               "host": socket.gethostname(), "git": {"backend": git_info(ROOT), "frontend": git_info(CFG["FRONTEND_DIR"])},
               "config": {k: CFG[k] for k in ("API_URL", "WEB_URL", "TEST_DB", "PY_IMAGE")},
               "totals": totals, "layers": layers, "comparison": cmp_, "suites": records}
    write_json(report_dir / "summary.json", summary)
    (report_dir / "summary.md").write_text(render_md(summary) + render_tier_md(summary), encoding="utf-8")
    if str(report_dir.resolve()).startswith(str(RESULTS_DIR.resolve()) + os.sep):
        update_latest(report_dir)
    say("")
    say(render_console(summary, report_dir))
    return summary


def update_latest(report_dir):
    latest = RESULTS_DIR / "latest"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(report_dir.name, target_is_directory=True)
    except OSError:
        pass
    (RESULTS_DIR / "LATEST").write_text(report_dir.name + "\n", encoding="utf-8")


def _md(text, n=220):
    return str(text or "").replace("|", "\\|").replace("\n", " ")[:n]


def render_md(s):
    L = [f"# Test run {s['run_id']}", ""]
    g = s["git"].get("backend") or {}
    L.append(f"- **Result:** {s['overall']} (exit code {s['exit_code']})" + (f" - {s['stop_reason']}" if s.get("stop_reason") else ""))
    L.append(f"- **Mode:** {s.get('mode')} `{' '.join(s.get('argv') or [])}`" + (f", pytest on {s['where']}" if s.get("where") else ""))
    L.append(f"- **When:** {s.get('started')} to {s.get('finished')} ({fmt_secs(s.get('seconds'))}) on {s.get('host')}")
    if g:
        L.append(f"- **Backend:** {g.get('commit')} ({g.get('branch')}){' + %d uncommitted file(s)' % g['dirty_files'] if g.get('dirty_files') else ''}")
    fg = s["git"].get("frontend") or {}
    if fg:
        L.append(f"- **Frontend:** {fg.get('commit')} ({fg.get('branch')})")
    L += ["", "## By layer", "", "| Layer | Suites | PASS | FAIL | BLOCKED | Suite outcomes |", "|---|---|---|---|---|---|"]
    for layer in LAYERS:
        x = s["layers"].get(layer)
        if x:
            L.append(f"| {layer} | {x['suites']} | {x['PASS']} | {x['FAIL']} | {x['BLOCKED']} | "
                     + ", ".join(f"{k} {v}" for k, v in sorted(x["suite_status"].items())) + " |")
    t = s["totals"]
    L.append(f"| **total** | {len(s['suites'])} | {t['PASS']} | {t['FAIL']} | {t['BLOCKED']} | |")
    L += ["", "## Suites", "", "| Suite | Layer | Status | PASS | FAIL | BLOCKED | Time | Log / note |", "|---|---|---|---|---|---|---|---|"]
    for r in s["suites"]:
        note = r.get("error") or ("; ".join(f"missing {x['prereq']}" for x in r.get("skipped", [])) if r.get("skipped") else "") or r.get("log", "")
        L.append(f"| {r['id']} | {r['layer']} | {r['status']} | {r['counts']['PASS']} | {r['counts']['FAIL']} | "
                 f"{r['counts']['BLOCKED']} | {fmt_secs(r.get('seconds'))} | {_md(note, 160)} |")
    fails = [(r["id"], cs) for r in s["suites"] for cs in r.get("cases", []) if cs["status"] == "FAIL"]
    L += ["", f"## Failures ({len(fails)})", ""]
    if fails:
        L += ["| Suite | Case | Title | Category | Detail |", "|---|---|---|---|---|"]
        for sid, cs in fails:
            L.append(f"| {sid} | {_md(cs['id'], 120)} | {_md(cs.get('title'), 120)} | {cs.get('category', '')} | {_md(cs.get('detail'))} |")
    else:
        L.append("None.")
    errs = [r for r in s["suites"] if r["status"] == "ERROR"]
    if errs:
        L += ["", "## Suite errors", ""] + [f"- **{r['id']}**: {r.get('error')}" for r in errs]
    skipped = [r for r in s["suites"] if r["status"] == "SKIPPED"]
    if skipped:
        L += ["", "## Skipped (missing prerequisites)", ""]
        for r in skipped:
            for x in r.get("skipped", []):
                L.append(f"- **{r['id']}** - {x['prereq']}: {x['message']}" + (f"  \n  fix: `{x['fix']}`" if x.get("fix") else ""))
    blocked = [(r["id"], cs) for r in s["suites"] for cs in r.get("cases", []) if cs["status"] == "BLOCKED"]
    if blocked:
        L += ["", f"## Blocked cases ({len(blocked)})", "", "| Suite | Case | Title | Detail |", "|---|---|---|---|"]
        for sid, cs in blocked[:150]:
            L.append(f"| {sid} | {_md(cs['id'], 120)} | {_md(cs.get('title'), 120)} | {_md(cs.get('detail'), 160)} |")
        if len(blocked) > 150:
            L.append(f"| ... | {len(blocked) - 150} more in summary.json | | |")
    cmp_ = s["comparison"]
    L += ["", "## Compared with the previous run", ""]
    if not cmp_.get("previous"):
        L.append("No previous run in reports/test-results/.")
    else:
        L.append(f"Previous run: `{cmp_['previous']}` (suites compared: {', '.join(cmp_['suites_compared']) or 'none'})")
        for key, title in (("regressions", "Regressions (now FAIL)"), ("new_failures", "New failing cases"),
                           ("fixed", "Fixed (FAIL -> PASS)"), ("newly_blocked", "Newly BLOCKED"), ("unblocked", "No longer BLOCKED")):
            items = cmp_.get(key) or []
            L += ["", f"### {title}: {len(items)}"]
            for it in items[:200]:
                L.append(f"- {it['suite']} `{it['id']}` {it['before'] or 'new'} -> {it['after']}: {_md(it.get('title'), 120)}")
    return "\n".join(L) + "\n"


def render_tier_md(s):
    """The tier section of summary.md: what the tier ran per suite and which known cases it left out."""
    t = s.get("tier")
    if not t:
        return ""
    L = ["", f"## Tier {t['name']}", "", f"Selection from `{t.get('file')}`; estimated {fmt_secs(t.get('estimate_s'))} "
         f"with --jobs {s.get('jobs', 1)}. Excluded cases still run in the full tier.", "",
         "| Suite | Cases run | Known cases | Excluded | Patterns |", "|---|---|---|---|---|"]
    for r in s["suites"]:
        x = r.get("tier")
        n = sum(r["counts"].values())
        L.append(f"| {r['id']} | {n} | {x['known_cases'] if x else 'all'} | {len(x['excluded']) if x else 0} | "
                 + (_md(", ".join(x["patterns"]), 200) if x else "whole suite") + " |")
    if t.get("left_out"):
        L += ["", f"Suites not in this tier ({len(t['left_out'])}): {', '.join(t['left_out'])}"]
    for r in s["suites"]:
        ex = (r.get("tier") or {}).get("excluded") or []
        if ex:
            L += ["", f"<details><summary>{r['id']}: {len(ex)} excluded case(s)</summary>", "",
                  ", ".join(ex[:400]) + (f" ... (+{len(ex) - 400} more in summary.json)" if len(ex) > 400 else ""), "", "</details>"]
    return "\n".join(L) + "\n"


def render_console(s, report_dir):
    out = [c(f"Result: {s['overall']}", "green" if s["overall"] == "PASS" else ("red" if s["overall"] == "FAIL" else "yellow"))
           + f"   PASS {s['totals']['PASS']}  FAIL {s['totals']['FAIL']}  BLOCKED {s['totals']['BLOCKED']}"]
    for r in s["suites"]:
        out.append(f"  {r['status']:8} {r['id']:28} P{r['counts']['PASS']:<5} F{r['counts']['FAIL']:<4} B{r['counts']['BLOCKED']:<4}"
                   + (f" {r['error'][:90]}" if r.get("error") else ""))
    cmp_ = s["comparison"]
    if cmp_.get("previous"):
        out.append(f"vs {cmp_['previous']}: {len(cmp_['regressions'])} regression(s), {len(cmp_['new_failures'])} new failure(s), "
                   f"{len(cmp_['fixed'])} fixed")
    out.append(f"Report: {rel(Path(report_dir) / 'summary.md')}")
    return "\n".join(out)


def cmd_report(args):
    d = Path(args.run_dir) if args.run_dir else (RESULTS_DIR / "latest")
    d = d.resolve()
    data = load_json(d / "summary.json")
    if not data:
        die(f"no summary.json in {d}")
    meta = {k: data.get(k) for k in ("mode", "layer", "started", "finished", "seconds", "argv", "where", "stop_reason", "jobs",
                                     "tier")}
    s = write_summary(d, data["suites"], meta)
    return s["exit_code"]


def cmd_collect(args):
    """Snapshot the results files the suites last wrote (no tests are run)."""
    reg = load_registry()
    suites = ordered([s for s in discover(reg) if args.layer in (None, "all") or s["layer"] == args.layer])
    report_dir = new_run_dir(args, label="collected")
    records = []
    for s in suites:
        rec = {"id": s["id"], "layer": s["layer"], "status": "NOT_RUN", "counts": {k: 0 for k in STATUSES}, "cases": [],
               "error": None, "seconds": None}
        for p in result_paths(s, report_dir) if not s.get("result", {}).get("written_by_runner") else []:
            if p.exists():
                try:
                    cases, secs, _ = PARSERS[s["result"].get("format", "results_json")](p)
                except Exception as exc:  # noqa: BLE001
                    rec.update(status="ERROR", error=f"cannot read {rel(p)}: {exc}")
                    break
                raw = report_dir / "raw" / f"{s['id']}.json"
                raw.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, raw)
                rec.update(cases=cases, seconds=secs, result_file=rel(p), raw=rel(raw),
                           result_written=dt.datetime.fromtimestamp(p.stat().st_mtime).astimezone().isoformat(timespec="seconds"))
                rec["counts"] = normalize_counts(cases)
                rec["status"] = suite_status(cases, None)
                break
        records.append(rec)
    meta = {"mode": "collected", "layer": args.layer or "all", "started": now_iso(), "finished": now_iso(), "seconds": 0,
            "argv": sys.argv[1:], "where": None, "stop_reason": "collected: no tests were run; results as each suite last wrote them"}
    return 0 if write_summary(report_dir, records, meta)["overall"] != "FAIL" else 1


# ════════════════════════════════════════════════════════════════════════
#  list
# ════════════════════════════════════════════════════════════════════════

def cmd_list(args):
    reg = load_registry()
    suites = ordered(discover(reg))
    rows = []
    for s in suites:
        if args.layer and s["layer"] != args.layer:
            continue
        files = suite_files(reg, s)
        if s.get("kind") == "pytest":
            exclude = [str(ROOT / x) for x in s.get("exclude_paths", [])]
            files = [f for f in files if not any(f.startswith(x + os.sep) for x in exclude)]
            n = sum(count_pytest_functions(f) for f in files)
            cases = f"{n} test functions in {len(files)} files"
            last = None
        else:
            last = last_results(s)
            cases = f"{last['cases']} cases (last results)" if last else "no results yet"
        rows.append({"id": s["id"], "layer": s["layer"], "kind": s.get("kind"), "discovered": s.get("discovered", False),
                     "cwd": s.get("cwd"), "command": s.get("command") or ["python", "-m", "pytest"] + s.get("pytest_args", []),
                     "cases": cases, "duration_s": s.get("duration_s"), "prereqs": s.get("prereqs", []),
                     "last": last, "only": (s.get("only") or {}).get("syntax"), "notes": s.get("notes", ""),
                     "files": [rel(f) for f in files]})
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0
    for layer in LAYERS:
        lrows = [r for r in rows if r["layer"] == layer]
        if not lrows:
            continue
        info = reg.get("layers", {}).get(layer, {})
        say(c(f"\n{info.get('title', layer).upper()}  ({layer})", "bold") + c(f"  {info.get('proves', '')}", "dim"))
        for r in lrows:
            when = ""
            if r["last"]:
                cts = r["last"]["counts"]
                when = (f"  last: P{cts['PASS']} F{cts['FAIL']} B{cts['BLOCKED']} "
                        f"@ {dt.datetime.fromtimestamp(r['last']['mtime']):%Y-%m-%d %H:%M}")
            say(f"  {r['id']:28} ~{fmt_secs(r['duration_s']):>6}  {r['cases']}{when}" + (c("  [discovered]", "yellow") if r["discovered"] else ""))
            if r["kind"] == "pytest" and args.verbose:
                for f in r["files"]:
                    say(c(f"      {f} ({count_pytest_functions(ROOT / f)})", "dim"))
            if args.verbose:
                say(c(f"      $ (cd {r['cwd']}) {' '.join(r['command'])}", "dim"))
                if r["only"]:
                    say(c(f"      --only: {r['only']}", "dim"))
    say(c("\nRun a layer: python3 scripts/test.py run <layer>   (details: list -v, plan: run <layer> --dry-run)", "dim"))
    return 0


# ════════════════════════════════════════════════════════════════════════
#  trace: BRD requirement -> test case traceability matrix
# ════════════════════════════════════════════════════════════════════════

CASE_ID = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z]+)*)-(\d+)([a-z]?)(?:\s*(?:\.\.|–|-)\s*(\d+))?\b")
BR_REF = re.compile(r"BR-([A-Z]{2,4})-(\d{2,3})((?:\s*(?:/|\.\.|,\s*(?=\d))\s*\d{2,3})*)")
SECTION_REF = re.compile(r"§\s?(\d+(?:\.\d+)?)")


def expand_case_ids(cell):
    m = CASE_ID.match(cell.strip().strip("`*"))
    if not m:
        return []
    prefix, start, suffix, end = m.group(1), m.group(2), m.group(3), m.group(4)
    if end and not suffix:
        width = len(start)
        a, b = int(start), int(end if len(end) >= width else start[:width - len(end)] + end)
        if 0 <= b - a <= 200:
            return [f"{prefix}-{n:0{width}d}" for n in range(a, b + 1)]
    return [f"{prefix}-{start}{suffix}"]


def expand_br(text):
    refs = []
    for m in BR_REF.finditer(text or ""):
        area, first, tail = m.group(1), m.group(2), m.group(3) or ""
        width = len(first)
        nums = [int(first)]
        for sep, num in re.findall(r"(/|\.\.|,)\s*(\d{2,3})", tail):
            if sep == "..":
                nums += list(range(nums[-1] + 1, int(num) + 1))
            else:
                nums.append(int(num))
        refs += [f"BR-{area}-{n:0{width}d}" for n in nums]
    return refs


def parse_catalogue(path):
    """Cases from the markdown tables of a TEST_CASES.md: {case_id: {"refs": [...], "sections": [...], "text": str}}."""
    cases = {}
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    header = None
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            header = None
            continue
        cells = [x.strip() for x in line.replace("\\|", "\u2758").strip().strip("|").split("|")]
        if re.fullmatch(r"[\s|:-]+", line.strip()):
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if re.fullmatch(r"\s*\|[\s|:-]+\|?\s*", nxt):
            header = [h.lower() for h in cells]
            continue
        if header is None or not header or "id" not in header[0]:
            continue
        ids = expand_case_ids(cells[0])
        if not ids:
            continue
        brd_cols = [j for j, h in enumerate(header) if "brd" in h]
        ref_text = " ".join(cells[j] for j in brd_cols if j < len(cells)) if brd_cols else " ".join(cells[1:])
        refs = expand_br(ref_text)
        sections = ["§" + x for x in SECTION_REF.findall(ref_text)]
        title = cells[1] if len(cells) > 1 and not brd_cols else (cells[-2] if len(cells) > 2 else "")
        for cid in ids:
            cases[cid] = {"refs": refs, "sections": sections, "text": _md(title.replace("\u2758", "|"), 140)}
    return cases


CODE_CASE_ID = re.compile(r"""["']((?:[A-Z][A-Z0-9]*-)+\d+[a-z]?)["']""")


def code_section_refs(path):
    """(BR ref, case id) pairs from a suite script: a comment line naming BR ids opens a section, and every
    case id literal recorded below it (until the next such comment) covers those requirements."""
    pairs, current = [], []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "BR-" in stripped:
            current = expand_br(stripped)
            continue
        if current:
            for cid in CODE_CASE_ID.findall(line):
                if not cid.startswith("BR-"):
                    pairs += [(r, cid) for r in current]
    return pairs


REQUIREMENTS_FILE = ROOT / "docs" / "REQUIREMENTS.md"
IN_SCOPE_STATUSES = {"fixed", "decision", "change request"}   # "Out of release" needs no case


def requirements_register(path):
    """{rid: (title, status)} from the table of docs/REQUIREMENTS.md (columns ID | ... | Requirement | ... | Status)."""
    out, header = {}, None
    if not path or not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [x.strip() for x in line.strip().strip("|").split("|")]
        if header is None:
            header = [h.lower() for h in cells]
            continue
        if "status" in header and re.fullmatch(r"BR-[A-Z]{2,4}-\d{2,3}", cells[0]) and len(cells) == len(header):
            title = cells[header.index("requirement")] if "requirement" in header else ""
            out[cells[0]] = (title, cells[header.index("status")])
    return out


def brd_requirements(path):
    reqs = {}
    if not path or not Path(path).exists():
        return reqs
    register = requirements_register(path)
    if register:
        return {rid: _md(title, 90) for rid, (title, _status) in register.items()}
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        for m in re.finditer(r"\bBR-[A-Z]{2,4}-\d{2,3}\b", line):
            rid = m.group(0)
            if rid not in reqs:
                rest = re.sub(r"\s{2,}", " ", line[m.end():]).strip(" |:-")
                rest = re.sub(r"^[\s*:]*\([MSC]\)[\s*:]*", "", rest)
                reqs[rid] = _md(rest, 90)
    return reqs


def current_case_statuses(reg, from_run):
    """{suite_id: {case_id: case}} from the suites' own results files, overlaid with a run's summary.json."""
    out = {}
    for s in discover(reg):
        if s.get("kind") == "pytest" or s.get("result", {}).get("written_by_runner"):
            continue
        for p in result_paths(s):
            if p.exists():
                try:
                    out[s["id"]] = {cs["id"]: cs for cs in PARSERS[s["result"].get("format", "results_json")](p)[0]}
                except Exception:  # noqa: BLE001
                    pass
                break
    run_dir = Path(from_run) if from_run else (RESULTS_DIR / "latest")
    data = load_json(run_dir / "summary.json") if run_dir.exists() else None
    for r in (data or {}).get("suites", []):
        if r.get("cases"):
            out[r["id"]] = {cs["id"]: cs for cs in r["cases"]}
    return out, (rel(run_dir) if data else None)


def trace_check(path, req_cases):
    """Traceability gate (CI): every in-scope requirement in the register has at least one test case."""
    register = requirements_register(path)
    if not register:
        say(f"trace --check: no requirements register with a Status column at {path or REQUIREMENTS_FILE}")
        return 2
    bad_status = {r: s for r, (_t, s) in register.items() if s.lower() not in IN_SCOPE_STATUSES | {"out of release"}}
    in_scope = [r for r, (_t, s) in register.items() if s.lower() in IN_SCOPE_STATUSES]
    uncovered = [r for r in in_scope if not req_cases.get(r)]
    unknown = sorted(r for r in req_cases if r.startswith("BR-") and r not in register)
    for r, s in sorted(bad_status.items()):
        say(f"UNKNOWN STATUS  {r}: {s!r} (use Fixed, Decision, Change request or Out of release)")
    for r in uncovered:
        say(f"NO CASE         {r} ({register[r][1]}): {register[r][0][:90]}")
    for r in unknown:
        say(f"NOT IN REGISTER {r}: referenced by {', '.join(req_cases[r][:5])} (add it to {rel(Path(path))})")
    out_of_release = len(register) - len(in_scope) - len(bad_status)
    say(f"traceability: {len(register)} requirements, {len(in_scope)} in scope, {out_of_release} out of release; "
        f"{len(in_scope) - len(uncovered)} in scope with a case, {len(uncovered)} without, "
        f"{len(unknown)} referenced but not registered")
    return 1 if uncovered or bad_status or unknown else 0


def requirement_case_pairs(suites):
    """{(requirement, suite id, case id)} from the case catalogues and the BR comments in suite scripts (the static
    sources `trace --check` uses), so a tier's coverage can be counted per suite and case."""
    pairs, explicit = set(), set()
    for path in sorted(glob.glob(str(ROOT / "system_tests" / "*" / "TEST_CASES.md"))):
        d = Path(path).parent.name
        sid = next((s["id"] for s in suites if s.get("cwd") == f"system_tests/{d}" or s["id"] == d), d)
        for cid, info in parse_catalogue(path).items():
            pairs.update((r, sid, cid) for r in info["refs"] + info["sections"])
            explicit.update([cid] if info["refs"] else [])
    for s in suites:
        if s.get("script", "").endswith(".py") and (ROOT / s["script"]).exists():
            pairs.update((r, s["id"], cid) for r, cid in code_section_refs(ROOT / s["script"]) if cid not in explicit)
    return pairs


def trace_tier_coverage(brd_path, suites):
    """How many in-scope requirements each tier exercises (a requirement counts when one of its cases is selected)."""
    register = requirements_register(brd_path)
    data = load_tiers(required=False)
    if not register or not data:
        return
    in_scope = {r for r, (_t, st) in register.items() if st.lower() in IN_SCOPE_STATUSES}
    pairs = requirement_case_pairs(suites)
    parts = []
    for name in tierlib.TIER_NAMES:
        try:
            sel = tierlib.resolve(data, name, suites)
        except tierlib.TierError as exc:
            parts.append(f"{name}: invalid ({exc})")
            continue
        covered = {r for r, sid, cid in pairs if r in in_scope and sid in sel
                   and (sel[sid].get("cases") is None or tierlib.matches(cid, sel[sid]["cases"]))}
        parts.append(f"{name} {len(covered)}")
    say(f"tiers: in-scope requirements with a case in the tier (of {len(in_scope)}): " + ", ".join(parts))


def cmd_trace(args):
    reg = load_registry()
    suites = {s["id"]: s for s in discover(reg)}
    # --check is static: case catalogues and suite scripts only, never run results
    statuses, run_used = ({}, None) if args.check else current_case_statuses(reg, args.from_run)
    brd_path = args.brd or env("TEST_BRD_PATH") or (str(REQUIREMENTS_FILE) if REQUIREMENTS_FILE.exists() else "")
    reqs = brd_requirements(brd_path)
    catalogues = {}
    for path in sorted(glob.glob(str(ROOT / "system_tests" / "*" / "TEST_CASES.md"))):
        d = Path(path).parent.name
        sid = next((sid for sid, s in suites.items() if s.get("cwd") == f"system_tests/{d}" or sid == d), d)
        catalogues[sid] = {"path": rel(path), "cases": parse_catalogue(path)}
    # case -> status lookup across all suites
    all_status = {}
    for sid, cases in statuses.items():
        for cid, cs in cases.items():
            all_status.setdefault(cid, (sid, cs))
    req_cases = {}

    def add(req, cid):
        req_cases.setdefault(req, [])
        if cid not in req_cases[req]:
            req_cases[req].append(cid)
    for sid, cat in catalogues.items():
        for cid, info in cat["cases"].items():
            for r in info["refs"] + info["sections"]:
                add(r, cid)
    for sid, cases in statuses.items():                 # BR refs written in result titles, e.g. "(BR-CM-01)"
        for cid, cs in cases.items():
            for r in expand_br(cs.get("title", "")):
                add(r, cid)
    # Fallback for cases whose catalogue row names no BR id: section comments in the suite code,
    # e.g. "# ═══ BR-DF-09 security ═══" above the code that records OUT-001..009.
    explicit = {cid for cat in catalogues.values() for cid, info in cat["cases"].items() if info["refs"]}
    code_refs = {}
    for s in suites.values():
        if s.get("script", "").endswith(".py") and (ROOT / s["script"]).exists():
            for r, cid in code_section_refs(ROOT / s["script"]):
                if cid not in explicit:
                    add(r, cid)
                    code_refs.setdefault(cid, [])
                    if r not in code_refs[cid]:
                        code_refs[cid].append(r)
    if args.check:
        rc = trace_check(brd_path, req_cases)
        trace_tier_coverage(brd_path, list(suites.values()))
        return rc

    def st(cid):
        return all_status[cid][1]["status"] if cid in all_status else "no result"

    def summary_of(cids):
        n = {}
        for cid in cids:
            n[st(cid)] = n.get(st(cid), 0) + 1
        return ", ".join(f"{v} {k}" for k, v in sorted(n.items()))
    br_keys = sorted({*reqs, *(r for r in req_cases if r.startswith("BR-"))})
    sec_keys = sorted((r for r in req_cases if r.startswith("§")), key=lambda x: [int(p) for p in re.findall(r"\d+", x)])
    uncovered = [r for r in br_keys if not req_cases.get(r)]
    failing = [r for r in br_keys if any(st(cid) == "FAIL" for cid in req_cases.get(r, []))]
    L = ["# Traceability matrix", "",
         f"Generated by `python3 scripts/test.py trace` on {dt.date.today().isoformat()}. Do not edit by hand.", "",
         "Sources:",
         f"- BRD requirement list: {'`' + rel(brd_path) + '`' if reqs else 'not provided (pass --brd FILE or set TEST_BRD_PATH); only requirements referenced by a case are listed'}",
         "- Case catalogues: " + ", ".join(f"`{c_['path']}`" for c_ in catalogues.values()),
         "- Latest statuses: each suite's results.json" + (f", overlaid with `{run_used}/summary.json`" if run_used else ""),
         "- BRD references inside result titles (e.g. \"(BR-CM-01)\") also count as coverage.", "",
         "## Summary", "",
         f"- BRD requirements: **{len(br_keys)}**; covered by at least one case: **{len(br_keys) - len(uncovered)}**; "
         f"**no case: {len(uncovered)}**; with a failing case: **{len(failing)}**"]
    for sid, cat in catalogues.items():
        ids = list(cat["cases"])
        with_res = [i for i in ids if i in all_status]
        n = {}
        for i in with_res:
            n[st(i)] = n.get(st(i), 0) + 1
        L.append(f"- `{sid}` catalogue: {len(ids)} cases, {len(with_res)} with a result ("
                 + ", ".join(f"{v} {k}" for k, v in sorted(n.items())) + f"), {len(ids) - len(with_res)} without")
    catalogued = {cid for cat in catalogues.values() for cid in cat["cases"]}
    extra = {}
    for sid, cases in statuses.items():
        for cid, cs in cases.items():
            if cid not in catalogued:
                extra.setdefault(sid, []).append(cs)
    L.append("- Cases with results but no catalogue entry: " + ", ".join(f"`{sid}` {len(v)}" for sid, v in sorted(extra.items())))
    L += ["", "## Requirements without a test case", ""]
    if uncovered:
        L += ["| Requirement | BRD text |", "|---|---|"] + [f"| {r} | {reqs.get(r, '')} |" for r in uncovered]
    else:
        L.append("None" + ("." if reqs else " among the requirements referenced by cases (no BRD list given)."))
    L += ["", "## Requirements with a failing case", ""]
    if failing:
        L += ["| Requirement | BRD text | Failing cases |", "|---|---|---|"]
        L += [f"| {r} | {reqs.get(r, '')} | {', '.join(cid for cid in req_cases[r] if st(cid) == 'FAIL')} |" for r in failing]
    else:
        L.append("None.")
    L += ["", "## Requirement -> cases", "", "| Requirement | BRD text | Cases | Latest statuses |", "|---|---|---|---|"]
    for r in br_keys + sec_keys:
        cids = req_cases.get(r, [])
        shown = ", ".join(cids[:25]) + (f" (+{len(cids) - 25})" if len(cids) > 25 else "")
        L.append(f"| {r} | {reqs.get(r, '')} | {shown or '**none**'} | {summary_of(cids) if cids else 'NO CASE'} |")
    for sid, cat in catalogues.items():
        L += ["", f"## Catalogue `{cat['path']}`", "", "| Case | BRD refs | Latest | Title / expected |", "|---|---|---|---|"]
        for cid, info in cat["cases"].items():
            refs = ", ".join(info["refs"] + info["sections"]) or (
                ", ".join(code_refs[cid]) + " (from code)" if code_refs.get(cid) else "-")
            L.append(f"| {cid} | {refs} | {st(cid)} | {info['text']} |")
    L += ["", "## Cases with results but no catalogue entry", "",
          "Suites without a TEST_CASES.md (new features, E2E journeys, UI suites, API sweep) trace through their case ids; "
          "the API sweep is summarised by endpoint group.", ""]
    for sid, cases in sorted(extra.items()):
        if len(cases) > 400:
            groups = {}
            for cs in cases:
                key = "-".join(cs["id"].split("-")[:3])
                g = groups.setdefault(key, {"PASS": 0, "FAIL": 0, "BLOCKED": 0})
                g[cs["status"]] = g.get(cs["status"], 0) + 1
            L += [f"### `{sid}` ({len(cases)} cases, {len(groups)} groups)", "", "| Group | PASS | FAIL | BLOCKED |", "|---|---|---|---|"]
            L += [f"| {k} | {v['PASS']} | {v['FAIL']} | {v['BLOCKED']} |" for k, v in sorted(groups.items())]
        else:
            L += [f"### `{sid}` ({len(cases)} cases)", "", "| Case | Status | Title |", "|---|---|---|"]
            L += [f"| {cs['id']} | {cs['status']} | {_md(cs.get('title'), 150)} |" for cs in cases]
        L.append("")
    out = Path(args.out) if args.out else ROOT / "docs" / "TRACEABILITY.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    say(f"wrote {rel(out)}: {len(br_keys)} BRD requirements ({len(uncovered)} without a case, {len(failing)} with a failing case), "
        f"{len(catalogued)} catalogued cases, {sum(len(v) for v in extra.values())} other cases")
    return 0


# ════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════

def add_plan_options(p):
    p.add_argument("--jobs", type=int, default=1, help="run up to N parallel-safe suites at once (system_tests/TIERS.json "
                                                        "'suites'); serial suites always run alone. Default 1")
    p.add_argument("--skip", action="append", help="suite id(s) / globs to leave out, e.g. --skip unit,functional")
    p.add_argument("--timings", action="append", help="run directory (or summary.json) whose per-suite times feed the "
                                                      "estimates; newest first, then reports/test-results/*")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="scripts/test.py", description="Outreach 360 test runner (docs/TESTING.md)",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="show the suites per layer with case counts")
    p.add_argument("--layer", choices=LAYERS)
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("run", help="run a tier (smoke, core, full = all) or a layer and write a report")
    p.add_argument("layer", choices=list(tierlib.TIER_NAMES) + ["all"] + LAYERS)
    p.add_argument("--suite", action="append", help="suite id(s), comma separated or repeated; glob patterns allowed")
    p.add_argument("--only", help="case filter passed to suites that support one (syntax per suite: list -v)")
    p.add_argument("--report-dir", help="write the report here instead of reports/test-results/<timestamp>")
    p.add_argument("--keep-going", action="store_true", help="continue after a suite fails or errors")
    p.add_argument("--dry-run", action="store_true", help="print the plan, commands and prerequisite status only")
    p.add_argument("--restart-api-between", action="store_true",
                   help="docker restart the API between live-stack suites (clears the in-memory rate limiter)")
    p.add_argument("--pause", type=int, default=0, help="seconds to wait between live-stack suites")
    p.add_argument("--where", choices=["auto", "host", "docker"], default="auto", help="where pytest layers run")
    p.add_argument("--keep-db", action="store_true", help="functional: do not recreate the throwaway database first")
    p.add_argument("--extra", help="extra arguments appended to every selected suite's command; use the = form: --extra=\"--no-ui --no-perf\"")
    p.add_argument("--force", action="store_true", help="run suites even when a hard prerequisite is missing")
    p.add_argument("-q", "--quiet", action="store_true", help="do not echo suite output (it is still in logs/)")
    add_plan_options(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("tiers", help="case counts and estimates of smoke / core / full; --check validates TIERS.json")
    p.add_argument("--check", action="store_true", help="fail on unknown suites, dead patterns or a tier that drops "
                                                        "cases of a tier it includes (static; runs nothing)")
    add_plan_options(p)
    p.set_defaults(fn=cmd_tiers)

    p = sub.add_parser("doctor", help="check the prerequisites of every layer (changes nothing)")
    p.add_argument("--layer", choices=LAYERS)
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("env", help="print (or --execute) the commands that bring the test stack up or down")
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--e2e", action="store_true", help="the e2e wiring: SES stand-in + vector-api:e2e + SEND_ON_ANY_DAY")
    p.add_argument("--build", action="store_true", help="(re)build images")
    p.add_argument("--execute", action="store_true", help="run the commands instead of printing them")
    p.set_defaults(fn=cmd_env)

    p = sub.add_parser("db", help="create / drop / reset the functional layer's throwaway MySQL schema")
    p.add_argument("action", choices=["create", "drop", "reset", "status"])
    p.set_defaults(fn=cmd_db)

    p = sub.add_parser("report", help="re-aggregate a run directory (summary.md/json, comparison)")
    p.add_argument("run_dir", nargs="?")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("collect", help="snapshot each suite's current results file into a report (no tests run)")
    p.add_argument("--layer", choices=LAYERS + ["all"])
    p.add_argument("--report-dir")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("trace", help="write docs/TRACEABILITY.md (BRD requirement -> case -> latest status)")
    p.add_argument("--brd", help="requirement list (default docs/REQUIREMENTS.md; any BRD markdown/text also works; "
                                 "or TEST_BRD_PATH)")
    p.add_argument("--check", action="store_true",
                   help="CI gate: fail when an in-scope requirement has no test case (static; writes nothing)")
    p.add_argument("--out", help="output file (default docs/TRACEABILITY.md)")
    p.add_argument("--from-run", help="run directory whose statuses overlay the suites' results files (default: latest)")
    p.set_defaults(fn=cmd_trace)

    args = ap.parse_args(argv)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        say(c("interrupted", "yellow"))
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
