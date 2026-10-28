# Testing Outreach 360

This page covers the four test layers, how to run each one with a single command, how to add a test to each layer, and how results are kept and compared between runs.

Everything runs through one runner, `scripts/test.py`. It uses only the Python 3 standard library. Suites are listed in `tests/registry.json`. New test files are picked up automatically when they follow the naming rules below.

```bash
python3 scripts/test.py list            # every suite per layer, with case counts and the last results
python3 scripts/test.py doctor          # check prerequisites per layer, and print the fix for each missing one
python3 scripts/test.py run smoke --jobs 4   # a tier: smoke | core | full (= all); see "Tiers" below
python3 scripts/test.py run unit        # or a layer: functional | system | e2e | ui
make test-unit                          # the same through make (test-functional, test-system, test-e2e, test-ui, test-all)
```

## 1. Test pyramid

| Layer | What it proves | Where | Needs | Typical time |
|---|---|---|---|---|
| **unit** | Pure logic is correct in isolation. Sockets and SQLAlchemy connections are blocked, so a unit test cannot quietly depend on a database or the network. | `tests/unit/test_*.py` | Python with the app's requirements, or docker | about 20 s |
| **functional** | The API behaves correctly in-process (FastAPI `TestClient`) against a real MySQL schema that the migrations build. It covers routing, validation, permissions, tenant isolation and persistence. | `tests/test_*.py` + `tests/conftest.py` | MySQL, a throwaway schema (`vtest`) | about 4 min |
| **system** | The running stack (api + worker + mysql + web) meets the BRD through its public API. Results are cross-checked with SQL, and some UI checks are included. Also covers the frontend/backend contract and the API sweep. | `system_tests/<suite>/run_*.py`, `system_tests/contract` | the docker stack | 1-45 min per suite |
| **e2e** | Whole journeys (J1-J6) work through the real worker, the SES stand-in (moto), SES webhooks and a fake IMAP mailbox, with no DB shortcuts. | `system_tests/e2e` | the stack with the e2e wiring | about 30 min |
| **ui** | Screens render and behave correctly in a real Chromium against the running stack, with DB cross-checks. | `system_tests/ui_outreach`, `system_tests/ui_crm`, `system_tests/ui_features/*.mjs` | the stack, Node 18+, Playwright | 3-15 min per suite |

Run them in this order. A failing unit or functional test is cheaper to diagnose than the same bug seen through a browser.

### What each layer contains (at the time of writing)

| Layer | Suites | Size |
|---|---|---|
| unit | `unit` | `python3 scripts/test.py list` counts the test functions per file. Parametrised cases expand at run time. |
| functional | `functional` | every `tests/test_*.py` |
| system | `contract`, `new_features`, `contacts`, `sales_pipeline`, `outreach`, `api_sweep`, discovered `system_tests/*/run_*.py` (e.g. `ab_testing`, `behaviour_fixes`, `contact_scoring`, `crm_kpis`, `discount_approval`, `webhooks`), `reports_platform` (always last) | about 6,300 recorded cases, 5,800 of them in the API sweep |
| e2e | `e2e` (J1-J6) | 188 cases |
| ui | `ui_outreach`, `ui_crm`, `ui_features.<name>` (one suite per `.mjs`) | about 360 cases + new features |

The unit layer starts with tests for e-mail parsing and reply extraction, the business calendar and send windows (with fixed datetimes), the OpenAI cost maths, reply-based unsubscribe detection, campaign-prospect status priority, fiscal-year maths, the rate limiter, TOTP (RFC 6238 vectors) and recovery codes, out-of-office detection, the start-up secret checks and the observability helpers.

## 2. Running each layer

| Layer | One command | What the runner does |
|---|---|---|
| unit | `python3 scripts/test.py run unit` | `pytest tests/unit` on the host when the app's dependencies are importable (it also picks up `./venv`). Otherwise it runs in a throwaway container from `TEST_PY_IMAGE` (default `vector-api:latest`). |
| functional | `python3 scripts/test.py run functional` | Recreates the throwaway schema (`db reset`, skip with `--keep-db`), then runs `pytest tests --ignore=tests/unit`. In docker mode this runs on the compose network. |
| system | `python3 scripts/test.py run system` | Each suite runs from its own folder, one at a time. `reports_platform` runs last. |
| e2e | `python3 scripts/test.py run e2e` | `python3 -m system_tests.e2e.run_journeys`: J1 in a thread next to J2-J6 (each journey has its own workspace; `--serial` runs them one after another). The log has a `timing <journey> <phase>: Ns` line per J1 phase and per journey. Bring the stand-ins up first (see section 5). |
| ui | `python3 scripts/test.py run ui` | Each Playwright suite runs with `node`, one after another. |
| all | `python3 scripts/test.py run all --keep-going` | The `full` tier: all layers, every case. `reports_platform` runs at the very end (unless the stack relaxes the per-IP limits, section 7). |

### Tiers: smoke, core, full

Live suites are grouped into three tiers. A tier is a selection of suites and, for suites it cuts down, of their case ids. It is written down, with a one-line reason per suite, in `system_tests/TIERS.json`. Nothing is ever deleted: a case left out of smoke and core still runs in full.

