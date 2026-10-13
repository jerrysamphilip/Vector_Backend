# System, E2E and UI tests

These suites test the **running stack** against BRD v2.0 (*Outreach360 CRM*):

- the system layer (`*/run_*.py`, `contract/`)
- the end-to-end journeys (`e2e/`)
- the Playwright UI suites (`ui_outreach/`, `ui_crm/`, `ui_features/`)

The unit and functional layers live in `tests/`.

**Full guide:** [`docs/TESTING.md`](../docs/TESTING.md). It covers the layers, one command per layer, how to add a suite, case-id conventions, results and comparison, prerequisites, rate limits and troubleshooting.

## Which suites exist

The list of suites is no longer kept here. It lives in **`tests/registry.json`**: id, layer, command, working directory, prerequisites, results file, typical duration, sign-ups made and notes.

The runner also discovers new files on its own:

- `system_tests/<dir>/run_<x>.py` becomes a system suite `<dir>`
- `system_tests/ui_features/<x>.mjs` becomes a UI suite `ui_features.<x>`

See every suite, with case counts and the last results:

```bash
python3 scripts/test.py list        # add -v for commands and case-filter syntax
```

Each suite has these properties:

- It creates its own workspace (tenant), so suites never touch each other's data or the demo data. Section 8 of the guide lists the few that reuse their own cached workspace.
- It records every case by id in `results.md` and `results.json` next to its script (UI areas: `out/<area>/`).

`REPORT.md` holds the consolidated findings of earlier runs. `<suite>/TEST_CASES.md` holds the case catalogues, and `docs/TRACEABILITY.md` maps BRD requirements to cases.

## Running

Start the local stack first. The runner prints the commands (`docker/LOCAL_DOCKER.md` has the background).

```bash
python3 scripts/test.py doctor                      # what is missing, and how to fix it
python3 scripts/test.py run system                  # every system suite, one at a time, reports_platform last
python3 scripts/test.py run e2e                     # needs: python3 scripts/test.py env up --e2e --execute
python3 scripts/test.py run ui
python3 scripts/test.py run system --suite contacts --only CM-04      # one suite, a subset of cases
```

Results of each run go to `reports/test-results/<timestamp>/summary.md`. That file compares the run with the previous one.

You can still run a single suite by hand from its folder, e.g. `cd system_tests/contacts && python3 run_contacts.py`, with these environment variables:

| Variable | Default |
|---|---|
| `API_URL` | `http://localhost:8191` |
| `WEB_URL` | `http://localhost:8190/vector` |
| `MYSQL_CONTAINER` | `vector-mysql-1` |
| `MYSQL_DATABASE` | `outreach_ai` |
| `PW_DIR` / `PW_BASE` / `PW_MODULES` | a folder whose `node_modules` holds Playwright (the runner uses `reports/.work`) |
| `CHROMIUM` | `/opt/pw-browsers/chromium` |

**Requirements:**

- Python 3 (standard library only).
- Docker access to the MySQL container, for setup and SQL checks.
- Node 18+ with Playwright (`cd system_tests && npm install --no-save --no-package-lock playwright@1.63.0`), for the browser checks.

**When to run:**

- **On a weekday.** The scheduler holds every send outside US business days, so a few sending cases are BLOCKED at weekends. The e2e wiring sets `SEND_ON_ANY_DAY=true`.
- **Run the reports suite last.** Its login rate-limit checks lock the client IP out of logins for about 15 minutes. The runner always orders it last.
- **Mind the rate limits.** The API allows 10 sign-ups per hour and 60 logins per 5 minutes per IP. Use `run ... --restart-api-between` for long runs.

`common.py` holds the shared Python helpers: workspace and user creation, HTTP calls, SQL checks and result recording. `ui/ui_common.mjs` and `ui_crm/crm_common.mjs` hold the Playwright equivalents.
