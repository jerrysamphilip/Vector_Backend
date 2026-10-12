# E2E journeys - real code paths, local stand-ins

These suites remove the fakes of the earlier system tests (no more "mark the email SENT in the DB"): the **worker** really
sends through **SES** (a moto server), SES events come back through the **webhook**, replies arrive through the **IMAP sync** from a
fake mailbox server, and the hourly jobs are run on demand with `docker exec`.

## Stand-ins

| Piece | What it is | How it is wired |
|---|---|---|
| SES | `moto` server in container `ses-mock` (image `vector-ses-mock`, `Dockerfile.ses-mock`) | `api` + `worker` get `AWS_ENDPOINT_URL=http://ses-mock:5000`, `AWS_ACCESS_KEY_ID/SECRET=test`, `AWS_REGION=us-east-1`, `SENDER_EMAIL=platform@e2e-platform.example`, `SEND_ON_ANY_DAY=true` (botocore honours `AWS_ENDPOINT_URL`; no app change needed). Host port 5001. |
| App image | `vector-api:e2e`, built from the current tree (the prebuilt `:latest` predated `SEND_ON_ANY_DAY` and later fixes) | `docker-compose.e2e.yml` points `migrate`, `api`, `worker` at it |
| IMAP | `FakeImap` (TLS, self-signed) from `system_tests/outreach/helpers.py`, run by the test process, reached from the containers at `172.18.0.1` | inbox rows carry `imap_host/port` |
| SNS | raw SES events POSTed to `/webhooks/ses/notifications?token=<SES_WEBHOOK_TOKEN>` (also the unsigned `{Message: json}` envelope). Real SNS signatures cannot be produced locally. |
| Clock | `travel_due()` moves `scheduled_at` of one campaign (own tenant) to "now" instead of waiting days / for a business day. The scheduler, windows, pacing, claims and re-planning are all real. |
| Jobs | `run_job('sales_jobs'|'send_safety'|'deliverability'|...)` runs one iteration in a fresh process in `vector-api-1` (a fresh process also triggers the hourly parts). |

The SES stand-in is shared by everything on the stack. The harness verifies every sender identity/domain it uses in moto
(`ses_verify`) and always filters captured mail by its own unique recipients/domains.

## Bring up / down

```bash
cd /home/user/Vector_Backend
SC=<scratchpad dir with ca.crt and Dockerfile.sandbox>
docker build -f $SC/Dockerfile.sandbox --build-context certs=$SC -t vector-api:e2e .
docker build -f system_tests/e2e/Dockerfile.ses-mock --build-context certs=$SC -t vector-ses-mock system_tests/e2e
FRONTEND_DIR=../Vector_Frontend docker compose -f docker-compose.yml -f $SC/override.yml \
    -f system_tests/e2e/docker-compose.e2e.yml up -d --no-build ses-mock api worker
# down (back to the plain stack):
FRONTEND_DIR=../Vector_Frontend docker compose -f docker-compose.yml -f $SC/override.yml up -d --no-build api worker
docker rm -f vector-ses-mock-1
```
(moto state is in memory: restarting `ses-mock` forgets identities and captured mail; the harness re-verifies what it needs.)

## Run

```bash
python3 -m system_tests.e2e.run_journeys                # all journeys (~45-60 min on a busy stack)
python3 -m system_tests.e2e.run_journeys --only J2,J3   # subset
```
`run_journeys` first checks that moto answers, that `api` and `worker` carry `AWS_ENDPOINT_URL`/`SEND_ON_ANY_DAY`, and that the API is up;
otherwise it exits with code 2 and prints the commands above. Results: `results.md`, `results.json`, `run.log`.

## Files
`helpers.py` (SES capture + verify, link following, `run_job`, TOTP, event builders, patient HTTP), `flows.py` (API flows: inbox, upload,
campaign, test clock), `j1..j6_*.py`, `JOURNEYS.md`, `FINDINGS.md`.
