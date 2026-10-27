# Refactor plan: database design, layering, session framework, UI, tests

Status: proposed, 2026-10-25. Source findings: the database review, the architect's layering feedback
(route / service / repository / domain-query / UI / session framework / unit-test isolation), and the open
items in [REVIEW_RESPONSES.md](REVIEW_RESPONSES.md) and [DECISIONS.md](DECISIONS.md).

## How the work is cut

- **One chunk = one agent run** (about 1–2 hours) ending in **one commit** on a green tree.
- **Each chunk owns its files.** Chunks in the same wave never edit the same file, so 2–3 can run in parallel.
  When usage is tight, run one chunk at a time in the listed order; nothing is lost between chunks.
- **Migrations are pre-numbered** (below), so parallel chunks never fight over the Alembic chain.
- **Done means:** every CI gate in [CI_GATES.md](CI_GATES.md) passes, unit + functional tests pass on a fresh
  database, the OpenAPI schema is unchanged unless the chunk says otherwise, and every baseline the chunk
  touches has shrunk (never grown). Live system / UI / E2E suites run only in wave 6.
- **Decision-gated** chunks (marked ⏸) wait for the matching row in DECISIONS.md.

## Target architecture (agreed in chunk L0, ADR 0009)

| Layer | Folder | May contain | May not |
|---|---|---|---|
| Route | `app/routers` | Parse request, `Depends`, call one service function, map result to response model | SQLAlchemy, models, business rules |
| Service | `app/services` | Business rules, transactions (unit of work), calls repositories and adapters through Protocols | SQLAlchemy queries, FastAPI types |
| Domain / query | `app/domain` | Typed query/filter objects (parameter objects), value objects, pure rules | I/O |
| Repository | `app/repositories` | All SQL and ORM mapping, per aggregate | Business rules |
| Adapters | `app/adapters` | SES, IMAP/SMTP, OpenAI, storage, Microsoft 365 behind Protocols | Business rules |
| UI | frontend `src/api` + react-query; Tailwind 4 + Radix UI primitives | Calls routes only, through one read hook and mutations | Hand-rolled fetch in effects or hand-rolled accessible widgets |

Unit tests per layer: route (fake service), service (fake repository + fake adapters), domain (pure).
Repositories and queries are tested against MySQL in the functional layer. Import-linter enforces the table;
current violations sit in a shrinking baseline.

## Migration numbers (reserved)

| Rev | Chunk | Change |
|---|---|---|
| 0017 | D1 | Collation: every table utf8mb4_0900_ai_ci |
| 0018 | D2 | TIMESTAMP → DATETIME(6) (UTC) |
| 0019 | team-review P0 | One live account per email (`users.active_email` unique) |
| 0020 | team-review P1 | Send integrity keys: one email per step, (inbox_id, sent_at) index |
| 0021 | team-review P1 | `automation_rule_runs`: once per rule and contact |
| 0022 | D3 | tenant_id on child tables + backfill + tenant-first indexes |
| 0023 | D5 | CHECK constraints on status columns |
| 0024 | D7 | FULLTEXT ngram index for contact search |
| 0025 | D6 | Retention bookkeeping (archive tables / purge log) |
| 0026+ | later | Assigned when a decision-gated chunk starts |

## Waves and chunks

### Wave 0 — foundation (sequential, small)

| ID | Chunk | Owns | Size |
|---|---|---|---|
| L0 | ADR 0009 layering; `app/domain`, `app/repositories`, `app/adapters` skeletons; base `Repository`/`UnitOfWork` Protocols; import-linter contracts for the table above with today's violations baselined | `docs/adr/0009*`, `.importlinter`, new packages | S |
| Q0 | Coverage gate: pytest-cov on unit + functional, ratcheting floor file `ci/coverage_floor.json` | `.github/workflows/ci.yml`, `scripts/ci/` | S |
| DEC | DECISIONS.md: DEC-22 stay on MySQL 8.4 vs PostgreSQL (default: MySQL) | `docs/DECISIONS.md` | XS |

### Wave 1 — database (D1, D2 sequential; then D3, D4, D5, D7, D8 in parallel)

