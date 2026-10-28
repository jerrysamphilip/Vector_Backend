# Outreach 360 operations runbook

Release 1.1.0. This runbook is for the people who deploy and run Outreach 360. Every statement here was checked
against the code on branch `claude/eager-gates-s6yb8g`. When something is not built, the runbook says
**Not implemented** and points to the decision that covers it in [DECISIONS.md](DECISIONS.md) (`DEC-nn`).
Background: [ARCHITECTURE.md](ARCHITECTURE.md), [adr/](adr/), [CI_GATES.md](CI_GATES.md), [RELEASE.md](RELEASE.md).

| § | Section |
|---|---|
| 1 | Deployment overview |
| 2 | Configuration and secrets |
| 3 | Health, readiness and metrics endpoints |
| 4 | Logging and observability |
| 5 | Background worker and job locks |
| 6 | Limits per process and per replica (P-4) |
| 7 | Backup and restore |
| 8 | Re-applying GDPR erasures and unsubscribes after a restore |
| 9 | Key and credential rotation |
| 10 | Alerting |
| 11 | Incident playbooks |
| 12 | Release and upgrade |
| 13 | Rollback |

---

## 1. Deployment overview

| Item | What the repository does | Where |
|---|---|---|
| Cluster | EKS `nts-innovation-ai`, `us-west-2` | `.github/workflows/CICD.yaml` |
| Environments | push to `dev` → namespace `sales360` (`manifests/deployment.yaml`); push to `stage` → `sales360-stage` (`manifests/deployment-stage.yaml`). The two manifests differ only in namespace (`tests/unit/test_manifests.py::test_stage_matches_dev_except_namespace`). | `CICD.yaml` |
| Production | **Not implemented.** `CICD.yaml` has no production branch or namespace. See DEC-21. | - |
| Gate before deploy | `CICD.yaml` calls `ci.yml` (`needs: ci`). Nothing deploys unless the unit, contract, test and secrets jobs pass. | `CICD.yaml` job `ci` |
| Image | built from `Dockerfile` (user 10001, port 8001), pushed to ECR as `<repo>:<short sha>`, and then deployed **by digest** (`<repo>:<sha>@sha256:...`). The rendered manifest is checked with `scripts/pin_images.py --check --strict`. | `CICD.yaml` steps "Push Docker image", "Update image in manifest" |
| Base-image pins (P-2) | `python3 scripts/pin_images.py` rewrites every `FROM` / `image:` to `name:tag@sha256:...`. `--check` runs in CICD. Refreshed by hand before each release ([RELEASE.md](RELEASE.md) step 1). How often to refresh is DEC-15. | `scripts/pin_images.py` |
| Workloads | `Deployment/backend` (API, **2 replicas**, rolling update maxUnavailable 0 / maxSurge 1, PDB minAvailable 1), `Service/backend` (8000 → 8001), `Deployment/backend-worker` (**1 replica**, `python -m app.worker`, health on 8002, no Service). | `manifests/deployment.yaml` |
| Migrations | Each `backend` pod has an initContainer `migrate` that runs `python -m app.db.migrate` (`alembic upgrade head` under the MySQL named lock `vector:migrate`, which waits up to 600 s, then idempotent reference data). The first pod migrates. Pods started later find the schema at head and exit at once. There is no separate Kubernetes Job. The worker has no initContainer, so it can briefly run against the old schema during a rollout. | `manifests/deployment.yaml`, `app/db/migrate.py`, ADR 0004 |
| Container hardening (P-1) | non-root 10001, `readOnlyRootFilesystem`, `capabilities.drop: [ALL]`, no privilege escalation, seccomp `RuntimeDefault`, no service-account token. The only writable paths are emptyDir volumes: `/tmp` (256Mi) and `/app/uploads` (1Gi). | `tests/unit/test_manifests.py` |
| Pod Security | `manifests/namespace.yaml` labels both namespaces `enforce: restricted`. A **cluster admin** applies it; CICD does not. Rollout timing is DEC-17. | `manifests/namespace.yaml` |
| NetworkPolicies (P-3) | `manifests/networkpolicy.yaml` (default-deny ingress; API 8001 from `ingress-nginx`, web pods and `monitoring`; egress limited to DNS, 3306, 443, 465, 587, 993, 143, and the metadata IP is blocked). CICD applies it **only when** the repository variable `APPLY_NETWORK_POLICIES=true` is set. See DEC-02. | `CICD.yaml`, ADR 0008 |
| MySQL transport | Plain text. `app/core/database.py` passes no TLS arguments. See ADR 0008 and DEC-01. | ADR 0008 |
| Rollout check | `kubectl rollout status` for `backend` and `backend-worker`, 600 s. | `CICD.yaml` |

