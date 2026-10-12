# Running Vector locally in Docker

Vector runs as its own compose project (`vector`) next to the other apps behind the shared hub nginx,
the same way PatientTrack does.

```
http://localhost/vector/        through the hub nginx
http://localhost:8190/vector/   directly (web container)
http://localhost:8191/docs      backend Swagger
localhost:3316                  MySQL (user vector / vector, db outreach_ai)
```

## Layout

Check out both repos side by side (or set `FRONTEND_DIR` in `.env`):

```
Backend/     docker-compose.yml lives here (C:\Projects\Vector\Backend)
Frontend/    (C:\Projects\Vector\Frontend)
```

## Start

```bash
cd C:\Projects\Vector\Backend
cp .env.example .env      # optional; every value has a default. If you already have a .env, append to it instead.
docker compose up -d --build
```

On first start the one-shot `migrate` service creates all tables in the empty MySQL (Alembic) and seeds persona blueprints.
The UI has no sign-up page, so create your first account (a Super Admin with its own workspace) via the API:

```bash
curl -X POST http://localhost:8190/vector/api/api/auth/register -H 'Content-Type: application/json' \
  -d '{"first_name":"Dev","last_name":"User","email":"dev@example.com","password":"devpass123","tenant_name":"Dev Co"}'
```

Then sign in at http://localhost:8190/vector/.

## Demo data

To try the contact management features with realistic data (reps, accounts, ~90 contacts, a finished
campaign with email history, logged calls/meetings/notes, custom fields and a few duplicates to merge):

```bash
docker compose exec api python -m app.db.seed_contacts_demo            # into the first Super Admin's workspace
docker compose exec api python -m app.db.seed_contacts_demo --reset    # remove it again
```

Re-running replaces the previous demo data. Demo emails use the reserved `.example` domain and the campaign is
completed, so nothing is ever sent. Demo reps sign in as e.g. `marcus.bell@vector-demo.example` / `demo-pass-123`.

For the sales features (Phase 2), run this after the contact demo. It builds a four-level team, 100 leads at every
stage, an SQL queue, 30 opportunities across this financial year (some won, some lost) and proposals:

```bash
docker compose exec api python -m app.db.seed_sales_demo               # needs seed_contacts_demo first
docker compose exec api python -m app.db.seed_sales_demo --reset       # remove it again
```

Sign in as `arjun.mehta@…` (L1 Sales Head, sees everything), `priya.nair@…` (L2 Business Development, her team),
`marcus.bell@…` / `sofia.alvarez@…` (L3 Business Executive) or `leo.fischer@…` (L4 Market Research), all
`@vector-demo.example` / `demo-pass-123`. Set levels and managers for real users under Sales → Sales Team.

## Hub nginx

Add the block in `docker/hub-nginx-vector.conf` to the hub's `server { ... }` block (next to PatientTrack)
and reload the hub (`docker exec <hub-container> nginx -s reload`). The hub needs
`extra_hosts: ["host.docker.internal:host-gateway"]`, which it already has for PatientTrack.

## How the paths fit together

The frontend is built with `BASE_PATH=/vector/`, so the SPA, its assets and its API calls all live under
`/vector/` and do not clash with WSR's `/api/` or TeamMap's `/assets/`. The web container
(`Frontend/docker/nginx.local.conf`) strips `/vector/api` and forwards the rest to the backend,
exactly like the Vite dev proxy does with `/api`. EKS builds are unaffected: `BASE_PATH` defaults to `/`.

## Day-to-day

```bash
docker compose up -d --build api     # after backend changes
docker compose up -d --build web     # after frontend changes
docker compose logs -f api
docker compose down                  # stop (add -v to wipe MySQL data and uploads)
```

For hot-reload frontend work, run Vite against the dockerised backend:

```bash
cd C:\Projects\Vector\Frontend
API_PROXY_TARGET=http://localhost:8191 npm run dev     # http://localhost:5173
```

## Notes

- Compose also reads this repo's `.env` for `${...}` values. The stack's database, `BASE_URL`, `FRONTEND_URL`
  and `APP_PROFILE` are fixed or use `LOCAL_*` names, so prod values in an existing `.env` don't leak in.
  `OPENAI_API_KEY`, `AWS_*` and `SENDER_EMAIL` are taken from it when set.
- Without AWS/OpenAI keys the background jobs (scheduler, SES/deliverability sync, warmup) log errors and carry on.
- Microsoft 365 mailboxes connect with OAuth (Email Accounts → key icon on the inbox). Set `MS365_CLIENT_ID`,
  `MS365_CLIENT_SECRET` (and `MS365_TENANT_ID` if single-tenant) in `.env` from an Azure app registration with
  the redirect URI `http://localhost/vector/api/inboxes/oauth/microsoft/callback` and the delegated permissions
  `IMAP.AccessAsUser.All`, `SMTP.Send` and `offline_access`. SMTP AUTH must be enabled for the mailbox.
