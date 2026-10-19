# Release checklist (deployment hardening)

Run before merging to `dev` / `stage` for a release. CICD.yaml re-checks the parts marked (CI).

1. **Refresh image pins (P-2).** Resolve every base / third-party image tag to its current digest and
   commit the result:

   ```bash
   python3 scripts/pin_images.py                          # rewrites FROM / image lines as name:tag@sha256:...
   PIN_MIRROR=mirror.gcr.io python3 scripts/pin_images.py # same, when Docker Hub rate-limits (same digests)
   python3 scripts/pin_images.py --check                  # (CI) fails on any line without @sha256
   ```

   Covers `Dockerfile`, `system_tests/**/Dockerfile*`, `docker-compose.yml` (local dev, pinned anyway)
   and `manifests/`. `<ECR_IMAGE_URI>` stays a placeholder: CI replaces it with the pushed image **by
   digest** and checks the rendered manifest with `--check --strict`. Rebuild and run the tests after a
   refresh: a new digest is a new base image. Do the same in Vector_Frontend.
2. **Container hardening (P-1)** (CI, `tests/unit/test_manifests.py`): every container non-root,
   `readOnlyRootFilesystem`, `capabilities.drop: [ALL]`, no privilege escalation, seccomp
   `RuntimeDefault`; writable paths are emptyDir volumes (`/tmp`, `/app/uploads`). A new path the app
   writes to needs a volume, or the pod fails with `Read-only file system`. Namespaces enforce Pod
   Security `restricted` (`manifests/namespace.yaml`, applied by a cluster admin).
3. **Internal transport (P-3)**: `manifests/networkpolicy.yaml` is applied when the repository variable
   `APPLY_NETWORK_POLICIES=true`; MySQL TLS status in [adr/0008](adr/0008-internal-transport.md).
4. **Smoke test a read-only container locally**:

   ```bash
   docker build -t vector-api:rc .
   docker run --rm --read-only --tmpfs /tmp --tmpfs /app/uploads:uid=10001,gid=10001 --cap-drop ALL \
     --security-opt no-new-privileges:true -e ... vector-api:rc python -m app.db.migrate
   ```
