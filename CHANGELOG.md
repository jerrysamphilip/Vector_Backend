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
- Round 4 (database and team-review leftovers):
  - `tenant_id` on 14 child tables, filled from the parent by one flush listener (`app/models/tenant_fill.py`),
    with tenant-first indexes and database-enforced tenant integrity (composite keys, one primary company per
    contact) (D3, C-22; migration 0028). CHECK constraints on status columns, vocabularies in
    `app/domain/status.py` (D5; 0029).
  - Contact and global search used a FULLTEXT ngram pre-filter (D7; 0030); reverted by 0034 (see Fixed).
  - Optional MySQL TLS (`MYSQL_SSL_MODE`, `MYSQL_SSL_CA`; D8) and time-ordered UUIDv7 primary keys for new rows
    (`app/core/ids.py`; D4). Short ids in file names, PDF references and logs now use an id's last 8
    characters (`short_id`), because UUIDv7s made in the same minute share their first 8.
  - Custom-property safety (C-17; 0031): properties are deactivated instead of deleted (values kept, still
    exported, reactivation), system properties protected, type fixed after creation, option renames keep stored
    values (`{value, label}`), new BOOLEAN and LONG_TEXT types and `max_length`. Inactive properties are hidden
    from import mapping.
  - IMAP settings derived from SMTP when not set (S-26), DNS records compared with live DNS with a suggested
    value (SPF merged) (S-25), and the scheduler parks messages until the sending window opens and moves a new
    contact to the campaign mailbox with the most room when one hits its daily cap (S-17).
  - Migration 0032 and model `server_default`s give a freshly built database the same 23 column defaults as an
    upgraded one (users.notify_email, webhook counters, forecast snapshot totals and others), so raw-SQL
    writers (scripts, system tests) work on both.
  - All remaining import-linter exceptions removed (57 -> 0); `app/core/auth.py` reads users through an
    `AuthStore` that `app/main.py` installs (fails closed when none is installed).
- Layered architecture applied to every remaining aggregate (wave 3: accounts, lists, identity/auth, import,
  automation, webhooks, scoring, notifications, campaigns, inbox, deliverability, worker): routers thin, services own
  the unit of work, SQL in repositories, `Mapped[]` models with identical DDL, OpenAPI unchanged for existing paths.
  External services behind adapters with DI providers (`app/adapters`: SES, SMTP/IMAP, OpenAI, file storage, OAuth).
- Edit a campaign after launch: `POST /campaigns/{id}/edits` (+ `/edits/checks` dry run) re-renders and re-plans unsent
  mail under row locks and row_version, adds/deletes steps safely, rebinds inboxes; e-mail previews as a real contact,
  template assignments, launch `pacing_warning`, business-day waits (migration 0027).
- Contacts: stored duplicate pairs with confidence and reasons, dismiss/reopen, merge preview with row locks, merge log
  snapshots, e-mail history so retired and merged addresses resolve to the surviving contact, export audit log, unique
  list names and paged lists (migration 0025).
- Companies: custom properties, backfill preview / run history / undo, export, field propagation with history
  (migration 0024).
- Background CRM import: upload, mapping (saved), dry run, checkpointed worker job with progress, cancel and resume;
  import modes, date/number/choice parsing, sheet choice (migration 0026). Hard-bounce automation trigger.
- Jobs: duplicate scan (6 h), hourly domain-health snapshots with 90-day retention; the scheduler wakes when the next
  paced mailbox is ready.
- Layered architecture applied to contacts (reference, `docs/LAYER_PATTERN.md`), sales and reports: typed domain
  query objects, repositories holding all SQL, services owning the unit of work, thin routers, `Mapped[]` models
  with identical DDL. OpenAPI and report outputs unchanged (golden test `tests/test_a6_report_outputs.py`).
  73 import-layer exceptions, 16 complexity hotspots, 71 function-local imports and 9 `# type: ignore` removed.
- Campaigns: duplicate (migration 0022), clear fields with null, enrolment dry run, timing columns, removed
  contacts hidden and re-enrolable, weighted inbox rotation.