| ID | Chunk | Owns | Size |
|---|---|---|---|
| D1 ✅ | One collation (0017); `scripts/test.py` and compose use it. Done: `app/core/db_charset.py`, every table and connection declares it | migration, `app/db/*` charset settings | S |
| D2 ✅ | DATETIME(6) everywhere (0018); `app/core/clock.py` `utcnow()` (naive UTC: the columns hold UTC without a zone) replaces 224 `datetime.utcnow()`; ruff DTZ003 bans new ones | all models' time columns, every `utcnow` call site | L (mechanical) |
| D3 | `tenant_id` on email_messages, email_events, campaign_prospects, email_templates, email_sequences, email_template_versions, prospect_list_members, proposal_lines, campaign_state_events, … (0022) with backfill, tenant-first indexes, writers set it, per-table isolation tests | those models + their writers | L |
| D4 | UUIDv7 default for new primary keys (`app/core/ids.py`), no data change | `app/core/ids.py`, model defaults | S |
| D5 | CHECK constraints for email/campaign/deal/proposal/lead status machines (0023); shared status enums in `app/domain/status.py` | migration, `app/domain/status.py` | M |
| D7 | Contact search on FULLTEXT ngram (0024) + 100k-contact seed + timing test | `contact_query_service` search, seed script | M |
| D8 | MySQL TLS via `connect_args` (off until DEC-01), RDS CA bundle path | `app/core/database.py`, config, docs | S |
| D6 ⏸ | Retention: archive/purge job for events, messages, activities, webhook deliveries (0025), default off (DEC-14) | new `app/services/retention.py`, jobs | M |

### Wave 2 — layering pattern (one agent, then review before wave 3)

| ID | Chunk | Size |
|---|---|---|
| L1 | **Contacts aggregate** as the reference: typed `Mapped[]` models for prospects/accounts link; `app/domain/contacts.py` (ContactFilter, sort, paging); `app/repositories/contacts.py`; services call the repository Protocol; routers thin; UUID path IDs; response models; unit tests per layer; remove the aggregate's baseline entries | L |

### Wave 3 — aggregates in parallel (same pattern as L1; disjoint files)

| ID | Aggregate (routers / services) | Size |
|---|---|---|
| A1 | Accounts, lists, prospect lists, recycle bin | M |
| A2 | Campaigns, sequences, templates, wizard, subject variants | L |
| A3 | Sales: leads, opportunities, proposals, quotes, sales admin, sales reports | L |
| A4 | Inbox: conversations, IMAP sync, SES webhook, deliverability, sending inboxes | L |
| A5 | Auth, users, invitations, platform admin, tenants | M |
| A6 | Reports, custom reports, CRM KPIs, analytics | M |
| A7 | Import, automation rules, webhooks, contact scoring, notifications | M |
| A8 | Worker side: email scheduler, sender, warmup, send-now — split the complexity hotspots (93/80/55) | L |

Each A-chunk also: parameter objects for long filter lists (C-6), UUID-typed IDs (C-7), response models (C-8),
shrink complexity / local-import / import-linter baselines (C-1, C-4, C-5).

### Wave 4 — cross-cutting (parallel with late wave 3)

| ID | Chunk | Size |
|---|---|---|
| S1 | Session framework: replace hand-rolled JWT on python-jose with PyJWT + Authlib (or fastapi-users), keep CSRF / idle / absolute / reuse policies on top; drop the python-jose audit accept | M |
| S2 | Adapters behind Protocols (SES, IMAP/SMTP, OpenAI, storage, M365) + DI providers; tests use `dependency_overrides`, not module patching (C-9) | M |
| F1 | Frontend read hook (react-query + AbortController, drops late answers); move the 13 `useEffect` fetchers (W-9) | M |
| F2 | Errors shown with the request reference everywhere; remove console-only catches (W-10) | S |
| F3a/F3b | Render tests for the 49 pages, two halves (W-11) | M + M |
| F4 | Frontend session: http.js refresh/CSRF/idle on react-query + one auth context | S |
| F5 | UI component framework: keep Tailwind 4; move dialogs, menus, selects, tabs, toasts onto Radix UI primitives (shadcn/ui style) and retire the hand-rolled `useDialog`; visual output unchanged | M |

### Wave 5 — decision-gated ⏸

| ID | Needs | Chunk |
|---|---|---|
| G1 | DEC-06 | D-11: HMAC audit seals with external key + write-once anchors + verify command |
| G2 | DEC-13 | D-13: erasure ledger outside the DB + re-apply command |
| G3 | DEC-09 | Shared rate limits / daily counters (Redis) |
| G4 | DEC-03 | Attachments on S3/EFS (worker currently sends without them) |
| G5 | DEC-04 | `OCC_REQUIRE_VERSION=true` + update the ~95 test calls that send no version |
| G6 | DEC-10 | API-sweep AGENT campaign checks |
| G7 | DEC-22 = PostgreSQL | Migration to PostgreSQL + row-level security (replaces D1/D7 specifics) |

### Wave 6 — evidence

| ID | Chunk |
|---|---|
| V1 | Live regression on one commit (all levels) → `docs/EVIDENCE_<date>.md` |
| V2 | Load test (100k contacts, 2 API replicas) and restore drill write-up |

## Suggested order under usage limits

1. L0, Q0, DEC (one session)
2. D1 → D2 (one each)
3. D3 ‖ D4 ‖ D5 (≤3 agents) then D7 ‖ D8
4. L1 (review the pattern)
5. A1–A8 in pairs or threes, with S1, S2, F1–F4 interleaved
6. Wave 5 as decisions land; wave 6 last
