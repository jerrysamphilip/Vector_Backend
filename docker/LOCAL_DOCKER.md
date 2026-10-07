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
