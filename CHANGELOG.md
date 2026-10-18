# Changelog

All notable changes to the Outreach 360 backend. The release number lives in `VERSION` (read by
`app/core/config.py` as `APP_VERSION`, shown by `/health` and sent to Sentry as the release); CI
(`scripts/ci/check_version.py`) fails when this file, `VERSION` and the other version references disagree.
The frontend keeps its own CHANGELOG.md with the same release number. Format: [Keep a Changelog](https://keepachangelog.com/),
versions follow [SemVer](https://semver.org/).

## [1.1.0] - Unreleased

Release candidate for BRD v2.0 Phase 1 (R1.0) and Phase 2 (R2.1). Requirements and their status:
`docs/REQUIREMENTS.md`; test traceability: `docs/TRACEABILITY.md`.

### Added
- CI gates for the NTS Build Standard Addendum (`docs/CI_GATES.md`): complexity (C-1), type-ignore ratchet (C-2),
  no `assert` in app code (C-3, ruff S101 + bandit B101), import-layer contracts and function-local import
  register (C-4/C-5), requirement traceability, release identity, frontend/backend route contract, blocking
  dependency audit with a dated ignore list. Baselines in `ci/` may only shrink.
- Checked-in requirements register `docs/REQUIREMENTS.md` (BRD v2.0, 92 requirements, status per requirement);
  `scripts/test.py trace` reads it by default and `trace --check` is the CI gate.
- `VERSION` file as the single release identity.
- Session limits (ADR 0007): 30-minute idle timeout, 12-hour absolute cap, refresh-token reuse detection that
  revokes the whole session (migration 0015).
- BRD section 8 KPIs: duplicate rate, reply-to-lead, CRM adoption, forecast accuracy (actual / committed);
  `GET /sales-reports/kpis` (migration 0014).
- RP-103 discount approval, RP-104 A/B subject-line tests, RP-106b outbound webhooks, CM-083 contact scoring.
- Multi-company contacts, @mentions in notes (only users who can see the record), SQL-to-win and forecast KPIs.
- Logout token revocation (migration 0013), platform admin console API.
- OpenAI hard caps: $0.20/day spend cap (reserved before each call, settled from usage), daily call limit shared
  across processes, output-token ceiling.
- Layered test framework (`scripts/test.py`: unit, functional, system, e2e, ui) and the `docs/TESTING.md` guide.

### Changed
- Refactor: routers are thin HTTP adapters over services; service-layer domain errors (`app/core/errors.py`);
  typing for app/core, app/services, app/utils (mypy in CI); architecture notes in `docs/ARCHITECTURE.md`.
- The AGENT role gets `manage_campaigns` by default; role defaults have a single source.
- Contact search: digits match phone numbers only for phone-like queries.
- `domain_error_handler` guards its input with an explicit type check instead of `assert` (kept under `python -O`).

### Security
- Fail closed (I-11): in Kubernetes the app refuses to start without an explicit `ENVIRONMENT`; manifests make
  the `ENVIRONMENT` secret key required. Production also refuses a wildcard CORS origin and requires
  `METRICS_TOKEN` while `/metrics` is enabled (or `METRICS_ENABLED=false`). Guards proven by
  `tests/unit/test_production_guards.py`.
- Mailbox SMTP/IMAP always verify TLS certificates and host names and never log in over plain text;
  `MAIL_TLS_VERIFY=false` is refused in production.
- Review fixes: privilege check on reactivation, expired-invite sign-in refused, exact tenant-name duplicate check.
- API sweep results are redacted before they are recorded; cached test tokens and raw captures are untracked.

### Fixed
- 422 handler crash on custom validators; nullable blueprint lists; whole-second webhook queue times.
- Unique-prospect click counts on every screen; global timeseries replied/bounced always 0; inbox tie-break.
- E2E-found defects: reply-only unsubscribe, paused contacts kept receiving, inbound counted as sent.
- API-sweep defects: wizard/list/regeneration/activation 500s; DB errors mapped to 422/409; NUL-byte guard.

## [1.0.0]

Baseline: BRD v1.0 feature set (campaigns, AI email content, sequencing and SES sending, deliverability,
multi-inbox rotation, prospect upload, global unsubscribe, roles and audit log, multi-tenancy, reports).

## Release checklist

Run for every release, in order, together with the deployment-hardening steps in `docs/RELEASE.md`
(image pins, container hardening). CI enforces the items marked (CI).

1. Bump `VERSION` (SemVer) in the backend and `version` in the frontend `package.json` / `package-lock.json`
   to the same number; move the `Unreleased` entries under the new number and set today's date in both
   CHANGELOG.md files (CI: `scripts/ci/check_version.py`, frontend `npm run version:check`).
2. All gates green on the release commit, with no new baseline entries (CI, `docs/CI_GATES.md`). Baselines
   that shrank are committed shrunk.
3. `python3 scripts/test.py trace --check` passes (CI) and `docs/TRACEABILITY.md` is regenerated from the
   release-candidate run (`python3 scripts/test.py trace`); every requirement marked Out of release in
   `docs/REQUIREMENTS.md` is signed off by the product owner.
4. Dependency audits: no unreviewed findings and no ignore entry past its review date (CI: pip-audit and
   `npm run audit:check`).
5. Migrations: `alembic heads` is a single head; the release notes name any migration that locks large tables.
6. Deployment secret (`sales360-secret`) has `ENVIRONMENT`, `JWT_SECRET_KEY`, `CREDENTIALS_ENCRYPTION_KEY`,
   `SES_WEBHOOK_TOKEN` and, in production, `METRICS_TOKEN` (the pods refuse to start without them).
7. Tag both repositories `v<VERSION>` on the commits that were deployed, and build images tagged with it.
8. Live-stack suites (Actions -> CI -> Run workflow, layer `all`) on the release candidate; results attached
   to the release.