> **Attachment storage gap (DEC-03).** `ATTACHMENTS_DIR` (`/app/uploads/attachments`) sits on a per-pod emptyDir.
> A template attachment that one API replica uploads cannot be seen by the other replica or by `backend-worker`.
> When the file is missing, `EmailSenderService._load_template_attachments` logs
> `Attachment file missing on disk, skipping` and **sends the email without the attachment**. The files are also
> lost whenever a pod restarts.

## 2. Configuration and secrets

All variables come from the Kubernetes secret `sales360-secret` (`secretKeyRef`), except `LOG_LEVEL=INFO`,
`RUN_BACKGROUND_JOBS` (`false` on the API, `true` on the worker) and `WORKER_HEALTH_PORT=8002`, which are set
in the manifest. Settings are read from the environment in `app/core/config.py`. `app-properties.ini` fills only
keys that are missing from the environment.

### 2.1 Required, or the pod refuses to start

| Variable | Rule enforced at start-up | Code |
|---|---|---|
| `ENVIRONMENT` | Required when `KUBERNETES_SERVICE_HOST` is set. The secret key is `optional: false`. `production`/`prod` switches on the production guards. | `config.py` (bottom), I-11 |
| `JWT_SECRET_KEY` | At least 32 characters and not the public default when deployed (Kubernetes, or `ENVIRONMENT` production/staging). A local run gets a random key for each process. | `secrets_guard.resolve_jwt_secret` |
| `CREDENTIALS_ENCRYPTION_KEY` | Required when deployed and must not be the development key. Any string works; it is stretched with SHA-256 into a Fernet key. | `secrets_guard.credentials_fernet_key` |
| `METRICS_TOKEN` | Required in production while `METRICS_ENABLED=true`. Otherwise set `METRICS_ENABLED=false`. | `config.py` |
| `MAIL_TLS_VERIFY` | `false` is refused in production. The default is `true`. | `config.py`, `app/core/tls.py` |
| `FRONTEND_URL` | A wildcard (`*`) origin is refused in production. | `config.py` |
| `MYSQL_HOST/PORT/USER/PASSWORD/DATABASE` | Needed to connect. `DATABASE_URL`, if set, takes precedence. | `config.py` |

Guards proven by `tests/unit/test_production_guards.py` and `tests/unit/test_secrets_guard.py`.

### 2.2 Operational settings

