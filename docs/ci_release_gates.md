# CI and Release Gates

`.github/workflows/ci.yml` is the only automated quality gate in the repository.
It runs on an ephemeral GitHub-hosted runner, uses synthetic credentials only,
and never deploys, provisions cloud resources, or contacts a payment provider.

Two things it deliberately does **not** do:

- It does not prove a deployment is safe. Deployment suitability is decided by
  the fail-closed configuration rules (`docs/staging_deployment.md`), which CI
  exercises but cannot satisfy on the operator's behalf.
- It does not audit transitive dependencies for vulnerabilities. It enforces
  *determinism* (exact pins plus a matching lockfile), not *freshness*.

---

## 1. Jobs and what they gate

| Job | Runner | Blocks a release when |
| --- | --- | --- |
| `backend` | `ubuntu-latest`, Python from `.python-version`, `postgres:16-alpine` **and** `postgres:18-alpine` matrix | any check, migration drift, test, schema or deploy-check regression fails on either major |
| `frontend` | `ubuntu-latest`, Node 22, pnpm 12.10.1 | lint, types, unit tests, or the production build fail |
| `browser-e2e` | `ubuntu-latest`, both toolchains, its own `postgres:16-alpine` service (staging-aligned) | any of the 10 real-HTTP browser journeys fail |

`browser-e2e` declares `needs: [backend, frontend]`, so it only spends runner
minutes once the cheap gates are green.

Triggers: pushes to `main` and `feat/**`, every pull request, and manual
`workflow_dispatch`. The workflow token is `contents: read`; a concurrency group
cancels superseded runs on the same ref.

### 1.1 Backend job (`backend`)

The backend job runs **once per PostgreSQL major** in its matrix (16 and 18).
PostgreSQL 16 is the staging target; 18 is the next major the application must
survive. Both legs already passed locally before the matrix was written, so the
matrix records a verified fact rather than searching for a green cell, and
`fail-fast: false` keeps both reports even if one regresses.

In order:

1. `pip install -r requirements.txt` (exact pins; pip download cache keyed on the
   requirements file).
2. **Database isolation guard** — boots Django and asserts the resolved
   connection name is `avestellar_ci` and explicitly is *not* the development
   database. If the guard does not fire, the job aborts before any test runs.
3. `python manage.py check`
4. `python manage.py makemigrations --check --dry-run` — fails on uncommitted
   model drift.
5. Security suites (see §1.4).
6. `python -m pytest -q --create-db` — the full suite against PostgreSQL. The
   CI flag overrides the developer convenience `--reuse-db` in `pytest.ini` so
   every run starts from an empty test database.
7. `python manage.py spectacular --validate` — OpenAPI schema validation.
8. Staging `check --deploy` with `DJANGO_ENV=staging DEBUG=False`. This proves
   the deployed profile boots under CI without the developer's `.env`. Because
   `check --deploy` exits 0 on warnings, the step parses the output and fails on
   any `security.*` code other than `security.W021` (HSTS preload, which cannot
   be satisfied until a real hostname is submitted to the preload list).
9. OpenAPI schema uploaded as an artifact (7 days) even on failure.

### 1.2 Frontend job (`frontend`)

All steps run in `frontend/`:

`pnpm install --frozen-lockfile` → `pnpm run lint` → `pnpm run typecheck` →
`pnpm run test` → `pnpm run build`.

`--frozen-lockfile` means the lockfile may not be rewritten: a manifest change
that was not accompanied by a lockfile update fails here instead of silently
resolving new versions. The pnpm store cache is keyed on
`frontend/pnpm-lock.yaml`, so a lockfile change invalidates it; the install is
still lockfile-exact either way.

### 1.3 Browser E2E job (`browser-e2e`)

This job reuses `scripts/run_browser_e2e.sh` verbatim rather than driving
Playwright directly. The script owns the safety model, so bypassing it would
bypass the guards. See `docs/browser_e2e_runbook.md` for the 9-step lifecycle.

CI-specific inputs:

| Variable | Value | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://avestellar_ci:…@127.0.0.1:5432/avestellar_ci` | DDL admin connection for the throwaway service container |
| `E2E_AV_DATABASE_NAME` | `avestellar_live_http_ci_e2e` | disposable database, prefix enforced by the runner |
| `E2E_DJANGO_PORT` / `E2E_NEXT_PORT` | `8200` / `3100` | isolated ports; the runner refuses `8000` |
| `E2E_DROP_DB` | `0` | the service container is destroyed with the runner, so the drop path is not exercised in CI |
| `E2E_PYTHON` | resolved from `sys.executable` | the script defaults to `.venv/bin/python`, which does not exist on a runner |

The job does **not** mock financial HTTP calls. Next.js proxies to the isolated
Django instance and the runner verifies the built `.next/routes-manifest.json`
pins `127.0.0.1:8200` before starting anything.