- Sending safety: campaigns pause automatically when the hard-bounce rate reaches 5% or the complaint rate 0.3%
  (from 50 sends; before that at 5 hard bounces or 2 complaints). Tune with `AUTO_PAUSE_*` settings. Every sent
  email shows a final status (delivered, bounced, complained, rejected, failed, or unconfirmed when SES sent no
  event within `DELIVERY_CONFIRM_MINUTES`, default 15).
- Sales settings: `FISCAL_YEAR_START_MONTH` (default 4, April; 1 = calendar year) drives the forecast, and
  `DAILY_NEW_CONTACT_LIMIT` (default 500) caps first emails per user per day; follow-ups are never limited. Amounts
  display in USD; build the frontend with `VITE_CURRENCY=INR` (or another ISO code) to change that.
- Calendar & email sync (each user under Sales → Connections): set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
  from a Google Cloud OAuth client (scopes: Gmail read-only, Calendar events) and add the redirect
  `http://localhost/vector/api/connections/google/callback` (`GOOGLE_SYNC_REDIRECT_URI`; Google sign-in keeps its own
  `GOOGLE_REDIRECT_URI`); for Microsoft, add the redirect
  `http://localhost/vector/api/connections/microsoft/callback` and the Graph delegated permissions `Mail.Read`,
  `Calendars.ReadWrite`, `User.Read` to the same Azure app used for Microsoft 365 mailboxes. Sync runs every
  10 minutes and logs emails / meetings with your contacts on their timelines.
- Notifications are in-app (the bell) and, when `SENDER_EMAIL` and AWS are configured, also emailed; each user can
  turn email off. `QUOTE_CURRENCY` (default USD) is printed on proposal PDFs.

## Production settings

Set these in the backend secret for each environment (the manifests read them):

| Setting | Production value | What it does |
|---|---|---|
| `ENVIRONMENT` | `production` | Turns off self sign-up, `/docs`, dev-only links and seed scripts; the SES webhook refuses calls if `AWS_SNS_TOPIC_ARN` is empty |
| `ALLOW_SELF_SIGNUP` | `false` (default in production) | `true` lets anyone create a workspace from the login page |
| `AWS_SNS_TOPIC_ARN` | your SES notifications topic | Only notifications from this topic are accepted |
| `JWT_SECRET_KEY`, `CREDENTIALS_ENCRYPTION_KEY` | long random values | The app refuses to start with the development defaults |
| `TRUSTED_PROXY_HOPS` | `1` (one load balancer) | How many proxies append to `X-Forwarded-For`; used for rate limiting. The API must only be reachable through those proxies (as with the EKS ClusterIP service); a directly reachable API lets clients pick their own X-Forwarded-For and dodge the per-IP limits |
| `LOG_LEVEL` | `INFO` | Application log level |
| `ENABLE_DOCS` | unset | `true` re-enables `/docs` in production |

Health checks: `/health` (process is up) and `/health/ready` (database reachable, 503 otherwise).
The container runs as user 10001; the uploads directory must be writable by it.

## Schema migrations and the background worker

The stack runs four backend pieces:

```
migrate   one-shot: python -m app.db.migrate (alembic upgrade head + persona blueprint sync), then exits
api       HTTP only (RUN_BACKGROUND_JOBS=false); starts after migrate completes successfully
worker    python -m app.worker: scheduler, IMAP sync, deliverability, warmup, CRM purge, send safety,
          sales jobs; health on :8002 inside the container (/health, /health/ready)
mysql
```

- **Alembic is the only place the schema changes.** The API no longer creates or patches tables on start.
  `alembic/versions/0001_baseline` builds the whole schema idempotently (works on an empty database and on
  any database from an older build); databases stamped by the old chain (kept for history in
  `alembic/versions_legacy/`, not loaded) are detected and brought forward through the baseline.
- `python -m app.db.migrate` takes a MySQL named lock, so concurrent runs (several Kubernetes initContainers,
  compose + AUTO_MIGRATE) queue up and the later ones find nothing to do. Re-run it any time:
  `docker compose run --rm migrate`.
- `AUTO_MIGRATE=true` (default false) also runs the migrations when the API starts, handy for
  `uvicorn app.main:app` outside Docker.
- `RUN_BACKGROUND_JOBS` (default true, for a plain `uvicorn` run) starts the background loops inside the API.
  Compose and Kubernetes set it to false on the API and run the worker instead. Each loop iteration takes a MySQL
  named lock (`vector:job:<name>:<db>`), so an API with jobs on, a second worker or an overlapping rollout
  never runs the same iteration twice; the others log "lock held by another process" and skip.
  `WORKER_HEALTH_PORT` (default 8002) sets the worker's health port.

### Adding a schema change

```bash
alembic revision -m "add foo to campaigns"     # from the repo root, in your local venv
```