| Variable | Default | Effect |
|---|---|---|
| `ALLOW_SELF_SIGNUP` | unset: **off** in production, on elsewhere | Public registration creates a tenant and a SUPER_ADMIN (`settings.self_signup_enabled`). See DEC-11. |
| `ENABLE_DOCS` | false | `/docs`, `/redoc` and `/openapi.json` are off in production unless set to true. |
| `OCC_REQUIRE_VERSION` | unset (off) | When on, a guarded update that sends no `expected_version` / `If-Match` gets 428 `VERSION_REQUIRED`. When off, the update is accepted and logged as `Deprecated: ... updated without expected_version` (`app/core/concurrency.py`). The OCC tables are contacts, accounts, leads, opportunities, proposals, campaigns and sequence steps. When to switch it on is DEC-04. |
| `SESSION_IDLE_MINUTES` / `SESSION_ABSOLUTE_HOURS` / `REFRESH_REUSE_GRACE_SECONDS` | 30 / 12 / 30 | Checked when the refresh token is exchanged (ADR 0007). The effective idle sign-out comes 30-60 min after the last interaction, set by `ACCESS_TOKEN_EXPIRE_MINUTES` (30). |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | 30 / 7 | JWT access token (HS256) and opaque refresh token (stored as a SHA-256 hash). |
| `OPENAI_API_KEY` | empty | Without it, AI features fall back to built-in templates. |
| `OPENAI_MAX_USD_PER_DAY` | **0.20** (decided, DEC-12) | Worst-case cost is reserved in `platform_flags` (`llm_usd_micro:<UTC day>`) before each call. `0` blocks every call. |
| `OPENAI_MAX_CALLS_PER_DAY` / `OPENAI_MAX_OUTPUT_TOKENS` | 100 / 1500 | Call ceiling across all processes (`llm_calls:<day>`), and clamp on output tokens. |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` | - / - / us-east-1 | SES sending and SNS. |
| `AWS_SES_CONFIGURATION_SET`, `AWS_SNS_TOPIC_ARN`, `AWS_SNS_ENABLED` | empty / empty / false | SES event publishing. In production the SES webhook refuses calls when `AWS_SNS_TOPIC_ARN` is empty. |
| `SNS_ALLOWED_TOPIC_ARNS` | empty (any topic) | Optional comma-separated allow-list of SNS topics, on top of `AWS_SNS_TOPIC_ARN`. Signed messages older than 24 h (or 15 min in the future) are refused; when the signing certificate cannot be fetched the webhook answers 503 so SNS retries. |
| `MYSQL_SSL_MODE` / `MYSQL_SSL_CA` | `disabled` / empty | Database TLS for the API, worker and migrations (D8, off until DEC-01): `disabled`, `required` (encrypted, not verified), `verify_ca`, `verify_identity`. For RDS put `https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem` in the image or a mounted secret (e.g. `/etc/ssl/rds/global-bundle.pem`), set `verify_identity` and that path, and set the RDS parameter `require_secure_transport=ON`. Production logs a warning at start-up while it is `disabled`. |
| `SES_WEBHOOK_TOKEN` | empty | Shared secret for raw SNS delivery (`?token=` on the subscription URL). |
| `SENDER_EMAIL`, `SES_MAX_SEND_RATE` | - / 12 per second | |
| `AUTO_PAUSE_BOUNCE_RATE` / `AUTO_PAUSE_COMPLAINT_RATE` / `AUTO_PAUSE_MIN_SENDS` / `_MIN_BOUNCES` / `_MIN_COMPLAINTS` | 0.05 / 0.003 / 50 / 5 / 2 | Auto-pause thresholds (`app/services/send_safety.py`). |
| `DELIVERY_CONFIRM_MINUTES` | 15 | A sent email with no SES event after this long becomes `UNCONFIRMED`. |
| `DAILY_NEW_CONTACT_LIMIT` | 500 | New contacts per user per UTC day (BR-OV-01). |
| `UNSUBSCRIBE_SUPPRESSION_DAYS` | 180 | |
| `MS365_CLIENT_ID/SECRET/REDIRECT_URI`, `GOOGLE_CLIENT_ID/SECRET`, `GOOGLE_SYNC_REDIRECT_URI`, `GRAPH_REDIRECT_URI` | optional | Microsoft 365 mailbox OAuth, calendar and email sync. |
| `TRUSTED_PROXY_HOPS` | 1 | Which `X-Forwarded-For` entry counts as the client IP for rate limits. |
| `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE` | off / 0.0 | |
| `LOG_LEVEL`, `LOG_FORMAT` | INFO / text | `LOG_FORMAT=json` writes one JSON object per line. |

## 3. Health, readiness and metrics endpoints

| Process | Endpoint | Meaning | Probe |
|---|---|---|---|
| API :8001 | `GET /health` | The process is up. No dependencies are checked. Returns `status`, `app`, `version`. **The I-17 change is in progress** (see REVIEW_RESPONSES). | startup + liveness |
| API | `GET /health/ready` | `SELECT 1` succeeds, else 503 `database: unreachable` | readiness |
| API | `GET /metrics` | Prometheus. Needs `Authorization: Bearer $METRICS_TOKEN` when the token is set (401 otherwise). Off with `METRICS_ENABLED=false`. | scrape |
| Worker :8002 | `GET /health` | The process is up (`role: worker`) | startup + liveness |
| Worker | `GET /health/ready` | DB answers **and** no loop heartbeat is older than `max(3 × interval, 180 s)`, else 503 with `stale_loops` | readiness |
| Worker | `GET /metrics` | Prometheus for the worker process. **No token** on this port. It is reachable only inside the cluster, and NetworkPolicy blocks all ingress to the worker once applied (DEC-02). | scrape |

Health checks, `/webhooks/ses` and `/tracking` are exempt from the per-IP rate limit.

## 4. Logging and observability

Source: `app/core/observability.py`.

- **Request IDs.** Every request gets an `X-Request-ID`: the caller's value if it matches `[A-Za-z0-9._:-]{1,128}`, otherwise a new one. The ID is returned in the response header and added to every log line (`%(request_id)s`). Use it to find a user-reported error.
- **Log format.** Text: `ts LEVEL [request_id] logger: message`. Set `LOG_FORMAT=json` for log shipping. Request bodies are never logged: the 422 handler logs only the path and the error locations (`tests/test_auth.py::test_validation_error_does_not_log_body`).
- **Sentry** runs only when `SENTRY_DSN` is set. Request bodies, cookies, query strings, auth headers and `user` are stripped (`_scrub_event`, `tests/test_observability.py::test_sentry_scrubber_strips_sensitive_data`).
- **Metrics**

| Metric | Labels | Recorded by |
|---|---|---|
| `http_requests_total` | method, route (template), status | API middleware |
| `http_request_duration_seconds` (histogram) | method, route | API middleware |
| `emails_sent_total` | result (`sent`, `failed`, `bounced`, `blocked`, ...) | sender / scheduler |
| `ses_webhook_events_total` | type (Bounce, Complaint, Delivery, ...) | SES webhook |
| `imap_sync_runs_total` | result (`ok`, `error`) | IMAP sync |
| `job_last_success_timestamp` | job | worker loops, **only after a successful iteration** |

Metrics live in the process that records them. Scrape every API pod and the worker pod, and aggregate with
`sum`/`max` across pods.

## 5. Background worker and job locks (ADR 0004)

| Loop | Interval | Stale after |
|---|---|---|
| `scheduler` (sends email; also recovers stuck `SENDING`) | 30 s | 180 s |
| `imap_sync` | 120 s | 360 s |
| `deliverability` | 600 s | 1800 s |
| `warmup` | `WARMUP_INTERVAL_SECONDS` (900 s) | 2700 s |
| `crm_purge` (recycle-bin expiry, `PURGE_CONTACT`) | 6 h | 18 h |
| `send_safety` (auto-pause health cycle, delivery reconciliation) | 60 s | 180 s |
| `sales_jobs` | 60 s | 180 s |
| `contact_scoring` | 300 s | 900 s |
| `webhooks` (outbox delivery, ADR 0005) | 15 s | 180 s |

- Each iteration takes `GET_LOCK('vector:job:<name>:<database>', 0)` on its own NullPool connection. A replica that does not get the lock skips the iteration (`LOCK_SKIPS`). Scaling the worker is safe, but one iteration of each loop runs at a time across the fleet.
- If a lock holder crashes, MySQL frees the lock when the connection drops. Nothing needs cleaning up. To see who holds a lock: `SELECT IS_USED_LOCK('vector:job:scheduler:<db>')`, which returns the connection id (look it up in `SHOW PROCESSLIST`).
- `HEARTBEATS` are refreshed at the start and end of every loop pass, **including passes that failed**. So `/health/ready` catches a hung loop, not a failing one. Alert on `job_last_success_timestamp` for that (§10).
- The scheduler also claims each message with a unique `send_key` before sending, a second guard against double sends (BR-DF-06).
- Platform-wide send pause: `POST /scheduler/stop` / `POST /scheduler/start` (PLATFORM_ADMIN), stored in `platform_flags.scheduler_paused`.

## 6. Limits per process and per replica (P-4)

The manifests run **2 API replicas and 1 worker**. A counter held in process memory is counted separately by
each replica, so with N API replicas the **effective limit is the limit × N**. Shared counters (Redis) are
DEC-09. No load test has been run yet (DEC-18).

| Limit | Kept in | Configured limit | Effective, 2 API replicas (manifest) | Effective, N replicas |
|---|---|---|---|---|
| Global requests per client IP | API memory (`rate_limit.GLOBAL_PER_IP`) | 1200 / 60 s | 2400 / 60 s | 1200·N |
| Sign-in attempts per IP | API memory | 60 / 5 min | 120 / 5 min | 60·N |
| Wrong passwords per e-mail from one IP (lockout) | API memory (`LOGIN_FAILURES_PER_EMAIL_IP`) | 10 / 15 min | 20 / 15 min | 10·N |
| Wrong passwords per e-mail, all IPs (backstop) | API memory (`LOGIN_FAILURES_PER_EMAIL`) | 50 / 15 min | 100 / 15 min | 50·N |
| Reset / magic-link e-mails per address | API memory | 5 / 15 min | 10 / 15 min | 5·N |
| Reset / magic-link requests per IP | API memory | 30 / 15 min | 60 / 15 min | 30·N |
| Self sign-ups per IP | API memory | 10 / 1 h | 20 / 1 h | 10·N |
| MFA attempts per IP | API memory | 30 / 5 min | 60 / 5 min | 30·N |
| MFA failures per pending token / per user | API memory | 5 / 10 min; 10 / 15 min | 10; 20 | 5·N; 10·N |
| Daily new contacts per user (BR-OV-01) | `DailyLimiter` in memory, one per sending process: the worker scheduler, plus each API replica for "send now" (`campaign_send_now_service` uses the scheduler singleton of its own process). Each process seeds its counter from the DB count of today's first-step sends on its first reservation of the day, then counts locally | 500 / UTC day | up to 500 × 3 in the worst case (1 worker + 2 API replicas each seeded before the others sent) | up to 500 × (workers + API replicas) |
| OpenAI USD and calls per day | **MySQL** `platform_flags` (atomic UPDATE) | $0.20, 100 calls | $0.20, 100 calls (shared) | shared, exact |
| SES send rate | per sending process (`SES_MAX_SEND_RATE`) | 12 / s | 12 / s (only the worker sends on schedule) | 12 / s per sending process |
| Duplicate-KPI cache (`crm_kpis._DUP_CACHE`) | API memory, TTL 600 s | - | each replica caches separately; figures may differ for up to 10 min | - |
| Daily activity cache (`user_activity._recorded`) | API memory, 50,000 entries | one insert per user per day per process | one INSERT IGNORE per user per day **per replica** (harmless) | N inserts |
| Visible-team cache (`user._visible_user_ids`) | on the request's `User` object | - | lives for one request only; role and hierarchy changes apply on the next request | - |

Counters held in memory reset when a pod restarts. A restart therefore also clears a lockout early. The sign-in lockout rules are being reworked on this branch (review item R-10); the values above are those in `app/core/rate_limit.py` on 2026-10-18.

## 7. Backup and restore

The repository has **no backup configuration**. RDS is managed outside it. What to set up and do:

| Step | Action |
|---|---|
| Configure | Turn on RDS automated backups (retention as decided in DEC-14), point-in-time recovery (PITR) and deletion protection, and take a manual snapshot before each release ([RELEASE.md](RELEASE.md)). The snapshot carries the encrypted mailbox credentials: keep `CREDENTIALS_ENCRYPTION_KEY` safe with it, because the snapshot cannot decrypt them without it (§9.2). |
| Not backed up | Template attachment files (per-pod emptyDir, DEC-03). In-memory rate-limit counters. Prometheus metrics. |
| Restore (PITR) | 1. **Stop sending first.** `POST /scheduler/stop` (or `UPDATE platform_flags SET value='restore' WHERE name='scheduler_paused'`), then `kubectl scale deploy/backend-worker --replicas=0`. 2. Before restoring, if the old database is still readable, **export the erasure and unsubscribe records** made after the restore point (§8). 3. `aws rds restore-db-instance-to-point-in-time` into a new instance. 4. Point `MYSQL_HOST` in `sales360-secret` at the new instance and restart `backend` (the initContainer runs any pending migrations). 5. Re-apply erasures and unsubscribes (§8, **mandatory**). 6. Review messages in `QUEUED`/`SCHEDULED`/`SENDING` whose send time falls between the restore point and the incident: they may already have gone out through SES, so the restored rows do not know it. Cancel them or confirm them against SES before resuming. 7. Scale the worker back to 1 and `POST /scheduler/start`. |
| Restore drill | **Not run yet.** It is a go-live blocker (DEC-18). |

## 8. Re-applying GDPR erasures and unsubscribes after a restore (D-13)

**Not implemented: there is no erasure ledger outside the database. See DEC-13.** A restore brings back every
contact erased after the restore point. It also brings back the subscriptions of everyone who unsubscribed
after it. Today the only record of either lives in the database itself:

| Record | Table | Contents |
|---|---|---|
| Erasure / purge | `audit_logs` | `action` = `ERASE_CONTACT` (single-contact erasure, `DELETE /prospects/{id}` in `prospect_list_router`) or `PURGE_CONTACT` (recycle-bin purge by a user or the `crm_purge` job). `entity_id` holds the prospect id. No personal data is stored. |
| Unsubscribe / suppression | `global_unsubscribes` | `tenant_id`, `email`, `unsubscribed_at`, `reason`, `suppression_expires_at` |

Manual procedure. It works only while the pre-restore database (or a later snapshot of it) can still be read:

1. Against the **old** database, export `SELECT tenant_id, entity_id, action, created_at FROM audit_logs WHERE action IN ('ERASE_CONTACT','PURGE_CONTACT') AND created_at >= :restore_point` and `SELECT * FROM global_unsubscribes WHERE unsubscribed_at >= :restore_point`.
2. Against the **restored** database: re-insert the suppression rows (`INSERT IGNORE`). Then, in a `backend` pod, re-run the erasure for the exported prospect ids through the same code path the API uses: `python -c "import app.models; from app.core.database import SessionLocal; from app.services.contact_service import delete_contacts; db=SessionLocal(); delete_contacts(db, IDS, actor_id=None, reason='ERASE_CONTACT'); db.commit()"`. This also scrubs import-error rows, webhook payloads and notifications (D-12).
3. Record the re-apply in the incident log.

If the old database is lost (a disaster rather than a rollback), erasures and unsubscribes made after the last
backup **cannot be recovered**. Only SES's account-level suppression list (bounces and complaints, if that list
is enabled) survives. This residual risk is DEC-13 / DEC-14.

## 9. Key and credential rotation

### 9.1 `JWT_SECRET_KEY` (HS256, one key)

There is no key list, so rotation is immediate:

- Every access token and pending MFA token becomes invalid at once. Refresh tokens are opaque (stored as a SHA-256 hash), so they **keep working**: browsers get a new access token on their next refresh and stay signed in. To force everyone to sign in again, also revoke the refresh tokens: `UPDATE refresh_tokens SET revoked_at = NOW(), revoked_reason = 'rotation' WHERE revoked_at IS NULL` (`revoked_reason` is `String(16)`).
- Click-tracking signatures (`security.sign_tracking_url`) use the same key. Links in mail already sent still redirect when the URL appears in the stored email body. Otherwise the user sees the "Leaving this site" page.
- Procedure: generate the key with `python -c "import secrets; print(secrets.token_urlsafe(48))"`, update `sales360-secret`, then `kubectl rollout restart deploy/backend deploy/backend-worker`.

### 9.2 `CREDENTIALS_ENCRYPTION_KEY` (Fernet, one key)

**Rotating it without losing data is not implemented.** `app/core/encrypted_type.py` uses a single `Fernet`
(no `MultiFernet`, no key id in the stored `enc:v1:` prefix). After a change, every value that cannot be
decrypted reads as `None` and logs `Stored credential could not be decrypted`. The key protects:

| Column | Effect of a changed key |
|---|---|
| `sending_inboxes.smtp_password`, `imap_password` | Mailboxes stop sending and syncing until the passwords are entered again |
| `sending_inboxes.oauth_refresh_token`, `oauth_access_token` | Microsoft 365 mailboxes must be reconnected |
| user calendar/email connections `access_token`, `refresh_token` (`app/models/sales_extra.py`) | Users must reconnect Google / Microsoft |
| `users.mfa_secret` | **Users with MFA cannot complete sign-in** until an admin resets their MFA |
| `webhook_endpoints.secret` | The secret reads as empty, so receivers can no longer verify signatures. Rotate each endpoint's secret (`POST` rotate, `tests/test_webhooks.py::test_wh_f18_rotate_secret`) |

Today the only safe options are: (a) do not rotate unless the key is compromised; (b) on compromise, change
the key and then have every tenant re-enter mailbox passwords, reconnect OAuth, reset MFA and rotate webhook
secrets. A re-encryption command (`MultiFernet([new, old]).rotate`) is DEC-19.

### 9.3 SES, SNS and IMAP/SMTP credentials

| Credential | Procedure |
|---|---|
| AWS access key (SES/SNS) | Create a second key on the IAM user, update `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in the secret, restart `backend` and `backend-worker`, watch `emails_sent_total{result="failed"}`, then deactivate the old key. |
| `SES_WEBHOOK_TOKEN` | Only one value is accepted. Update the secret and the SNS subscription URL `?token=` together, in one change window. Notifications that arrive in between are refused (SNS retries them). |
| Mailbox SMTP/IMAP passwords | Tenant users change them on the inbox screen. A blank password keeps the saved one (`tests/test_inbox_credentials.py`). |
| `METRICS_TOKEN` | Update the secret and the Prometheus scrape config together. Restart the API. |
| `MYSQL_PASSWORD` | Change it in RDS, then update the secret and restart all pods. The pool recycles connections every 300 s. |