Playwright browsers are cached under `~/.cache/ms-playwright`, keyed on the
lockfile hash. On failure only, `frontend/test-results` and
`frontend/playwright-report` are uploaded (7 days). They contain synthetic
tenant names only — the fixtures in `scripts/seed_browser_e2e.py` — so they are
safe to retain.

### 1.4 Checks that block specifically on security work

Run first, so a security regression fails in seconds rather than after the full
suite:

```
tests/test_settings_hardening.py
tests/test_cache_configuration.py
tests/test_auth_security.py
tests/test_daraja_callback.py
tests/test_provider_secrets.py
tests/test_statement_upload_limits.py
tests/test_no_committed_credentials.py
tests/test_dependency_pinning.py
```

`test_ci_workflows.py` additionally validates the workflow file itself: no
`pull_request_target`, no `secrets.*` references, read-only token, pinned action
tags, no `continue-on-error`, no step loading a `.env`, service containers that
are health-checked throwaway PostgreSQL, Python selected through `.python-version`
rather than a competing literal, and no real credentials in the YAML.

### 1.5 Runtime versions

| Component | Where it is decided | Value |
| --- | --- | --- |
| Python | `.python-version` (repo root), consumed by `actions/setup-python` via `python-version-file` | 3.14.4 |
| Node.js | `ci.yml` (`setup-node`) | 22 |
| pnpm | `ci.yml` (`pnpm/action-setup`) | 12.10.1 |
| PostgreSQL | `ci.yml` matrix / E2E service | 16 and 18 (backend); 16 (E2E) |

`.python-version` is the single source of truth for Python: no workflow step
repeats the number, and `test_ci_workflows.py` fails if one reappears. That pin
is honoured by pyenv, uv, and setup-python — but **not by every deployment
platform**. Heroku-style buildpacks want `runtime.txt`, container builds must pin
the base image, and consoles with a Python setting must be told 3.14.4. See
§2a of `docs/staging_deployment.md`.

---

## 2. Database isolation

Three independent barriers keep CI away from real data:

1. **Separate service container per job.** `backend` (each matrix leg) and
   `browser-e2e` each get their own PostgreSQL container created for that job and
   destroyed with the runner. There is no persistent CI database to poison.
2. **Name assertion.** The backend job refuses to proceed unless the resolved
   database name is `avestellar_ci`. A future edit that points `DATABASE_URL` at
   `avestellar_finance` fails the job instead of deleting a developer's data.
3. **Runner-enforced prefix.** The E2E script only creates and drops databases
   whose name starts with `avestellar_live_http_`, checks this in three places,
   and after boot re-reads the *live server's* connection name to confirm it is
   the disposable database.

Teardown only signals processes the runner itself started (its launcher records
PIDs and walks `/proc` for descendants), so an unrelated local server is never
killed. Locally, the runner also refuses port 8000, which is reserved for the
ordinary development server.

---

## 3. Secrets policy

Test jobs must never be able to reach production systems. Concretely:

- **No repository or environment secret is referenced.** `${{ secrets.* }}` must
  not appear in the workflow; `tests/test_ci_workflows.py` fails if it does.
  Adding a provider key to CI is therefore a reviewed code change, not a
  settings tweak.
- **Every value in the workflow is synthetic and recognisably so**
  (`ci_synthetic_password`, `ci-only-synthetic-secret-…`). The committed-secret
  scanner treats these as nonfunctional placeholders; that classification is
  asserted in `tests/test_no_committed_credentials.py`.
- **No `.env` file reaches a runner.** Developer `.env` files are git-ignored and
  are never created in CI; a test asserts no workflow step copies or sources one.
- **Never place in CI:** production `SECRET_KEY`, `DATABASE_URL` for a real
  database, Daraja consumer key/secret/passkey, `MAUZOPILOT_API_KEY`,
  `AI_ASSISTANT_API_KEY`, webhook signing secrets, or any personal access token.
  The security suite (`test_provider_secrets.py`) fails if a provider credential
  is required to boot, so CI cannot accidentally start depending on one.
- Build logs print counts and file names, never environment values. If you add a
  step, do not echo configuration; echo the name of the check instead.

---

## 4. Shared cache and authentication throttling

Throttle counters live in the Django cache. The default cache is in-process
(`LocMemCache`), so with N workers the effective limit is N times the configured
rate. Distributed throttling requires a cache shared by all workers.

### 4.1 Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `REDIS_URL` | empty | `redis://`, `rediss://`, or `unix://` URL. Empty selects the in-process cache. |
| `CACHE_REQUIRE_SHARED` | `False` | refuse to boot when no shared cache is configured |
| `ALLOW_INSECURE_CACHE_TRANSPORT` | `False` | permit plaintext `redis://` to a non-loopback host |
| `CACHE_SOCKET_TIMEOUT_SECONDS` | `3` | connect/read timeout for cache operations |
| `CACHE_MAX_CONNECTIONS` | `100` | pool ceiling |