Write `upgrade()` by hand in the new file under `alembic/versions/` (check `information_schema` before an
ALTER if the change might already exist somewhere), update the model, then `docker compose run --rm migrate`.
`alembic revision --autogenerate` can be used as a starting point, but always read and trim its output before
running it: it does not know about the hand-made indexes (e.g. 0002) and will propose dropping them.
Never add DDL to application start-up again.


## Observability

Implemented in `app/core/observability.py`.

- **Request IDs.** Every response carries `X-Request-ID` (the caller's value when it is a sane
  token, otherwise a new one). The same id appears in every log line as `[<id>]`, so an error a user
  reports can be found with `docker compose logs api | grep <id>`. `LOG_FORMAT=json` switches the logs
  to one JSON object per line (`ts`, `level`, `logger`, `request_id`, `message`).
- **Metrics.** `GET /metrics` (Prometheus text format) on the API, on by default
  (`METRICS_ENABLED=false` turns it off). Set `METRICS_TOKEN` in any deployed environment; the scraper
  then sends `Authorization: Bearer <token>`. Keep `/metrics` off the public ingress either way.
  - `http_requests_total{method,route,status}` and `http_request_duration_seconds{method,route}`,
    where `route` is the route template (`/campaigns/{campaign_id}`), never the raw path.
  - `emails_sent_total{result}`, `ses_webhook_events_total{type}`, `imap_sync_runs_total{result}`,
    `job_last_success_timestamp{job}`, recorded with `record_email_sent()`, `record_ses_event()`,
    `record_imap_sync()` and `record_job_heartbeat()`.
  - Metrics are per process. The worker exposes its own: `start_metrics_server()` (port `METRICS_PORT`,
    default 9100) or a `/metrics` route on its health server.
- **Sentry (optional).**
  - API: `SENTRY_DSN`, with `ENVIRONMENT` as the Sentry environment and `SENTRY_TRACES_SAMPLE_RATE`
    (default 0, so no performance tracing). PII is never sent. Request bodies, cookies, query strings and
    auth headers are stripped before an event leaves the process.
  - Web app: `VITE_SENTRY_DSN` (and optionally `VITE_SENTRY_ENVIRONMENT`) at build time. Without it the SDK
    is not bundled at all. The CSP allows `https://*.ingest.sentry.io` and `https://*.ingest.us.sentry.io`.

Suggested alerts:

| Alert | PromQL (sketch) | Threshold |
|---|---|---|
| Send failures | `sum(rate(emails_sent_total{result!="sent"}[15m])) / sum(rate(emails_sent_total[15m]))` | > 5% for 15 min |
| Stuck background job | `time() - job_last_success_timestamp` | > 3 x the job's interval (e.g. 15 min for the 60 s loops) |
| SES complaints | `sum(increase(ses_webhook_events_total{type="Complaint"}[24h])) / sum(increase(ses_webhook_events_total{type="Delivery"}[24h]))` | > 0.1% (SES reviews accounts at 0.1%) |
| SES bounces | the same, with `type="Bounce"` | > 5% |
| API errors | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))` | > 2% for 10 min |
| IMAP sync failing | `increase(imap_sync_runs_total{result="error"}[30m]) > 0 and increase(imap_sync_runs_total{result="ok"}[30m]) == 0` | any |

## Running tests

The suite in `tests/` drives the API in-process (FastAPI `TestClient`) against a real MySQL
database. Use an **empty, throwaway** database: the tests register their own tenants and data.
`tests/conftest.py` runs `app.db.migrate` to build the schema.

```bash
# One-off test database in the compose MySQL
docker exec vector-mysql-1 mysql -uroot -proot -e \
  "CREATE DATABASE vtest; GRANT ALL ON vtest.* TO 'vector'@'%'"

# Run the suite in a throwaway container on the compose network (repo mounted at /src)
docker run --rm --network vector_default -u 0 -e HOME=/tmp \
  -e MYSQL_HOST=mysql -e MYSQL_USER=vector -e MYSQL_PASSWORD=vector -e MYSQL_DATABASE=vtest \
  -e ENVIRONMENT=test -e ALLOW_SELF_SIGNUP=true -e RUN_BACKGROUND_JOBS=false \
  -e JWT_SECRET_KEY=local-test-only-0123456789abcdef0123456789 \
  -e CREDENTIALS_ENCRYPTION_KEY=local-test-only-credentials-key \
  -v "$PWD":/src -w /src vector-api \
  sh -c "pip install -q -r requirements-dev.txt && ruff check . && python -m pytest -q"

# Clean up
docker exec vector-mysql-1 mysql -uroot -proot -e "DROP DATABASE vtest"
```

Outside Docker: `pip install -r requirements.txt -r requirements-dev.txt`, point `MYSQL_*` at an
empty database and run `python -m pytest -q`. CI (`.github/workflows/ci.yml`) does the same against a
`mysql:8.4` service container on every pull request. `CICD.yaml` runs CI first and only deploys if it passes.