## 10. Alerting

Recommended Prometheus alerts on the metrics that exist (§4):

| Alert | Expression (sketch) | Severity |
|---|---|---|
| API 5xx rate | `sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 0.02` | page |
| API latency | `histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) > 2` | warn |
| Rate-limit storm | `sum(rate(http_requests_total{status="429"}[5m])) > 5` | warn |
| Scheduler not succeeding | `time() - max(job_last_success_timestamp{job="scheduler"}) > 300` | page |
| Any loop not succeeding | `time() - max by (job) (job_last_success_timestamp) > 3 * <interval>` (§5) | warn |
| Worker not ready | kube-state `kube_pod_status_ready{pod=~"backend-worker.*"} == 0` for 5 min | page |
| Send failures | `sum(rate(emails_sent_total{result="failed"}[15m])) / sum(rate(emails_sent_total[15m])) > 0.1` | warn |
| Bounce/complaint spike | `sum(rate(ses_webhook_events_total{type=~"Bounce|Complaint"}[15m]))` well above its baseline | warn |
| SES webhook silent | `sum(increase(ses_webhook_events_total[1h])) == 0` while `emails_sent_total` rises | warn |
| IMAP sync failures | `sum(rate(imap_sync_runs_total{result="error"}[30m])) / sum(rate(imap_sync_runs_total[30m])) > 0.5` | warn |
| Readiness flapping | `kube_pod_container_status_restarts_total` increasing for `backend` | warn |