Rules enforced at startup (`config/env_validation.py`):

- Only `redis://`, `rediss://`, and `unix://` are accepted. `locmem://`,
  `memcached://`, and other schemes are rejected so the deployment does not
  believe it is shared when it is not.
- In staging/production (or any `DEBUG=False` run), a **remote** plaintext
  `redis://` URL is rejected; use `rediss://`. Plaintext to loopback is allowed.
- The URL must parse and include a host (and a socket path for `unix://`).
- A configured `REDIS_URL` with no installable client aborts the boot. Django's
  Redis backend imports `redis` lazily, so without this guard a missing package
  would appear as a 5xx on the first login instead of a startup error. The
  client is pinned in `requirements.txt` (`redis==8.1.0`, no transitive deps).
- **The URL is never echoed.** Validation errors name the setting, not the value,
  because cache URLs routinely embed a password. `scripts/check_shared_cache.py`
  follows the same rule and reports only the exception class.

### 4.2 When a shared cache is unavailable

The cache backend is fail-closed: a connection error propagates instead of being
swallowed, so throttled endpoints return `5xx` rather than silently accepting
unlimited attempts. That is the safer failure for authentication endpoints. A
`5xx` on login is a visible outage; a silent bypass of the login throttle is not.

The consequence must be stated plainly: **`REDIS_URL` being set does not prove
distributed throttling is active.**

### 4.3 Staging verification (operator action)

No Redis instance is provisioned by CI or by this milestone. The verification is
configuration-only: `scripts/check_shared_cache.py` against
the host's unreachable port returns `RESULT: cache round-trip FAILED
(ConnectionError)` and exit code 1, which is the same fail-closed path a real
outage takes.

When staging has a cache, verify it before claiming distributed rate limiting:

```bash
export DJANGO_ENV=staging DEBUG=False
export REDIS_URL=rediss://default:<password>@<host>:6380/1
export CACHE_REQUIRE_SHARED=True          # multi-worker: make absence fatal
python manage.py check
python scripts/check_shared_cache.py --require-shared
```

A successful run prints `RESULT: shared cache verified (round-trip OK)`. Only
then may the deployment be described as having distributed throttling. Until
then, the accurate description is "per-process throttling".

---

## 5. Reproducing a failed job locally

Run these from the repository root. Export the CI profile first so the behaviour
matches the runner:

```bash
export DJANGO_ENV=test DEBUG=True
export SECRET_KEY=ci-only-synthetic-secret-0123456789abcdefghijklmnop
export ALLOWED_HOSTS=localhost,127.0.0.1
export MAUZOPILOT_ENABLED=False MAUZOPILOT_API_KEY= MAUZOPILOT_WEBHOOK_SECRET=
export AI_ASSISTANT_ENABLED=False AI_ASSISTANT_API_KEY=
```

Point `DATABASE_URL` at a **disposable** database you create for this purpose —
never at `avestellar_finance`:

```bash
createdb avestellar_ci_local    # a role with CREATEDB, separate from your dev role
export DATABASE_URL=postgresql://<your-ci-role>:<password>@127.0.0.1:5432/avestellar_ci_local
```

Then reproduce the failing step:

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python -m pytest -q --create-db
python manage.py spectacular --validate --file /tmp/avestellar-openapi.yaml
```

Deploy-check step (note the profile override):

```bash
DJANGO_ENV=staging DEBUG=False ALLOWED_HOSTS=ci.avestellar.invalid \
  python manage.py check --deploy
```

Frontend (`cd frontend`):

```bash
pnpm install --frozen-lockfile
pnpm run lint && pnpm run typecheck && pnpm run test && pnpm run build
```

Browser E2E — read `docs/browser_e2e_runbook.md` first. Locally the script uses
`.venv/bin/python`; override `E2E_PYTHON` only if you deliberately installed the
requirements elsewhere:

```bash
DATABASE_URL=postgresql://<role>:<password>@127.0.0.1:5432/<admin_db> \
  bash scripts/run_browser_e2e.sh
```

If `--frozen-lockfile` fails, do not delete the lockfile. Run `pnpm install`
(without `--frozen-lockfile`), review the diff in `frontend/pnpm-lock.yaml`, and
commit it together with the manifest change.

---

## 6. Known gaps

- No GitHub Actions run has been executed for this workflow in this milestone.
  Local results are not a substitute: first-run risk lives in runner image
  contents (PostgreSQL client tools, browser dependencies) and in action
  version behaviour.
- Coverage is measured and reported but not enforced with a threshold.
- No dependency vulnerability scanner or SBOM is wired in; only pinning and
  manifest/lockfile drift are enforced.
- `security.W021` (HSTS preload) remains the single tolerated deploy warning
  until a real hostname exists and is submitted to the preload list.
- The browser job covers Chromium only.
