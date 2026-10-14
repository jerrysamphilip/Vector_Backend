# ADR 0006: Test framework layers

Status: Accepted (2026-10)

## Context

Tests had grown as ad-hoc scripts (some inside the `app` package, some calling OpenAI for real), API
sweeps and browser checks, each started differently and with results that could not be compared
between runs.

## Decision

Four layers behind one standard-library runner, `scripts/test.py` (details in
[docs/TESTING.md](../TESTING.md)):

| Layer | Scope | Runs in CI |
|---|---|---|
| unit (`tests/unit`) | pure logic; sockets and DB connections are blocked | every push/PR (with ruff and mypy) |
| functional (`tests/test_*.py`) | in-process API (`TestClient`) on a real MySQL schema built by the migrations | every push/PR |
| system / e2e (`system_tests/`) | the running docker stack through its public API, with SQL cross-checks; journeys through the real worker, an SES stand-in and a fake IMAP server | on demand |
| ui (`system_tests/ui_*`) | Playwright against the running stack | on demand |

- Suites are registered in `tests/registry.json`; results are stored per run for comparison.
- No test calls OpenAI or real mail providers.
- Ad-hoc developer checks live in `scripts/dev/`, outside the app package and outside test discovery.

## Consequences

- The PR gate (unit + functional) needs only Python and MySQL and runs in minutes; the expensive layers
  run before releases or on demand.
- A behaviour change should come with a test at the lowest layer that can prove it: a unit test for a
  rule, a functional test for an endpoint's contract and permissions.