- Inbox: reply classification POSITIVE / NEUTRAL / NEGATIVE / OUT_OF_OFFICE / UNSUBSCRIBE (rules first, LLM only
  within the daily cap; migration 0023); confident unsubscribes suppress, low-confidence ones are flagged for
  review; conversation search, labels, campaign, activity timeline and paging; sending-pipeline alerts (no mailbox,
  limit reached, failure rate, missing delivery events, SES unavailable, stuck sending); resume a paused mailbox.
- CI gates for the NTS Build Standard Addendum (`docs/CI_GATES.md`): complexity (C-1), type-ignore ratchet (C-2),
  no `assert` in app code (C-3, ruff S101 + bandit B101), import-layer contracts and function-local import
  register (C-4/C-5), requirement traceability, release identity, frontend/backend route contract, blocking
  dependency audit with a dated ignore list. Baselines in `ci/` may only shrink.
- Checked-in requirements register `docs/REQUIREMENTS.md` (BRD v2.0, 92 requirements, status per requirement);
  `scripts/test.py trace` reads it by default and `trace --check` is the CI gate.
- `VERSION` file as the single release identity.
- DATETIME(6) for every time column (chunk D2, migration 0018): no 2038 limit, microsecond precision; every
  connection runs in UTC (`time_zone = '+00:00'`). `app/core/clock.py` `utcnow()` replaces `datetime.utcnow()`
  (deprecated); ruff DTZ003 rejects new uses. API timestamps now carry microseconds.
- One collation (chunk D1, migration 0017): every table, column and connection on utf8mb4 / `utf8mb4_0900_ai_ci`
  (`app/core/db_charset.py`), set on the table instead of taken from the schema default. A schema created as
  `utf8mb4_unicode_ci` (an RDS default) used to fail the build with MySQL 3780; 0017 converts existing mixed tables.
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
- A hard bounce stops the campaign's remaining steps per step stop_on_bounce; company backfill covers only the contacts
  the user can see; `DELETE /campaigns/sequences/{id}` refuses (409) once a step was sent, else removes its unsent mail
  and renumbers; list names that already exist get " (2)" on upload/import/bulk add.
- Conversations follow the contact owner/team visibility scope (an agent sees the conversations of the contacts
  they can see); before, any member of the workspace saw all of them.
- Refactor: routers are thin HTTP adapters over services; service-layer domain errors (`app/core/errors.py`);
  typing for app/core, app/services, app/utils (mypy in CI); architecture notes in `docs/ARCHITECTURE.md`.
- The AGENT role gets `manage_campaigns` by default; role defaults have a single source.
- Contact search: digits match phone numbers only for phone-like queries.
- `domain_error_handler` guards its input with an explicit type check instead of `assert` (kept under `python -O`).

### Security
- Test runs no longer read the git-ignored `.env` (a developer's real OpenAI / AWS keys; one in-process test had
  made a real SES call). `app/core/config.py` skips `.env` when `OUTREACH_IGNORE_DOTENV=1` (set by both test
  conftests, `scripts/test.py` and CI) or, with the flag unset, whenever pytest is loaded; `OUTREACH_IGNORE_DOTENV=0`
  opts back in. Production and dev processes still read `.env`.
- SNS webhook hardening (C-04/C-09): signed timestamp window (24 h back, 15 min ahead), RSA certificates within
  their validity only, certificate cache with TTL and size limit, no redirects, no userinfo or non-443 ports in
  certificate and subscribe URLs, optional `SNS_ALLOWED_TOPIC_ARNS`, 503 when the certificate cannot be fetched
  so SNS retries. Positive tests sign real SignatureVersion 1 and 2 messages.
- SMTP/IMAP connections resolve the host once, check it (no private/loopback/metadata addresses when deployed) and
  connect to that address with the hostname kept for TLS verification, closing the DNS-rebinding gap (S2).
- Session tokens on PyJWT 2.15.1 behind `app/core/tokens.py` (S1): python-jose and ecdsa removed, so the
  dependency audit has no accepted exceptions left. Only the configured HS algorithm is accepted (`none`, other HS
  sizes and asymmetric headers refused); `exp` (and `jti` on access tokens) required. Tokens issued before the
  switch stay valid; session policies (idle, absolute cap, refresh reuse detection, CSRF, MFA) unchanged.