**Not instrumented** (log lines only): auto-pause events (`[AutoPause]`), LLM cap reached (`[LLM] daily cap
reached`), interrupted sends (`[Scheduler] Interrupted sends`), stale-version 409s. Alert on these through
log-based alerting until metrics are added.

## 11. Incident playbooks

### 11.1 SES bounce or complaint spike / auto-pause

- **What the code does.** `send_safety.check_campaign_health` runs inside the SES webhook on each bounce or complaint. It pauses a campaign at a hard-bounce rate of 5% or a complaint rate of 0.3% (from 50 sends; before that, at 5 hard bounces or 2 complaints). `check_domain_health` pauses every campaign of that tenant sending from the domain. The hourly `run_health_cycle` re-checks rolling 24 h rates. On a pause: campaign `status=PAUSED`, `paused_reason="Paused automatically: ..."`, its queued emails become `PAUSED_BY_CAMPAIGN`, a `CampaignStateEvent` is written, a webhook event is emitted and an in-app notification goes to the owner and admins. Hard bounces are added to `global_unsubscribes` (`AUTO_SUPPRESS_HARD_BOUNCES=true`).
- **Do:** 1. Find the campaigns: `SELECT campaign_id, tenant_id, paused_reason FROM campaigns WHERE paused_reason LIKE 'Paused automatically%'`. 2. Check the SES account's reputation dashboard. SES reviews accounts at 5% bounce / 0.1% complaint, and its 0.1% is stricter than the app's 0.3%. 3. Have the tenant clean the list (invalid addresses, a stale import). 4. Resume with `POST /campaigns/{id}/resume` (MANAGER or above). If the rates are still over the limits, the next event pauses the campaign again. 5. If the whole account is at risk, `POST /scheduler/stop`.

