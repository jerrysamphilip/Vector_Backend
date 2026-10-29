# Load test and restore drill (chunk V2)

Evidence for DEC-18 / wave 6: contact list and search at 100k contacts, dashboards, mixed reads and writes
from 25 concurrent users against **two API replicas on one database**, and a timed backup/restore drill.
Everything runs locally against the docker MySQL (`127.0.0.1:3316`, root/root) and a database whose name
starts with `vtest_load` or `load_`; the scripts refuse any other name and never touch `outreach_ai`.

| File | What it does |
|---|---|
| `seed_load_data.py` | Seeds an empty, migrated database: 1 tenant, 10 users, 100k contacts, 2k accounts, 50 lists, 20 campaigns, ~200k email messages, leads, opportunities |
| `run_load.py` | Async HTTP load generator (httpx only), weighted request mix, p50/p95/p99, thresholds, exit 1 on breach |
| `loadlib.py` | Pure helpers for `run_load.py` (percentiles, mix, thresholds, markdown); unit-tested in `tests/unit/test_v2_loadlib.py` |
| `replicas.sh` | Starts / stops N API processes (ports 8199, 8200, ...) against one database, by recorded PID |
| `restore_drill.py` | mysqldump → drop/recreate → restore → `alembic current`, row counts, checksums, FK / unique invariants, timed |

Results go to `reports/load/` (git-ignored by the `reports/` line in `.gitignore`).

## 1. Seed

```bash
cd Vector_Backend
export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3316 MYSQL_USER=root MYSQL_PASSWORD=root MYSQL_DATABASE=vtest_load OPENAI_API_KEY=
docker exec vector-mysql-1 mysql -uroot -proot -e "DROP DATABASE IF EXISTS vtest_load; CREATE DATABASE vtest_load"
python -m app.db.migrate
python scripts/load/seed_load_data.py                       # full size
python scripts/load/seed_load_data.py --contacts 5000       # smoke size (messages default to 2 x contacts)
```

Options: `--contacts`, `--accounts` (default contacts/50), `--lists`, `--campaigns`, `--messages`
(default 2 × contacts), `--batch` (rows per INSERT, default 5000), `--seed`, `--password`, `--dup-rate` (0.05).

- **Users** (password in the manifest, default `LoadTest!2026-v2`): `owner@` (SUPER_ADMIN), `admin@` (ADMIN),
  `head@` (MANAGER, sales level 1: sees everything), `mgr.north@` / `mgr.south@` (MANAGER, level 2, see their
  team), `agent.n1..n3@` / `agent.s1..s2@` (AGENT, level 3, see their own records), all `@vector-load.example`.
- **Contacts**: names from ~190 first / ~250 last names, 90 % linked to an account (company name + domain email),
  10 % personal domains, 5 % unassigned owners, 4 custom fields (region, tier, ARR, renewal date), tags,
  lifecycle / lead status mixes, scores on 70 %. ~5 % are fuzzy duplicates (same name at the same company under
  a second address, some with a shared phone), which is what `/contacts/duplicates` looks for.
- **Campaigns**: 20 (ACTIVE / PAUSED / COMPLETED / DRAFT), 3-5 sequence steps, enrolments, messages
  SENT / BOUNCED / FAILED / QUEUED / CANCELLED, open / click / reply / bounce events, replies as unified-inbox
  conversations.
- **Sales**: default sales stages, leads for 3 % of contacts, opportunities for converted leads.

The seed is deterministic for a given `--seed` (ids, names, mixes); only timestamps are relative to the day it
runs, so "last 30 days" dashboards have data. It refuses to run unless the database is migrated and holds no
tenants, users or contacts; reseed by dropping and recreating the database. It writes
`reports/load/<db>_seed.json` (users, roles, password, row counts) for `run_load.py`.

Measured: 5k contacts in ~5 s; 100k contacts / ~200k messages in ~4 min (see "Expected runtimes").

## 2. Start the API (one or two replicas)