- Reset, invite and magic links are built from a validated origin only (`app/utils/safe_origin.py`): a crafted
  `Origin: http://evil.example\\@localhost` no longer yields a link to another host (F024); localhost origins only
  in a local environment.
- One live account per e-mail across workspaces (F017, migration 0019 `users.active_email` unique); login, reset and
  Google sign-in pick the active account; invite / accept / reactivate refuse a second one with 409.
- Mail-server hosts (SMTP/IMAP) must not resolve to loopback, private, link-local or metadata addresses when
  deployed (400; was 422 for the old checks). Auth dependencies no longer block the event loop (F004).
- Automation rules only use templates of the caller's workspace and need `manage_campaigns` to change (F058, F038).
- SES webhook: 1 MiB body cap (413), no exception text in responses. Upload confirm re-validates every row on the
  server; PATCH /prospects/{id} applies the contact visibility scope (404 outside it) and records history.
- Secrets in app-properties.ini are ignored with a warning; precedence is environment > .env > ini.
- Fail closed (I-11): in Kubernetes the app refuses to start without an explicit `ENVIRONMENT`; manifests make
  the `ENVIRONMENT` secret key required. Production also refuses a wildcard CORS origin and requires
  `METRICS_TOKEN` while `/metrics` is enabled (or `METRICS_ENABLED=false`). Guards proven by
  `tests/unit/test_production_guards.py`.
- Mailbox SMTP/IMAP always verify TLS certificates and host names and never log in over plain text;
  `MAIL_TLS_VERIFY=false` is refused in production.
- Review fixes: privilege check on reactivation, expired-invite sign-in refused, exact tenant-name duplicate check.
- API sweep results are redacted before they are recorded; cached test tokens and raw captures are untracked.

### Fixed
- Handover pass:
  - Campaign wizard drafts are owner-scoped: a draft id outside the caller's visibility scope returns 404 on read
    and delete (creator, admins, and managers over their team keep access; lists show only your own drafts).
  - Launch requires a template on every sequence step; a step without one is refused with a 400 naming the step
    (counting templates let a step with none pass when another step had several persona versions, and that step
    was silently skipped).
  - DEC-20: the background IMAP sync and webhook delivery skip suspended / deleted workspaces like the scheduler and
    warm-up; held webhook deliveries stay PENDING and go out after reactivation.
  - Removed the stray repo-root scripts `check_db.py`, `diagnose_duplicate_conversations.py` and
    `test_warmup_e2e.py` (the last contained real staff e-mail addresses and ran against the live session factory).
  - Docs refreshed for handover: REFACTOR_PLAN, OPERATIONS, DECISIONS (DEC-24..27 proposed), REVIEW_RESPONSES,
    CI_GATES, TRACEABILITY (regenerated), e2e FINDINGS.
- Warm-up now reads peer mailboxes with the effective IMAP settings (S-26) like the IMAP sync: a mailbox with
  SMTP settings but blank IMAP fields takes part in open/spam-rescue engagement (derived host, SMTP password
  reused) and is no longer flagged `IMAP_NOT_CONFIGURED`; saved IMAP settings still win, Microsoft 365
  mailboxes keep signing in with their OAuth token, send-only relays (SES, SendGrid...) are never read.
- UAT 1: step bodies saved by the rich-text editor (one line of HTML) were wiped by the line-based CTA and
  footer cleanup, so emails went out with only the footer and sign-off and the inbox showed "(No content)".
  The cleanup now works per HTML block (links unwrapped, text kept); rendering keeps the body if cleanup
  would empty it, and the sender refuses a body with no visible text (`FAILED`, `BLOCKED_EMPTY_BODY`, no SES
  call). HTML emails get the postal line, unsubscribe line and sign-off as separate paragraphs.
- UAT 2: API datetimes were naive ISO strings, which browsers read as local time (a 17:54 UTC bounce showed
  as 05:54 PM everywhere). Every API datetime now ends in `Z` (`app/schemas/types.py` `UtcDatetime`,
  `app/core/json_encoding.py`, `clock.iso_utc`). Bounces use SES's bounce/complaint timestamp, and the
  "Delivery Failed" note is a system notice (`is_system`, `last_message_is_system`): not shown or counted as a
  reply anywhere (inbox, timelines, scoring, account/sales/campaign reply counts, reply labels).