| Tier | When | Contents | Target |
|---|---|---|---|
| **smoke** | every deploy, including UAT | unit + functional; the contract check; the J1 journey (import -> campaign -> send -> reply -> deal -> report); ~25 key screens (login/session, home, contacts list + record, companies, lists, import, campaigns list / wizard / analytics / details + inbox tab, unified inbox, email accounts, domain health, deals, pipeline, leads, reports, team, platform admin); the security slice of the API sweep (401, 403 per role, tenant isolation, duplicate / idempotent writes) | <= 10 min |
| **core** | nightly, before a build goes to UAT | smoke + every system suite + every e2e journey + the UI suites cut to one case per screen / feature plus every security and permission case | ~25 min |
| **full** | release candidates, weekly | everything (`run all` is the same) | ~45 min |

```bash
python3 scripts/test.py run smoke --jobs 4 --keep-going     # also: core, full (= all)
python3 scripts/test.py run core --dry-run --jobs 4         # the plan: suites, cases per suite, serial / parallel steps, estimate
python3 scripts/test.py tiers                               # case counts and estimated time of every tier
python3 scripts/test.py tiers --check                       # validate TIERS.json (also a CI gate in job "unit")
```

How a tier reaches the suites: the runner sets `TEST_CASES` (comma-separated case-id patterns, fnmatch syntax `*`, `?`, `[0-9]`, `[!x]`) for every suite the tier cuts down. `system_tests/common.py` (`CaseFilter`, used by `Results`) and `system_tests/ui/ui_common.mjs` (`caseFilter`, used by `Recorder`, `S.t` and `tc`) never run or record an unselected case, and skip whole units whose ids cannot match: ui_outreach sub-suites, ui_crm areas, api_sweep phases (AUTH, ROLE, HIER, VAL, DUP, ISO), e2e journeys. Rows of a crash or setup failure (`*-CRASH`, `*-EXC`, `*-SETUP`) are always kept. A cut run carries the unselected rows of the previous `results.json` over (marked `carried_over`), so the file keeps listing the whole suite; the runner reports only the fresh, selected rows. The summary gets a "Tier" section: cases run per suite, cases known, the excluded case ids, and the suites left out of the tier.

`--dry-run` estimates each suite from the newest earlier run in `reports/test-results/` where it finished (`--timings RUN_DIR` puts another run first), scaled by the share of its cases the tier selects, and simulates the parallel pool. Setup time of a suite is not scaled down, so estimates for heavily cut UI suites are optimistic.

#### Adding a case or a suite to a tier

1. Find the case id (`results.json` of the suite, or its catalogue).
2. Add the id or a pattern to the suite's `cases` list in `system_tests/TIERS.json` under the tier (`smoke` or `core`; `core` includes everything in `smoke`, so a smoke case is never listed twice). A suite with no `cases` key runs whole. Keys may be a suite id, a glob (`ui_features.*`) or `layer:<layer>` (`core` lists `layer:system`, so a new system suite is in core automatically; a new UI suite is only in full until someone adds it).
3. UI suites are written as flows: a case often uses the page or the data an earlier case in the same block left behind. Select the first case of a screen block, or the whole chain up to the case you want (`UIO-AUTH-3?`, `UIC-CON-2[3-5]`), never a mid-chain case alone.
4. Run `python3 scripts/test.py tiers --check` (fails on unknown suites, patterns that select no known case, a tier that drops cases of a tier it includes, a pytest suite with `cases`) and `run <tier> --dry-run` to see the new estimate.

#### Parallel runs (`--jobs N`)

`--jobs N` runs up to N parallel-safe suites at once (3-4 suits the UI suites); the run lock still allows one run at a time. The plan is one parallel pool (every parallel-safe suite, the longest estimated first so the long pole, usually e2e, starts at once), then the serial suites one at a time, then `reports_platform`. Each suite keeps its own log (`logs/<suite>.log`) and results files, so nothing collides; console output of pooled suites goes to their logs only.

The parallel-safety rule, recorded per suite under `suites` in `TIERS.json` with its reason:

- **serial** (`parallel: false`, runs alone): the suite changes state other suites depend on (the global scheduler flag, the CRM purge across all tenants), locks the shared client IP, or measures latency (load tests, the 10k import). Today: contacts, reports_platform, ui_features.platform_admin. A suite not listed is serial until reviewed.
- **parallel with a lock**: suites holding the same lock never overlap. The `worker` lock is defined but held by no suite since the tune run of 2026-10-28: outreach, sales_pipeline, webhooks, e2e and ui_crm ran together in one core pool with no failure. Their job passes go through `app.jobs.run_once` (the worker loop's own lock, so never two passes of a job at once) and are idempotent per tenant; the scheduler sends every inbox lane in parallel without sleeping, so other suites' sends do not delay a suite's own. Give the lock back to a suite whose assertions depend on job passes it did not trigger itself.
- **parallel**: everything else (own workspaces, own data, own receiver ports).
- A tier entry may override it (`"parallel": true` with a reason) when its cut leaves out the cases that touch shared state, e.g. smoke runs three platform_admin cases without the scheduler switch, and core runs contacts in the pool because it leaves out the 10k-row import / latency cases (`--no-perf`).
- core passes `--no-perf` to contacts and `--skip-load` to reports_platform (args in `TIERS.json`): latency and load measurements run in full only, where those suites run alone.

Without `RATE_LIMIT_TEST_MULTIPLIER` on the stack (section 7) parallel suites share the default per-IP limits; the runner warns, and `--restart-api-between` / `--pause` only act between steps, never during a pool.

### Useful options

- `--suite contacts,ui_crm` selects suites by id. Glob patterns work, e.g. `--suite 'ui_features.*'`.
- `--only X` passes a case filter to suites that support one. The syntax depends on the suite (`list -v` shows it):
  - pytest: a `-k` expression
  - contacts: comma-separated id prefixes (`CM-00,CM-04`)
  - e2e: a journey list (`J2,J3`)
  - ui_outreach: a regex over case ids (results merge into the previous file)
  - ui_crm: a list of areas
- `--extra=--no-perf` appends arguments to the command of every selected suite. Use the `=` form, because the value starts with `--`. For example, `--suite contacts --extra=--no-ui`.
- `--dry-run` prints the plan, every command, its working directory, where results will appear and the prerequisite status, and runs nothing.
- `--keep-going` continues after a suite fails. By default the run stops at the first suite with a FAIL or an ERROR.
- `--restart-api-between` runs `docker restart vector-api-1` between live-stack suites. This clears the in-memory rate limiter (see section 7). It is skipped (not needed) when the stack runs with `RATE_LIMIT_TEST_MULTIPLIER` > 1.
- `--pause N` waits N seconds between live-stack suites (also skipped with the multiplier).
- `--jobs N` runs parallel-safe suites concurrently (see "Parallel runs"). `--skip unit,functional` leaves suites out. `--timings RUN_DIR` feeds the dry-run estimate.
- `--where host|docker` forces where pytest runs.
- `--force` runs a suite even when a hard prerequisite is missing.
- `-q` stops echoing suite output to the console. The output is still written to the log.

### Other commands

- `python3 scripts/test.py db create|drop|reset|status` manages the throwaway schema for the functional layer inside `vector-mysql-1`. The name comes from `TEST_DB_NAME` (default `vtest`). The runner refuses any name without `test` in it, and refuses `outreach_ai`. `make test-db-create` and `make test-db-drop` do the same.
- `python3 scripts/test.py env up|down|status [--e2e] [--build] [--execute]` prints the docker commands for the plain stack or the e2e wiring. Add `--execute` to run them.
- `python3 scripts/test.py report [RUN_DIR]` re-aggregates a run directory.
- `python3 scripts/test.py collect` snapshots the results files the suites last wrote into a report, without running anything. Use it to make a baseline for comparison.
- `python3 scripts/test.py trace` regenerates `docs/TRACEABILITY.md` (see section 6).

### Coverage

Coverage of `app/` is measured per layer with pytest-cov (settings in `.coveragerc`: `source = app`, branch
coverage on). The unit number shows how much logic is tested without a database; the functional number is
the total the API tests reach. CI fails when either drops below its floor in `ci/coverage_floor.json`
(docs/CI_GATES.md, "Coverage").

```bash
# unit layer (no database); add --cov-report=html:reports/coverage-unit/html for a browsable report
python3 scripts/test.py run unit --where host \
  --extra="--cov=app --cov-report=term --cov-report=json:reports/coverage-unit/coverage.json"
python3 scripts/ci/check_coverage.py --layer unit --report reports/coverage-unit/coverage.json

# functional layer (throwaway schema); the runner passes --extra on to pytest the same way
python3 scripts/test.py run functional \
  --extra="--cov=app --cov-report=term --cov-report=json:reports/coverage-functional/coverage.json"
python3 scripts/ci/check_coverage.py --layer functional --report reports/coverage-functional/coverage.json

# a single module while writing tests
python -m pytest tests/unit/test_email_utils.py --cov=app.utils --cov-report=term-missing
```

`check_coverage.py` prints line % and branch % per package next to the floors. After adding tests, raise the
floors with `--update` and commit `ci/coverage_floor.json` with the tests.

Do not run two layers with coverage at the same time from the same checkout: both write `.coverage*` data
files in the repository root and pytest-cov combines whatever it finds there, so the numbers mix. Give each
run its own data file instead, e.g. `COVERAGE_FILE=reports/.coverage-unit`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Everything passed. |
| 1 | A case failed, or a suite errored (it crashed, timed out or wrote no results). |
| 2 | Suites were skipped for missing prerequisites and nothing failed. |
| 3 | Another run holds the lock. |
| 130 | The run was interrupted. |

## 3. Adding a test

Name new files by these rules and the runner finds them. You don't need to edit the registry unless a suite needs special prerequisites, a case filter or ordering.

### Unit: `tests/unit/test_<module>.py`

Rules for unit tests:

- Pure functions only.
- Fix every clock: pass datetimes in, or monkeypatch `random`, `time` and `datetime` in the module under test.
- Pin settings with `monkeypatch.setattr(settings, ...)`.
- No database, network or docker. The layer's `conftest.py` makes those raise `UnitTestIsolationError`.
- Mock a socket call by monkeypatching the module's own `socket` attribute.

```python
"""Discount maths (app/services/pricing.py)."""
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services import pricing


@pytest.mark.parametrize("amount,pct,expected", [("100", 10, Decimal("90.00")), ("0", 50, Decimal("0.00"))])
def test_discount(amount, pct, expected):
    assert pricing.apply_discount(amount, pct) == expected


def test_cap_comes_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "MAX_DISCOUNT_PCT", 20)
    with pytest.raises(ValueError):
        pricing.apply_discount("100", 30)
```

`tests/unit/pytest.ini` makes the folder its own pytest root. So `python -m pytest tests/unit` never loads `tests/conftest.py`, which migrates and connects to MySQL. A full `python -m pytest` from the repo root still runs both layers. Every test in the folder gets the `unit` marker, and every other test under `tests/` gets `functional`.

### Functional: `tests/test_<feature>.py`

These tests use the fixtures in `tests/conftest.py`:

- `client`: a `TestClient`.
- `tenant_a` / `tenant_b`: self-signed-up workspaces with `headers`.
- `tenant_a_data`: a campaign, template, inbox, contact, conversation and one sent e-mail.
- `register_tenant(client, label)`, `login()`, `auth(token)` and `sql(query, **params)`.

The schema is a throwaway one, so create your own data and assert on it. Never rely on rows that another module created.

```python
"""Discount approval API (BRD BR-SF-xx)."""
from tests.conftest import register_tenant, sql


def test_large_discount_needs_approval(client):
    t = register_tenant(client, "disc")
    r = client.post("/proposals", headers=t["headers"], json={"name": "P", "discount_pct": 40})
    assert r.status_code == 201, r.text
    assert r.json()["approval_status"] == "PENDING"
    assert sql("SELECT approval_status FROM proposals WHERE proposal_id = :p", p=r.json()["proposal_id"]) == "PENDING"
```

### System: `system_tests/<feature>/run_<feature>.py`

A system suite uses `system_tests/common.py`:

- `Workspace(label)` makes a fresh tenant through `/api/auth/register`. `.add_user(role, first)` adds a role user by SQL inside that tenant.
- `req(method, path, body, token)` calls the API.
- `sql(query)` runs SQL through `docker exec vector-mysql-1`.
- `Results(suite, dir)` records cases and writes `results.json` / `results.md` next to the script.

The runner discovers the file as suite `<feature>`, runs it from its folder and reads `<feature>/results.json`.

```python
#!/usr/bin/env python3
"""<Feature> system suite (BRD x.y). Creates its own workspaces.   python3 run_<feature>.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import Results, Workspace, req, sql  # noqa: E402

R = Results("<feature>", HERE)
W = Workspace("feat")                    # own tenant: never use demo data or another suite's workspace
AGENT = W.add_user("AGENT", "Ann")

s, body = req("POST", "/contacts", {"email": f"a.{W.rid}@feat-{W.rid}.example"}, W.token)
R.check("FT-001", "Owner creates a contact (201)", s == 201, (s, body))
R.check("FT-002", "Row stored in the owner's tenant",
        sql(f"SELECT tenant_id FROM prospects WHERE email='a.{W.rid}@feat-{W.rid}.example'") == W.tenant_id)
s, _ = req("GET", f"/contacts/{body.get('prospect_id')}", None, AGENT["token"])
R.record("FT-003", "Agent cannot open another user's contact", s == 404, (s,))
R.record("FT-004", "Sending case", None, "blocked: needs a business day")   # None = BLOCKED
R.write()
```

Each sign-up counts against the limit of 10 per hour per IP. Use the fewest workspaces you can, and set `"registers"` in the registry if you add an explicit entry.

### UI: `system_tests/ui_features/<feature>.mjs`

There are two supported patterns:

- **ui_crm helpers (current convention):** export `run(rec, browser)` and end with `if (isMain(import.meta)) standalone('<feature>', run);`. Import both from `../ui_crm/crm_common.mjs`. `Tenant.get('<label>')` reuses an SQL-created tenant and wipes its rows. Results land in `system_tests/ui_crm/out/<feature>/results.json`, which the runner reads.
- **ui_common helpers:** use these when you need a fresh workspace through sign-up.

```js
// UIF-FT: <feature> screen.   node system_tests/ui_features/<feature>.mjs
import path from 'node:path';
import { Workspace, launch, session, watch, Recorder, step, WEB } from '../ui/ui_common.mjs';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const rec = new Recorder('ui_features <feature>', path.join(HERE, 'out', '<feature>'));   // -> out/<feature>/results.json
const ws = await Workspace.create('feat');
const browser = await launch();                          // CHROMIUM env or /opt/pw-browsers/chromium
const { page } = await session(browser, { email: ws.email, password: ws.password });   // cookies cached in system_tests/.auth
const w = watch(page);
await step(rec, 'UIF-FT-01', 'Feature page renders without console/API errors', async () => {
  await page.goto(`${WEB}/app/feature`);
  await page.getByRole('heading', { name: 'Feature' }).waitFor();
  const problems = [...w.drain(), ...(await w.bodyProblems())];
  return problems.length ? problems.join('; ') : true;    // true/undefined = PASS, null = BLOCKED, string = FAIL
});
await browser.close();
rec.write();
```

Pace your logins: the API allows 60 per 5 minutes per IP. `session()` caches cookies, and `crm_common` paces logins to 25 per 5 minutes.

#### Cross-screen UI suites

These suites follow the ui_crm convention and share `system_tests/ui_features/_ui_extra.mjs` (not a suite: the `_` prefix keeps it out of discovery). It provides uncached form logins (`freshLogin`), signed-out contexts, the overflow and accessible-name probes, and the `SCREENS` list (key screens with their primary action).

| Suite | Ids | Tenant | What it covers |
|---|---|---|---|
| `ui_features.template_library` | `UIF-TL-01..17` | `tmpllib` | Template library: CRUD, merge fields, validation, category filter and search, preview, render for a contact, usage count, AGENT vs admin rights, private templates, API failures |
| `ui_features.platform_admin` | `UIF-PA-01..17` | `padmin`, `pa_victim` | `/admin` console as a PLATFORM_ADMIN created by SQL (removed at the end): roster, detail, suspend and reactivate, create tenant, stats, route guards, and the scheduler switch through the API. The suite saves `platform_flags.scheduler_paused` first and restores it in a `finally` block. It deletes the tenant it creates. |
| `ui_features.mobile_layout` | `UIF-MOB-01..90` | `mobile` | 13 key screens (incl. the Sales reports CRM KPIs tab) at 390x844 and 768x1024: horizontal overflow (the detail names the offending elements), primary action, nav, content width, modal fit. Screenshots go to `ui_crm/shots/mobile_layout/`. |
| `ui_features.a11y_smoke` | `UIF-A11Y-01..92` | `a11y` | 19 screens (incl. the Sales reports CRM KPIs tab): accessible names from Chromium's AX tree (CDP), one h1, Tab reaches the primary action, and modal dialog semantics, focus trap, Escape and focus return. To check another screen, add it to `ROUTES` or `MODALS`. |
| `ui_features.session_auth` | `UIF-SESS-01..23` | `session` | Signed-out redirects, silent refresh when the access cookie is gone (including a form submit retried with the rotated CSRF token), CSRF 403, dead session, and sign-out (state, cookies, refresh token, Back button, other tab). It uses fresh logins only, so the shared cookie cache is never touched. |

Run one with `python3 scripts/test.py run ui --suite ui_features.mobile_layout`, or `node system_tests/ui_features/mobile_layout.mjs`. Set `ONLY=<regex>` to run only the matching case ids.

## 4. Case ids, results and comparison

### Case-id conventions

Ids are permanent. Never renumber or reuse one, because run-to-run comparison and traceability key on them. Add a letter suffix for a new sub-case (`OUT-024b`).

| Suite | Pattern |
|---|---|
| contacts | `CM-NNN` |
| sales_pipeline | `SP-NNN` |
| outreach | `OUT-NNN` |
| reports_platform | `RP-NNN` |
| new_features | `NF-NNN` |
| API sweep | `API-<METHOD>-<path>-<KIND>` |
| E2E | `E2E-J<n>-<nn>` |
| UI CRM | `UIC-<AREA>-NN` |
| UI outreach | `UIO-<AREA>-NN` |
| UI features | `UIF-<FEATURE>-NN` |
| contract | `CONTRACT-<METHOD> <path>` |
| pytest | `<module>::<test name>` |

A new suite picks a short new prefix. Link a case to the BRD in one of these ways:

- a `BRD ref` column in the suite's `TEST_CASES.md`
- a `(BR-XX-NN)` in the case title
- a `# ... BR-XX-NN ...` comment line above the code that records the case

### How results are captured

1. Every suite writes `results.json` in the same shape: `{suite, counts: {PASS, FAIL, BLOCKED}, seconds, cases: [{id, title, status, category, detail}]}`. The Python suites use `common.Results`, the UI suites use `Recorder`, e2e uses `Results`, and pytest uses `--junitxml`, which the runner converts. The contract check's JSON is turned into cases too.
2. The runner accepts a results file only when it was written during this run. A stale file means the suite status is ERROR.
3. Each run gets its own folder, `reports/test-results/<timestamp>[-layer]/`, which holds:
   - `summary.md`, for humans: totals per layer, every suite, every failure with id and detail, blocked cases, comparison
   - `summary.json`, for tools: everything above, with all cases
   - `logs/<suite>.log`, the full output of each suite
   - `raw/`, a copy of each suite's results file
   - `reports/test-results/latest` (a symlink) and `LATEST` (a text file), which point at the newest run
4. Comparison happens automatically against the previous run folder:
   - regressions (now FAIL)
   - new failing cases
   - fixed cases (FAIL to PASS)
   - newly blocked and unblocked cases
5. `reports/` is git-ignored. To keep a baseline, `collect` snapshots the current per-suite results files.

Case statuses:

- **PASS**
- **FAIL**: a product defect, a missing requirement or a test issue. See the `category` field.
- **BLOCKED**: the case could not run here, e.g. no SES, a weekend, or a missing tool.

Suite statuses:

- **PASS**, **FAIL** or **BLOCKED** (only blocked cases)
- **ERROR**: the suite crashed, timed out or wrote no results
- **SKIPPED**: a prerequisite is missing
- **NOT_RUN**: the run stopped earlier

## 5. Environment and prerequisites

`python3 scripts/test.py doctor` checks everything below and prints the exact command to fix each missing item. It only checks and never starts anything.

| Needed by | Prerequisite | Default / variable |
|---|---|---|
| unit, functional | Python with `requirements.txt` + `requirements-dev.txt`, or docker with `TEST_PY_IMAGE` | `TEST_PYTHON`, `./venv`; `TEST_PY_IMAGE=vector-api:latest`; `TEST_CA_CERT` (or `PIP_CERT`) when pip needs a CA bundle |
| functional | MySQL with a throwaway schema | `vector-mysql-1`, `TEST_DB_NAME=vtest`, `TEST_DB_ROOT_PASSWORD=root`; or `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` (CI). `DATABASE_URL` is ignored unless `TEST_ALLOW_DATABASE_URL=1`. |
| system, e2e, ui | Stack up: api `:8191`, web `:8190/vector`, mysql, worker; self sign-up allowed | `API_URL`, `WEB_URL`, `MYSQL_CONTAINER`, `TEST_API_CONTAINER`, `TEST_WORKER_CONTAINER` |
| contract | OpenAPI from the API, frontend source | `FRONTEND_DIR=../Vector_Frontend` |
| outreach, e2e | `openssl` (fake IMAP certificate); containers reach the host at `172.18.0.1` | |
| e2e | SES stand-in on `:5001`, api + worker with `AWS_ENDPOINT_URL=http://ses-mock:5000` and `SEND_ON_ANY_DAY=true` | `SES_MOCK_URL`; see below |
| ui (and UI parts of system suites) | Node 18+, Playwright 1.63 in `system_tests/node_modules`, Chromium | `CHROMIUM=/opt/pw-browsers/chromium` (contacts/sales UI scripts hard-code that path) |

The runner gives every suite these variables:

- `API_URL`, `WEB_URL` and `MYSQL_CONTAINER`.
- `OPENAI_API_KEY=""`.
- `OUTREACH_IGNORE_DOTENV=1` (pytest suites, host and docker).
- `PW_DIR`, `PW_BASE` and `PW_MODULES` set to `reports/.work`. That folder links to `system_tests/node_modules` and is writable, so the suites' Playwright and fake-IMAP scratch files stay out of the source tree. Variables you set yourself win.

### The `.env` file is never read by tests

`.env` is git-ignored and holds a developer's real keys (OpenAI, AWS SES, ...). In-process test runs (unit, functional, anything using `TestClient` or importing `app`) must not load it:

- `app/core/config.py` skips `.env` (both pydantic's `env_file` and the `.env` key scan that ranks it above `app-properties.ini`) when `OUTREACH_IGNORE_DOTENV=1`, and also when the flag is unset but `pytest` is loaded. `OUTREACH_IGNORE_DOTENV=0` forces it back on; production and dev processes, which set neither, read `.env` as before.
- `tests/conftest.py` and `tests/unit/conftest.py` set the flag before anything imports `app.core.config`; `scripts/test.py` sets it for host and docker pytest runs (docker mounts the repo, `.env` included, at `/src`); CI sets it on the unit, contract and functional jobs. `tests/unit/test_enviso_dotenv.py` proves it with a fake `.env` in `tmp_path`.
- `ENVIRONMENT=test` alone is not a guard: `.env` itself may set `ENVIRONMENT`.
- **Live stacks are different.** `docker compose` reads `.env` by design (variable substitution for api and worker), and so does any host `uvicorn` started from the repo root without the flag (e.g. `scripts/load/replicas.sh`, which only blanks `OPENAI_API_KEY`). A stack used for system, e2e or ui tests must be started with empty keys: `OPENAI_API_KEY= AWS_ACCESS_KEY_ID= AWS_SECRET_ACCESS_KEY= docker compose up -d api worker` (the e2e wiring points SES at the stand-in via `AWS_ENDPOINT_URL`). Jobs run with `docker exec vector-api-1 python -c ...` use that container's environment.

### Bringing the stack up

The real commands come from `docker/LOCAL_DOCKER.md` and `system_tests/e2e/README.md`. The runner prints them:

```bash
python3 scripts/test.py env up              # docker compose -f docker-compose.yml [-f $TEST_COMPOSE_OVERRIDE] up -d
python3 scripts/test.py env up --e2e        # build vector-api:e2e + vector-ses-mock if missing, then
                                            # compose -f docker-compose.yml [-f override] -f system_tests/e2e/docker-compose.e2e.yml
                                            #         up -d --no-build ses-mock api worker
python3 scripts/test.py env down --e2e      # back to the plain api/worker, remove vector-ses-mock-1
# add --execute to run them, --build to rebuild images
```

These variables control the commands, and none of them is tied to a scratchpad:

- `TEST_COMPOSE_OVERRIDE`: an extra compose file. In the sandbox this is the override that resets `build:` to the prebuilt `vector-api` / `vector-web` images, so `--no-build` is used.
- `TEST_SANDBOX_DOCKERFILE`: the Dockerfile for `vector-api:e2e`. The default is the repo `Dockerfile`; the sandbox uses `Dockerfile.sandbox`, which installs pip packages with a CA bundle.
- `TEST_CERTS_DIR`: a folder containing `ca.crt`, passed as `--build-context certs=...`. `Dockerfile.ses-mock` always needs it. Outside the sandbox, a copy of `/etc/ssl/certs/ca-certificates.crt` named `ca.crt` works.
- `FRONTEND_DIR`.

## 6. Traceability

`python3 scripts/test.py trace [--brd FILE]` writes `docs/TRACEABILITY.md`. It contains:

- BRD requirement (BR-xx-nn and § sections) mapped to cases and their latest statuses
- requirements with **no case**
- requirements with a failing case
- every catalogued case (`system_tests/*/TEST_CASES.md`) with its latest status
- every recorded case that is not in a catalogue (NF, E2E, UIC, UIO, UIF, API sweep grouped by endpoint)

Statuses come from each suite's `results.json`, overlaid with the latest run's `summary.json`. The requirement list is the checked-in register `docs/REQUIREMENTS.md` (ID, requirement, status); `--brd FILE` or `TEST_BRD_PATH` points it at another list (any BRD markdown/text works), and requirements that no case covers are listed too. Re-run `trace` after a test run. `python3 scripts/test.py trace --check` is the CI gate (docs/CI_GATES.md): it fails, without needing any results, when a requirement with status Fixed, Decision or Change request has no case in a catalogue or suite script.

## 7. Known constraints (the runner plans around them)

- **One run at a time.** The runner takes a lock (`reports/.runner.lock`), so a second run refuses to start. Within a run, suites run one at a time unless `--jobs N` (only suites `TIERS.json` marks parallel-safe, section 2 "Parallel runs").
- **Login limit: 60 per 5 minutes per client IP. Sign-up limit: 10 per hour per IP.** Both live in the API process memory. Every suite on this machine shares the IP.
  - The registry records how many sign-ups each suite makes. `run` warns when a plan would exceed the budget, and `doctor` estimates what is left since the API last started.
  - `--restart-api-between` (`docker restart vector-api-1`) clears the counters between suites. The suites themselves also cache workspaces (`.state.json`) and login cookies (`system_tests/.auth`).
- **Test-stack speedups** (settings of the api and worker containers; both are refused at start-up in production):
  - `RATE_LIMIT_TEST_MULTIPLIER=<n>` (default 1) multiplies the **per-client-IP** limits only: login attempts, sign-ups, reset / magic-link requests, two-factor attempts and the global per-IP ceiling. Per-email and per-(email, IP) limits, i.e. the account lockouts, are never relaxed. With n > 1 the runner skips `--restart-api-between` / `--pause`, scales the sign-up budget (`run` warning, `doctor`) by n, runs `--jobs` pools without the 429 warning and no longer forces reports_platform last. With n = 1 everything behaves as before. `TEST_RATE_LIMIT_MULTIPLIER=<n>` tells the runner the value of a stack it cannot inspect.
  - `X-Test-Default-Rate-Limits: 1` on a request makes a relaxed API count it against the real limits, in a bucket of its own. The rate-limit cases (reports_platform RP-140..142, opt-in with `RP_RATELIMIT=1`) send it, so they still see the production limits. Without the multiplier the header changes nothing.
  - `SEND_RETRY_BASE_SECONDS=<s>` (default 60) is the base of the send-retry backoff; 1 retries a failed send after 2 s, 4 s instead of minutes.
  - Jobs on demand: `docker exec vector-api-1 python -c "import asyncio; from app.jobs import run_once; asyncio.run(run_once('webhooks'))"` runs one iteration of a worker job (any job name of `app/jobs.py`) under the worker loop's own lock, so it never overlaps the worker and, when it returns, everything due at the call has been handled. Suites use it through `system_tests/common.run_job` instead of sleeping for worker cycles.
  - Local bring-up with both: `RATE_LIMIT_TEST_MULTIPLIER=100 SEND_RETRY_BASE_SECONDS=1 docker compose up -d --build api worker`. The e2e wiring sets the multiplier to 100 by default; CI's live-stack job sets both.
- **reports_platform runs alone and last** while the per-IP limits are the defaults: with `RP_RATELIMIT=1` its checks lock this IP out of logins for about 15 minutes, and its load test measures latency. The registry marks it `run_last`. With the multiplier its lockout probes use their own bucket, so it is no longer forced last, but it stays serial (never two copies of RP-140..142 or the load test at once).
- **Sending needs a business day.** The scheduler holds every send outside US business days, so on weekends and US holidays a few cases in outreach and sales_pipeline are BLOCKED. `SEND_ON_ANY_DAY=true` lifts this. It is a test and staging aid that the app ignores in production, and the e2e wiring sets it. `doctor` warns on weekends.
- **OpenAI: tests must never call it.** There is a daily spend cap (`OPENAI_MAX_USD_PER_DAY`, default $0.20, enforced in `app/services/llm_budget.py`). Guards:
  - `tests/conftest.py` and `tests/unit/conftest.py` force `OPENAI_API_KEY=""`, so every LLM path takes its no-key fallback. The environment variable beats `.env`.
  - The unit layer blocks all network access.
  - The runner gives every child process an empty key.
  - `doctor` warns if the API container has a key. Recreate the stack with `OPENAI_API_KEY= docker compose up -d api worker` for test runs, because the e2e wizard and reply classification would otherwise call OpenAI.
- Typical durations (one at a time, default limits, run 20261028-050134): system about 30 min, e2e about 27 min (J1 alone about 15-19 min), ui about 43 min, everything about 1 h 30 min. Targets with the tiers, `--jobs 4` and the test-stack speedups: smoke <= 10 min, core ~25 min, full ~45 min; `run <tier> --dry-run` prints the current estimate. Measured on the 4-vCPU sandbox with `RATE_LIMIT_TEST_MULTIPLIER=100` and `SEND_RETRY_BASE_SECONDS=1` (tune runs of 2026-10-28): smoke about 16 min, nearly all of it J1, whose real waits are the background IMAP cadence for J1-69 (~4 min) and the 120 s re-spread of step 3 for J1-103 (~9 min); core about 18 min with J1 next to J2-J6 (26-28 min with the journeys one after another); full about 32 min (without the contacts perf cases and the reports_platform load test).

## 8. Test data and fixtures

Every suite creates its own data. **No suite uses the demo data** (`seed_contacts_demo` / `seed_sales_demo`) or another suite's workspace. Ids, e-mails and domains carry a per-run random id.

| Suite | Data |
|---|---|
| unit | None |
| functional | Its own tenants (`register_tenant`) in the throwaway schema, which is recreated before each runner run |
| contract | None (reads source + OpenAPI) |
| new_features, contacts, sales_pipeline, reports_platform, ab_testing, behaviour_fixes, contact_scoring, crm_kpis, discount_approval, webhooks | Fresh workspaces through self sign-up on every run; role users inserted by SQL inside their own tenant |
| e2e | Each journey signs up its own workspaces |

The exceptions all reuse their own data or touch something shared, never demo data:

- **outreach**: workspaces A/B are created once and cached in `system_tests/outreach/.state.json`, to save sign-ups. Every run creates fresh, uniquely named data inside them.
- **api_sweep**: tenants A/B are cached in `system_tests/api_sweep/.state.json` for 25 minutes, then rebuilt. `--fresh` forces new ones.
- **ui_outreach**: workspaces are cached in `system_tests/ui_outreach/.state.json`. Logins use a per-workspace `X-Forwarded-For`.
- **ui_crm and ui_features**: these use tenants created by SQL, not sign-up (`Tenant.get(label)`, state in `system_tests/ui_crm/state/`). Their CRM rows are wiped at the start of each run.
- **Global effects:**
  - contacts runs the global CRM purge job, which the BRD defines as global.
  - e2e runs the hourly jobs on demand. They act on all tenants, but every assertion filters on the journey's own data.
  - The SES stand-in (moto) is shared by everything on the stack, so mail is always filtered by the suite's own recipients and domains.
  - reports_platform's rate-limit checks affect this client IP.

## 9. Troubleshooting

- **The docker daemon is dead after a restart** (`Cannot connect to the Docker daemon`, while a stale pid file blocks `dockerd`):
  ```bash
  pgrep -a dockerd || { rm -f /var/run/docker.pid /var/run/docker/containerd/containerd.pid; dockerd > /tmp/dockerd.log 2>&1 & }
  docker info && python3 scripts/test.py env up --execute
  ```
- **429 / "Too many requests"** during a run: the sign-up or login budget is used up. Run `docker restart vector-api-1` or use `--restart-api-between`, and wait about 15 minutes after reports_platform.
- **Sending cases BLOCKED**: it is a weekend or a US holiday in New York. Run on a weekday, or use the e2e wiring (`SEND_ON_ANY_DAY=true`).
- **The e2e suite exits 2 with "PRE-FLIGHT FAILED"**: the stand-ins are not wired. Run `python3 scripts/test.py env up --e2e --execute` (with `TEST_CERTS_DIR` set when images must be built).
- **A suite ERRORs with "no fresh results file"**: it crashed before `R.write()`. Read `reports/test-results/latest/logs/<suite>.log`.
- **Unit test fails with `UnitTestIsolationError`**: the code under test opened a socket or a DB connection. Mock it, or move the test to `tests/` as a functional test.
- **pip fails in docker mode**: set `TEST_CA_CERT` (or `PIP_CERT`) to a CA bundle. Proxies on the host's loopback are not passed into containers. Or install the dependencies into `./venv` so pytest runs on the host.
- **Playwright cannot find Chromium**:
  ```bash
  cd system_tests && npx playwright install chromium
  ln -sf "$(node -e "console.log(require('playwright').chromium.executablePath())")" /opt/pw-browsers/chromium
  ```
- **A leftover lock**: if no run is active, delete `reports/.runner.lock`. The runner also ignores locks whose process has gone.

## 10. CI

`.github/workflows/ci.yml` has these jobs:

- `unit`: ruff, the gates in docs/CI_GATES.md and `scripts/test.py run unit` with coverage, then the unit coverage floor (`scripts/ci/check_coverage.py`), with no database. It runs on every push and PR and uploads `coverage-unit` (JSON, XML, HTML).
- `test`: ruff, pip-audit, migrations and the functional layer with coverage against a `mysql:8.4` service, then the functional coverage floor; uploads `coverage-functional`. It runs on every push and PR, and gates `CICD.yaml` deploys together with `unit` and `secrets`.
- `live-stack`: only on **Run workflow** (`workflow_dispatch`). Input `tier` (smoke, core, full; default core), or input `layer` (system, e2e, ui) to run one layer instead. It builds the whole compose stack on the runner with `RATE_LIMIT_TEST_MULTIPLIER=100` and `SEND_RETRY_BASE_SECONDS=1`, installs Playwright, and wires the SES stand-in (every tier has e2e journeys). Then it runs `scripts/test.py run <tier> --keep-going --jobs 4 --skip unit,functional` (those two already ran as the `unit` / `test` gates) and uploads `reports/ci-live`. It needs the secret `FRONTEND_REPO_TOKEN` (read access to Vector_Frontend).
- The `unit` job also runs `scripts/test.py tiers --check` (static validation of `system_tests/TIERS.json`).