### 11.2 Emails stuck in `SENDING`

- **What the code does.** Each scheduler pass runs `_recover_stuck_sends`. A message claimed more than 15 min ago (`STUCK_SENDING_MINUTES`) with no `sent_at` is set to **SENT** when there is evidence that SES accepted it (an SES or provider message id, a delivery time, or a provider event). Otherwise it is set to **FAILED** (`failure_reason` "outcome is unknown; not retried"). It is never re-sent automatically (BR-DF-06).
- **Do:** check that the scheduler loop is succeeding (§10). Rows older than 15 min that are still `SENDING` mean the scheduler is not running or holds no lock: check `/health/ready` on the worker and `IS_USED_LOCK`. Re-queue a FAILED message only after confirming in SES that it was not delivered.

### 11.3 IMAP sync failures

- **What the code does.** Each inbox syncs with no database transaction open during IMAP I/O (T-1), and each message commits on its own, so a failure keeps the messages already stored. The failure text goes to `sending_inboxes.imap_last_error` and `imap_sync_runs_total{result="error"}`. TLS is always verified. A certificate error or a missing STARTTLS shows as a readable error.
- **Do:** `SELECT inbox_id, email, imap_last_error, last_sync_at FROM sending_inboxes WHERE imap_last_error IS NOT NULL`. Typical causes: a changed password (§9.3), an expired Microsoft 365 OAuth grant (reconnect), a self-signed server certificate (not accepted in production), a non-standard port blocked by `backend-egress` (add the port to the NetworkPolicy), a rotated `CREDENTIALS_ENCRYPTION_KEY` (§9.2).