```bash
export PYTHON=$(which python)   # the venv with the app's requirements
scripts/load/replicas.sh start 2 8199     # http://127.0.0.1:8199 and :8200; or `start 1` for one replica
scripts/load/replicas.sh status
...
scripts/load/replicas.sh stop             # kills only the PIDs it recorded (reports/load/pids/)
```

Each replica runs `uvicorn app.main:app --workers 1` with the round-rule environment (`MYSQL_*` from the shell,
`OPENAI_API_KEY=` empty, `RUN_BACKGROUND_JOBS=false` so nothing sends mail) and the **same**
`JWT_SECRET_KEY` and `CREDENTIALS_ENCRYPTION_KEY`, as every pod gets them from `sales360-secret`.

### Shared vs per-process state with two replicas

| State | Where | With 2 replicas |
|---|---|---|
| Data, sessions (refresh-token rows, revocation), optimistic-concurrency versions | MySQL | shared: a write on one replica is read on the other; `run_load.py` round-robins every VU's requests over both |
| Access/refresh cookies | signed with `JWT_SECRET_KEY` | valid on both only when the secret is shared. `run_load.py` checks this (`cross_replica_session` in the report: replica 1's cookie used on replica 2 → 200). Without `JWT_SECRET_KEY` each process signs with its own random key and users are signed out whenever the load balancer switches replica |
| Rate-limit counters (`app/core/rate_limit.py`) | memory of each process | **not shared**: a client gets the per-IP budget once per replica (GLOBAL_PER_IP 1200 req/min, 60 sign-ins / 5 min, 10 wrong passwords / 15 min per email+IP). The module docstring already says to move them to Redis for several replicas. The per-replica table in the report shows 429s per replica |
| Background loops | worker process | off on the API here; with `RUN_BACKGROUND_JOBS=true` on both replicas the MySQL named locks keep one iteration at a time |

`run_load.py` sends a distinct `X-Forwarded-For` per virtual user (10.77.x.y). With no proxy in front, the
right-most hop is the client address (`TRUSTED_PROXY_HOPS=1`), so each VU gets its own rate-limit budget, like
real users behind the ingress. `--no-xff` makes all VUs one client: expect 429 beyond ~20 req/s per replica.

## 3. Run the load

```bash
python scripts/load/run_load.py --base-url http://127.0.0.1:8199 --base-url http://127.0.0.1:8200 \
    --concurrency 25 --duration 120 --ramp 15
```

| Option | Default | Meaning |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8199` | repeat for replicas; requests round-robin per VU |
| `--concurrency` | 25 | virtual users (RP-131 uses 25) |
| `--duration` / `--ramp` | 60 / 10 s | total run time, VUs start evenly over the ramp |
| `--think` | 0.2 s | mean think time between a VU's requests (uniform 0..2×) |
| `--mix` | see below | override weights: `--mix contacts_list=40,export_small=0` |
| `--target` | see below | `endpoint.metric=value`, repeatable; replaces the default for that key |
| `--no-default-targets`, `--min-samples` (5) | | targets on endpoints with fewer requests are SKIP |
| `--results` | `reports/load/run_<ts>` | prefix of the `.json` and `.md` output |
| `--manifest`, `--users`, `--password` | seed manifest | who signs in |

Sign-in: once per user and replica (10 users × 2 = 20 sign-ins, sequential, one address per user — well under
60 / 5 min per IP), the way the SPA does it (`X-Auth-Mode: cookie`; httpOnly `access_token` + readable
`csrf_token` echoed in `X-CSRF-Token` on writes). Each (user, replica) has its own cookie jar. On a 401 the
session is refreshed once (serialised per session, because replaying a rotated refresh token revokes it).
Each user discovers the contacts and campaigns it may see (owner/team scope), so detail requests do not 404.

Default mix (weights): contacts_list 25 (pages 1-50, sort by 6 columns, lifecycle / lead status / custom-field
JSON filter / owner=me), contacts_search 12 (`q=` last name or company-domain fragment), contact_detail 15,
contact_create 4, contact_update 5 (GET then PATCH with `If-Match`; 409 counts as expected), campaigns_list 6,
campaign_detail 6 (detail / metrics / sequences), inbox_list 8 (`/conversations` pages), inbox_search 4
(`/conversations?campaign_id=`: the inbox has no free-text search), reports_dashboard 8
(`/reports/dashboard-summary`, 7/30/90 days, admins and managers: `view_analytics`), sales_dashboard 4,
export_small 3 (CSV of a narrow search, admins and managers: BR-CM-31).

An error is a transport error / timeout or a status the scenario does not expect (any 4xx/5xx other than
409 on update). The report has per-endpoint count, errors, error rate, p50/p95/p99/max (ms), req/s and status
counts, the same per replica, and the threshold table; exit code 1 when a threshold fails, 2 when the run
could not start (sign-in, nothing seeded).

### Thresholds (defaults)

| Target | Meaning | Source |
|---|---|---|
| `contacts_list.p95 <= 1000` | contact list pages at 100k contacts under 1 s for 95 % of requests | REQUIREMENTS §6 Performance "lists < 1 s at 100k" (SP-162), ADR 0003 |
| `contacts_search.p95 <= 1000` | contact search under 1 s | §6 / CM-091..093 (p95 < 1 s; CM-091 was only extrapolated to 100k) |
| `contact_detail.p95 <= 1000` | record view under 1 s | CM-092/093 |
| `reports_dashboard.p95 <= 3000`, `sales_dashboard.p95 <= 3000` | dashboards under 3 s | §6 "dashboards < 3 s" (RP-130) |
| `ALL.p95 <= 2000` | whole mix | OPERATIONS.md alert "API latency p95 > 2 s" |
| `ALL.error_rate <= 0` | no errors at 20+ concurrent users | §6 Scale (RP-131: 0 % errors, p95 < 3 s at 25 users) |

Add a throughput floor with `--target ALL.rps=30`; loosen for a laptop with e.g. `--target ALL.p95=3000`.
The thresholds hold for the 100k seed on one machine running MySQL, two API processes and the generator;
on the stage cluster run the generator from another host.

## 4. Restore drill

```bash
python scripts/load/restore_drill.py --source vtest_load            # target vtest_load_restore, dropped after
python scripts/load/restore_drill.py --source vtest_load --keep     # keep the restored DB and the dump
```

Steps (each timed): mysqldump inside `vector-mysql-1` (`--single-transaction --routines --triggers --events`)
→ drop + recreate the target with the source's charset/collation → restore → `alembic current` on the
target equals the source's `alembic_version` → exact row count per table → order-independent content checksum
per table (CRC32 of every column; `CHECKSUM TABLE` is not used because JSON partial updates change the
physical layout, not the data) → no FK orphans (every FK in information_schema) → no duplicates in any unique
index → same number of tables / indexes / FKs / generated columns / check constraints / triggers.
Output: `reports/load/restore_drill_<ts>.{json,md}`; exit 1 on any failed check.

This rehearses the mechanics and measures dump/restore time per 100k contacts. The production restore is RDS
point-in-time recovery (OPERATIONS.md §7, steps 1-7 incl. stopping the scheduler and re-applying erasures and
unsubscribes, §8), which a local drill cannot exercise; record both in the DEC-18 write-up.

## Expected runtimes (measured in the V2 sandbox: 4 vCPU, docker MySQL 8.4 on the same host)

| Step | Smoke (5k contacts) | Full (100k contacts, ~200k messages) |
|---|---|---|
| migrate | ~5 s | ~8 s |
| seed | ~5 s | ~4 min 15 s (prospects 44 s, email_messages 167 s; MySQL-bound, the host was busy) |
| run_load | as configured (+ ~5 s sign-in/discovery) | as configured; use ≥ 120 s |
| restore drill | ~9 s (11 MB dump, ~30k rows) | ~98 s (216 MB dump in 9 s, restore 69 s, checks 19 s; 585k rows) |

Latency numbers are only meaningful on a quiet machine: MySQL, both API processes and the generator share
the CPUs, so a busy host (parallel test suites, builds) moves p95 by whole seconds.
