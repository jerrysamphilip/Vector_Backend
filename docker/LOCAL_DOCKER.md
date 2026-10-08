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

On first start the backend creates all tables in the empty MySQL and seeds persona blueprints.
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
| `TRUSTED_PROXY_HOPS` | `1` (one load balancer) | How many proxies append to `X-Forwarded-For`; used for rate limiting |
| `LOG_LEVEL` | `INFO` | Application log level |
| `ENABLE_DOCS` | unset | `true` re-enables `/docs` in production |

Health checks: `/health` (process is up) and `/health/ready` (database reachable, 503 otherwise).
The container runs as user 10001; the uploads directory must be writable by it.