### 11.4 LLM budget exhausted

- **What the code does.** When the reservation would pass `OPENAI_MAX_USD_PER_DAY` ($0.20) or `OPENAI_MAX_CALLS_PER_DAY` (100), the call raises `LLMBudgetExceeded` and the feature falls back to the built-in templates, as if no key were set. The counters reset at 00:00 UTC (the key is per day).
- **Do:** check the spend with `SELECT name, value FROM platform_flags WHERE name LIKE 'llm_%'` (micro-dollars, calls). Raising the cap is a client decision (DEC-12). Change `OPENAI_MAX_USD_PER_DAY` in the secret and restart. Do not edit the counters to "top up".

### 11.5 Tenant suspension

- **What the code does.** `PUT /api/admin/tenants/{id}/status` `{"status": "SUSPENDED"}` (PLATFORM_ADMIN). `get_current_user` then refuses every API request for that tenant's users (`BLOCKED_TENANT_STATUSES`, `tests/test_auth.py::test_suspended_tenant_blocked`). Since 1.1.0 the scheduler and warmup skip suspended / deleted tenants: their scheduled emails stay SCHEDULED and resume on reactivation (`tests/test_suspended_tenant_sending.py`). IMAP sync and webhook delivery still run. No audit row is written, only an INFO log line.
- **Do:** sending stops automatically. To also stop IMAP sync, pause the tenant's campaigns in SQL (`UPDATE campaigns SET status='PAUSED', paused_reason='Tenant suspended' WHERE tenant_id=:t AND status='ACTIVE'` and `UPDATE email_messages SET status='PAUSED_BY_CAMPAIGN' WHERE status IN ('QUEUED','SCHEDULED') AND campaign_id IN (...)`), or use `POST /scheduler/stop` for everyone. Record who suspended the tenant and why in the incident log. Reactivate with `{"status": "ACTIVE"}` and resume the campaigns.

### 11.6 Pods fail to start after a configuration change

Look at the first log lines for the guard message: `ENVIRONMENT is not set`, `JWT_SECRET_KEY is missing...`,
`CREDENTIALS_ENCRYPTION_KEY is not set`, `METRICS_TOKEN is required`, `MAIL_TLS_VERIFY=false ... refused`,
`FRONTEND_URL contains a wildcard`. The fix is in the secret (§2.1). `Read-only file system` means a new
write path needs an emptyDir volume ([RELEASE.md](RELEASE.md) step 2).

## 12. Release and upgrade

Follow the checklists in `CHANGELOG.md` and [RELEASE.md](RELEASE.md), in this order:

