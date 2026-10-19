# CI gates (NTS Build Standard Addendum)

Every gate below is **blocking** in CI. Where the code base could not pass a rule on the day the gate was
introduced (2026-10-18), the gate enforces a checked-in, dated **baseline** instead of a lower threshold.

## Ratchet policy

- A baseline lists today's violations, nothing more. The rule's threshold is never lowered globally.
- Baselines **only shrink**. Each gate fails on a *new* violation, on a baselined item getting *worse*, and on a
  baseline entry that no longer matches the code (stale entries must be deleted in the same change that fixes
  them, so the slot cannot be reused by a later regression).
- Regenerating a baseline (`--update`) refuses to add new or worse entries. Adding one anyway is an exception:
  it needs a reviewer's approval in the pull request and a reason next to the entry (or a TODO in
  `.importlinter`).
- Ignore lists for dependency audits carry an id, the date added, a review-by date and a reason; an entry past
  its review date fails the gate.

## Backend (`.github/workflows/ci.yml`)

| Rule | Gate | Job / step | Baseline / config | Baseline size (2026-10-18) |
|---|---|---|---|---|
| C-1 complexity | `scripts/ci/check_complexity.py` (radon): no function over cyclomatic complexity 20; baselined functions may not rise; average < 5 | unit | `ci/complexity_baseline.json` (function -> score; `average_ceiling`) | 120 functions; average 5.24 (ceiling 5.24, target < 5) |
| C-2 typing | mypy (`mypy.ini`, unchanged) plus `scripts/ci/check_type_ignores.py`: count of `# type: ignore` in app/ may not grow | unit | `ci/type_ignore_baseline.txt` | 35 |
| C-3 no `assert` in app code | ruff `S101` for `app/**` (`ruff.toml`; test code exempt) and `bandit -r app -t B101` | unit | none (0 hits) | 0 |
| C-4 layering | `lint-imports`, contract "layered architecture": main/worker > jobs > routers / db > services > schemas / prompts > utils > models > core | unit | `.importlinter` `ignore_imports` (BASELINE section, each with a TODO) | 7 imports |
| C-5 forbidden imports | `lint-imports`: app.core must not import services/routers/jobs/db; nothing below routers imports app.routers | unit | `.importlinter` | 2 imports (app.core.auth -> services) |
| C-5 no cycles | `lint-imports` `acyclic_siblings` on `app` (sub-packages and modules inside them) | unit | `.importlinter` | 11 imports |
| C-5 local imports | `scripts/ci/check_local_imports.py` (AST): every function-local import in app/ is registered as `path:function:module` | unit | `ci/local_imports_baseline.txt` (the register of intentional / pending local imports) | 317 entries |
| Traceability | `python scripts/test.py trace --check`: every requirement with status Fixed / Decision / Change request has a test case in a catalogue (`system_tests/*/TEST_CASES.md`) or a suite script; static, no results needed | unit | `docs/REQUIREMENTS.md` | 92 requirements, 88 in scope, 4 out of release |
| Release identity | `scripts/ci/check_version.py`: `VERSION` is SemVer, is the newest `CHANGELOG.md` section, not hard-coded in config, app images in `manifests/` are the placeholder or tagged with it, docs mention no other release | unit | `VERSION` (1.1.0) | - |
| I-11 fail closed | `tests/unit/test_production_guards.py` (subprocess start-ups): default/missing JWT secret, `MAIL_TLS_VERIFY=false`, missing `ENVIRONMENT` in Kubernetes, wildcard CORS origin and missing `METRICS_TOKEN` are refused in production; `/docs` off and self sign-up off by default | unit (unit layer) | - | - |
| Route consistency | `system_tests/contract/check_contract.py`: every frontend API call has a backend route with that method (OpenAPI generated from the commit) | contract | needs read access to Vector_Frontend: secret `FRONTEND_REPO_TOKEN` (contents:read), else the workflow token (public repo only); without access the job passes with a **warning annotation** | 0 unmatched |
| Dependency audit | `scripts/ci/run_pip_audit.py` (pip-audit on requirements.txt) | test | `ci/pip_audit_ignore.txt` (id, package, added, review by, reason) | 2 (python-jose, ecdsa; review 2026-12-31) |
| Secrets | gitleaks | secrets | `.gitleaks.toml` | - |

Run them all locally from the repo root:

```bash
pip install -r requirements.txt -r requirements-dev.txt
ruff check . && mypy && bandit -q -r app -t B101 && lint-imports
python scripts/ci/check_complexity.py && python scripts/ci/check_type_ignores.py
python scripts/ci/check_local_imports.py && python scripts/ci/check_version.py
python scripts/test.py trace --check && python scripts/ci/run_pip_audit.py
python -m pytest -q tests/unit
```

After shrinking a baseline: `python scripts/ci/check_complexity.py --update`,
`python scripts/ci/check_type_ignores.py --update`, `python scripts/ci/check_local_imports.py --update`, or
delete the line from `.importlinter` / an ignore list by hand.

## Frontend (`Vector_Frontend/.github/workflows/ci.yml`)

| Rule | Gate | Baseline / config | Baseline size (2026-10-18) |
|---|---|---|---|
| Release identity | `npm run version:check` (`scripts/check-version.mjs`): `package.json` == `package-lock.json` == newest `CHANGELOG.md` section == app images in `manifests/` | `package.json` version 1.1.0 | - |
| Lint | `npm run lint` (ESLint, errors block) | `eslint.config.js` | - |
| W-13 help anchors | `src/help/helpAnchors.test.js` (Vitest, in `npm run test`): every in-app route in `App.jsx` has a help article naming it, every article route exists, related links, hard-coded article ids and `See "..."` references resolve to an article or heading | exceptions listed with a reason in the test (pre-login screens, `/admin`, `/app` index redirect, dev-only screen) | - |
| Build | `npm run build` | - | - |
| Dependency audit | `npm run audit:check` (`scripts/check-audit.mjs`): `npm audit` high/critical advisories | `ci/npm_audit_ignore.txt` (GHSA id, package, added, review by, reason) | 35 advisories in 11 packages; runtime ones review by 2026-11-15, build-only 2026-12-31. 34 are fixed by `npm audit fix` (no `--force`): pending upgrade |

## Requirement status (docs/REQUIREMENTS.md)

`Fixed` and `Decision` and `Change request` are in scope and need a test case; `Out of release` does not.
Moving a requirement out of the release needs product-owner sign-off, recorded in its Notes column and in
CHANGELOG.md. Requirements referenced by a case but missing from the register fail the gate.
