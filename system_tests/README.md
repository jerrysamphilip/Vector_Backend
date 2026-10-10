# BRD system & integration tests

These suites check the running application against BRD v2.0 (*Outreach360 CRM*). Each suite:
- creates its own workspace through self sign-up, so suites never touch each other's data or the demo data;
- records every case by ID in `results.md` and `results.json`.

They test a **live stack**, so they are not part of CI. The `tests/` folder holds the CI suite.

| Suite | BRD sections | Cases | Script |
|---|---|---|---|
| `contacts/` | §5.2 contact management, CM NFRs (10k import, search speed), §7 data rules | `TEST_CASES.md` | `run_contacts.py` |
| `sales_pipeline/` | §5.4 hierarchy, §5.5 daily limit, §5.6 leads & SQL, §5.7 pipeline | `TEST_CASES.md` | `run_sales_pipeline.py` |
| `outreach/` | §5.1 defect fixes, §5.3 existing outreach features, §7 compliance | `TEST_CASES.md` | `run_outreach.py` |
| `reports_platform/` | §5.8–5.12 reports, forecast, team, extra features; §6 platform NFRs | `TEST_CASES.md` | `run_reports_platform.py` |

`REPORT.md` holds the consolidated results and findings.

## Running

Start the local stack first (`docker/LOCAL_DOCKER.md`). Self sign-up must be allowed, which is the default outside production. Then run each suite from its own folder:

```bash
cd system_tests/contacts && python3 run_contacts.py
```

Environment variables:

| Variable | Default |
|---|---|
| `API_URL` | `http://localhost:8191` |
| `WEB_URL` | `http://localhost:8190/vector` |
| `MYSQL_CONTAINER` | `vector-mysql-1` |
| `MYSQL_DATABASE` | `outreach_ai` |

**Requirements:**
- Python 3 (standard library only).
- Docker access to the MySQL container, for setup and checks.
- Node with Playwright, for the `ui_*.mjs` browser checks.

**When to run:**
- **On a weekday.** The scheduler holds every send outside US business days, so a few sending cases are BLOCKED at weekends.
- **Run the reports suite last.** Its login rate-limit checks lock the client IP out of logins for about 15 minutes.

`common.py` holds the shared helpers: workspace and user creation, HTTP calls, SQL checks and result recording.
