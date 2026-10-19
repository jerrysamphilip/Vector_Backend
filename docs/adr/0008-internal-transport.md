# ADR 0008: Internal transport (MySQL TLS and NetworkPolicies)

Status: Proposed (2026-10). Needs an owner decision on option A vs B; the controls of option B are in
place either way.

## Context

Rule P-3: in-cluster transport is TLS, or an accepted ADR with compensating controls (NetworkPolicies).

- **API / worker -> MySQL (RDS)** is plaintext. `app/core/database.py` passes no `ssl` connect args and
  the URL built from `MYSQL_*` has no `ssl_*` parameters, so credentials and every query (contacts,
  mailbox credentials ciphertext, tokens) cross the VPC unencrypted. RDS accepts TLS but does not
  require it unless the parameter group sets `require_secure_transport=ON`.
- **Browser -> web / API** terminates TLS at the load balancer; LB -> pod hops are plain HTTP inside
  the VPC. The web pods serve static files only (nginx has no upstream in `nginx.conf`), so there is no
  web -> API hop inside the cluster.
- **API / worker -> external services** (SES/SNS/STS, OpenAI, Microsoft Graph, Google, Sentry) are HTTPS;
  SMTP/IMAP use implicit TLS or STARTTLS with certificate verification (`MAIL_TLS_VERIFY`, refused off
  in production).
- No NetworkPolicy existed: any pod in the cluster could reach the API and worker pods, and the pods
  could reach anything, including instance metadata.

## Options

**A. MySQL over verified TLS (recommended).** Ship the RDS CA bundle (`global-bundle.pem`, from
`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem`) in the image or a ConfigMap and
connect with `connect_args={"ssl": {"ca": <path>}}` with hostname checking (PyMySQL: `ssl_ca`,
`ssl_verify_cert`, `ssl_verify_identity`). Smallest code change: an optional `MYSQL_SSL_CA` setting read
in `database.py`, required when deployed. Interim without code: `DATABASE_URL` (it wins over
`MYSQL_*`, `app/core/config.py`) can carry `?ssl_ca=/etc/rds/global-bundle.pem`. Then set
`require_secure_transport=ON` on the RDS parameter group so a misconfigured client fails closed.
Cost: a CA file to rotate with AWS's bundle; a few ms of handshake per new pooled connection
(`pool_recycle=300`).

**B. Compensating controls only.** Keep plaintext MySQL inside the VPC and rely on:
`manifests/networkpolicy.yaml` (default-deny ingress for the namespace; API reachable on 8001 only
from the ingress controller, the web pods and monitoring; worker unreachable; API/worker egress
limited to DNS, 3306, 443, 465/587, 993/143, metadata service excluded), the RDS security group
admitting only the node / pod security group, and VPC isolation. Leaves the DB credentials and data
readable by anything that can sniff or spoof inside the VPC.

## Decision

Adopt **B now** as the documented compensating control (NetworkPolicies added, applied by `CICD.yaml`
once the `APPLY_NETWORK_POLICIES` repository variable is `true`), and **A as the target**: it is
the only option that meets P-3 without relying on network placement. LB -> pod HTTP stays as is
(covered by the same policies); revisit if a service mesh is introduced.

## Consequences

- Before enabling the policies, confirm the ingress source in `backend-ingress` / `frontend-ingress`
  (namespace `ingress-nginx`, or the ALB subnet CIDR for the AWS Load Balancer Controller in IP mode)
  and narrow the 3306 egress to the RDS subnet CIDR. A wrong ingress peer takes the app offline.
- The CNI must enforce NetworkPolicy (EKS VPC CNI with `enableNetworkPolicy=true`, Calico or Cilium);
  otherwise the objects are accepted and silently ignored.
- A mailbox on a non-standard SMTP/IMAP port is blocked until that port is added to `backend-egress`.
- `tests/unit/test_manifests.py` fails if the NetworkPolicies or the default deny disappear.
- When option A lands, record it in a new ADR that supersedes this one.