1. Refresh the image pins in both repositories (`scripts/pin_images.py`) and run the tests.
2. Bump `VERSION` and the frontend `package.json`/`package-lock.json`, and date both `CHANGELOG.md` entries (CI release-identity gate).
3. All CI gates green with no new baseline entries. Run the live-stack suites (`workflow_dispatch`, layer `all`) on the release candidate and attach the results.
4. `alembic heads` returns a single head. Name any migration that locks large tables. The 1.1.0 migrations are `0013`-`0032`. `0032_server_defaults` sets 23 column defaults that databases built fresh lacked (metadata only, no table copy; a column with a different default is left alone with a warning; downgrade is a no-op). `0028_child_tenant_ids` adds `tenant_id` to 14 child tables (messages, events, enrolments, templates, list members and others), backfills it from the parent in committed 10k-row batches (a rerun resumes) and adds tenant-first indexes and tenant-scoped keys (composite FKs from contact links and list members, one primary company per contact); a key that existing data breaks is skipped with a warning naming the statement to run later. `0029_status_checks` adds CHECK constraints on status columns: MySQL copies each table to add them (writes wait), so on a large `email_messages` run it in a maintenance window; a column holding a value outside its vocabulary keeps no CHECK and the warning lists the values. `0030_contact_fulltext` builds a FULLTEXT ngram index on `prospects.search_text` (writes to `prospects` wait while it builds: about 7 s per 100k contacts). It needs `ngram_token_size=2` (the default; set in the RDS parameter group) and is built with `innodb_ft_enable_stopword=OFF`, which the app and Alembic set on every connection: an `OPTIMIZE TABLE prospects`, a copying `ALTER` on prospects or a mysqldump restore from a plain client must run `SET SESSION innodb_ft_enable_stopword=OFF` first, or names such as "maria" stop matching. `0031_property_safety` adds `is_active`, `is_system` and `max_length` to `contact_field_definitions`. `0022`-`0027` add columns and tables only (campaign source, reply labels, company properties and backfill runs, contact identifiers / duplicate pairs / merge and export logs and a unique list name per workspace, background import jobs, business-day waits); `0025` skips the unique list-name index with a warning when a workspace already has two lists with the same name (rename them, then re-run it) and `0024`'s downgrade refuses while company properties or backfill runs exist. Background imports need the API and the worker to share `IMPORT_STORAGE_DIR` (like attachments; G4). `0019_unique_active_email` adds a unique live e-mail per account; when two live accounts already share an e-mail it adds the column, logs a warning and skips the index (find them: `SELECT LOWER(email), COUNT(*) FROM users WHERE status IN ('ACTIVE','INVITED') GROUP BY 1 HAVING COUNT(*) > 1`; resolve, then re-run 0019 with `alembic downgrade 0018_datetime6 && alembic upgrade head`). `0020_send_integrity_keys` does the same for two live e-mails on one campaign step. `0021_automation_rule_runs` adds a table. `0018_datetime6` turns every TIMESTAMP column into DATETIME(6) (UTC, past 2038): it rebuilds every table, so plan a maintenance window on a large database, and it fails on zero dates (`0000-00-00`) under strict mode, so check for them first. `0017_one_collation` converts any table not on `utf8mb4_0900_ai_ci` (`ALTER TABLE ... CONVERT TO` rebuilds it; on a database already uniform it changes nothing: check first with `SELECT table_name, table_collation FROM information_schema.tables WHERE table_schema = DATABASE()`). `0016_optimistic_concurrency` adds `row_version` to seven tables, and `0015_session_limits` alters `refresh_tokens` (ADR 0007: it backfills, so nobody is signed out).
5. Take a manual RDS snapshot.
6. Check that `sales360-secret` has `ENVIRONMENT`, `JWT_SECRET_KEY`, `CREDENTIALS_ENCRYPTION_KEY`, `SES_WEBHOOK_TOKEN` and, in production, `METRICS_TOKEN`.
7. Merge to `dev`/`stage`. CICD builds the image, deploys it by digest, applies the NetworkPolicies (if enabled) and waits for the rollout. The backend initContainer runs the migrations.
8. Tag both repositories `v<VERSION>` on the deployed commits.
9. After the deploy: `/health/ready` is 200 on both workloads, `job_last_success_timestamp` moves for every loop, and a test send and SES event arrive.

## 13. Rollback

| Case | Procedure |
|---|---|
| Code only, no new migration | `kubectl rollout undo deploy/backend deploy/backend-worker`, or redeploy the previous digest. |
| The release added migrations | **Downgrade first, with the new image**, then roll back. The old image's initContainer runs `alembic upgrade head` and fails on a database revision it does not know (`Can't locate revision`), so the pods would not start. Run it in a pod of the **current** image: `alembic downgrade <previous head>` (for example `0012_contact_scoring` before 1.1.0's `0013`-`0016`). Then deploy the old image. |
| Downgrade caveats | `0001_baseline` and `0005_tenant_sending_domains` raise `NotImplementedError`: restore from a backup instead. The other downgrades drop the tables and columns they added, so **data written to them since the upgrade is lost**: `row_version`, session fields, the KPI tables, revoked access tokens, webhooks, contact scoring and so on. Downgrading `0015` drops session tracking. A database restore instead of a downgrade needs §7 and §8. |
| Frontend | The web image rolls back on its own (static nginx). Keep the frontend and backend on the same release number. A 1.1.0 web client against an older API would send `If-Match` / `expected_version` and `X-User-Activity`, which older APIs ignore. |