- UAT 3-5: open and reply rates divide by delivered (sent − bounced) everywhere (campaign analytics, steps,
  reports, exports); the daily chart's last day no longer adds lifetime opens; "sent today" counts only sent
  emails; enrolments marked OPENED advance and complete (campaigns can auto-complete). Frontend labels fixed.
- A/B subject-variant stats divided open and reply rates by sent: they now use delivered (sent − bounced) like
  every other screen (bounce rate stays bounced / sent, click rate clicked / opened). Each variant row gains
  `bounced`, `delivered` and `bounce_rate`; the winner rule uses the same open-over-delivered rate (the minimum
  sample still counts sent emails), so a variant with more bounces can now win where it lost before.
- Fresh-vs-upgraded schema parity (`0035_schema_parity`): five columns NOT NULL in upgraded databases but
  NULLable in fresh builds (`campaigns.auto_paused`, `sending_domains.sends_24h` / `bounce_rate_24h` /
  `complaint_rate_24h`, `sending_inboxes.auth_type`) are NOT NULL everywhere (NULLs set to the default first);
  `updated_at` of `imap_sync_state`, `import_mappings` and `webhook_endpoints` refreshes on UPDATE in fresh builds
  too; upgraded databases get the five foreign keys the models declare and lose four legacy single-column
  indexes and a duplicate `users.invited_by` foreign key. information_schema of a fresh build and an upgraded copy
  is now identical (columns, indexes, keys).
- Contact and global search missed committed contacts: while another workspace was committing inserts, some
  contacts never matched the FULLTEXT pre-filter (D7, 0030) and so never appeared in search. Search is the
  plain `search_text LIKE` scan again (always exact) and `0034_drop_contact_fulltext` drops
  `ft_prospects_search_text`; connections no longer set `innodb_ft_enable_stopword`. Regression test
  `tests/test_final_contact_search.py`.
- Sign-in returned 500 when recording `last_login_at` hit a MySQL deadlock (1213) or lock wait timeout
  (1205): the login unit of work (session row, refresh token, `last_login_at`) now retries up to 3 times.
- "Send now" returned 500 for the rest of a batch when one message hit a MySQL deadlock: the error handler
  read the expired message before rolling the session back. It now rolls back first, retries the message once
  while it is still QUEUED, and otherwise marks it FAILED and carries on.
- Regression V1: contact merge / erasure / purge deadlocked with imports (full-table `merged_into_id`
  update across tenants): tenant-first index (0033), tenant-scoped update by key, retry on 1213/1205.
  `POST /uploads/{id}/confirmations` and `/revalidations` use the path id (400 on a mismatching body id, 404
  for another workspace's id).
- `conversations.last_message_at` no longer resets to now when a conversation is updated (e.g. marked read);
  the campaign schedule lists outbound emails only.
- Sending (team review): campaign mail is never sent from the platform address (no mailbox: stays queued; launch,
  resume and send-now refuse with NO_ACTIVE_INBOX); pacing no longer sleeps, so one workspace's delay cannot stall
  others (delay capped at 3600 s); SES outages re-queue mail (up to 50 times) behind a 5-minute circuit breaker
  instead of failing the queue, and a mailbox is paused after 5 sender rejections in 30 minutes; a bounce recorded
  mid-send survives; the SDK never resends a message (F079); one e-mail per step is a database key (migration 0020);
  per-step stop-on-reply / stop-on-bounce flags are honoured.
- Replies thread by the SES message id; bounce/complaint sender domains are parsed from `Name <a@dom>`;
  Delivery events that arrive while a message is still sending are recorded; AI follow-up regeneration uses the
  follow-up tone; an automation rule runs once per contact (migration 0021).
- Contacts: one bad row no longer fails an upload or a CRM import (per-row errors naming the field); CSVs in
  Windows-1252 import correctly; XLSX size and column guards; free-mail and company-domain rules use the full
  domain (subdomains collapse to the registrable domain; free-mail domains cannot be company domains); changing an
  e-mail refuses a suppressed address and moves unsent mail to the new one. Duplicate e-mail on
  PATCH /prospects/{id} is now 409 (was 400), like /contacts.
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
