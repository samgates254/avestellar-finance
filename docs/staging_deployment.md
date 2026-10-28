# Staging Deployment & Security Hardening (Milestone 7.1)

This document describes how AVESTELLAR FINANCE is configured to run safely in a
controlled staging environment and how it fails closed when production-style
configuration is missing. It does **not** provision hosting or deploy anything;
it is the operator's reference for a later, deliberate deployment.

## 1. Configuration profiles (`config/settings.py`)

Settings are gated by a single profile flag, resolved in `config/env_validation.py`:

```
DJANGO_ENV ∈ {development, test, staging, production}   # ENVIRONMENT = legacy alias
STRICT_CONFIG = strict_mode(DJANGO_ENV, DEBUG)
              = (DJANGO_ENV in {staging, production}) or (DEBUG is False)
```

- An **unrecognised** `DJANGO_ENV` value refuses to boot (no silent fallback to
development).
- **Local development / test suite** run with `DEBUG=True` and
  `DJANGO_ENV=development`, so they keep ergonomic defaults (permissive localhost
  `ALLOWED_HOSTS`, optional SQLite).
- **Anything deployed** (`DEBUG=False`, or `DJANGO_ENV=staging|production`)
  enters strict mode and **refuses to boot** unless the required values are set.

### Fail-closed requirements in strict mode

| Concern              | Behavior when missing/invalid                                   |
| -------------------- | --------------------------------------------------------------- |
| `SECRET_KEY`         | Empty, shorter than 32 chars, or a known placeholder → `ImproperlyConfigured`. |
| `ALLOWED_HOSTS`      | Empty or containing a wildcard → `ImproperlyConfigured`. Explicit hostnames required. |
| `DATABASE_URL`       | Missing/empty → `ImproperlyConfigured`.                          |
| Database URL shape   | Unparsable, or missing host/database name → `ImproperlyConfigured`. |
| Database engine      | The **effective** engine (`DATABASES["default"]["ENGINE"]`) is validated after parsing: anything other than PostgreSQL is refused, so an environment variable alone cannot satisfy the check. |

`DEBUG` defaults to **False** when unset, so a forgotten variable yields staging
behavior (fail closed) rather than an accidental open debug mode. There is **no
built-in `SECRET_KEY` fallback** at any profile.

### CORS / CSRF — never derived from DEBUG

- `CORS_ALLOWED_ORIGINS` is an explicit, comma-separated allow-list.
- `CORS_ALLOW_CREDENTIALS` is enabled only when origins are listed.
- `CORS_ALLOW_ALL_ORIGINS` defaults to **False** and is **not** tied to `DEBUG`.
- `CSRF_TRUSTED_ORIGINS` is an explicit allow-list for HTTPS origins.
- In strict mode, `*`, `http://*`, and `null` origins are **rejected** at
  startup, as are non-HTTPS origins. `ALLOW_PERMISSIVE_CORS=True` is the only
  override and exists purely as a documented, audited emergency hatch.

The Next.js app calls the API same-origin through its `/api/v1` proxy, so
cross-origin access is normally unnecessary; list exact origins only if the
browser must call the API directly.

### HTTPS / security headers (applied when `DEBUG=False`)

- `SECURE_SSL_REDIRECT` (default True).
- HSTS: `SECURE_HSTS_SECONDS` (default 1 year), subdomains, optional preload.
- `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` True.
- `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY=same-origin`,
  `X_FRAME_OPTIONS=DENY`.
- `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")` when
  `USE_X_FORWARDED_PROTO=True` (the common TLS-terminating managed-PaaS shape).
  Set it to `False` only if your proxy does **not** rewrite that header.

### Request / upload ceilings

- `DATA_UPLOAD_MAX_MEMORY_SIZE` (default 10 MiB) bounds non-file request bodies.
- `FILE_UPLOAD_MAX_MEMORY_SIZE` (default 5 MiB) is the threshold above which an
  uploaded file spools to a temp file — it does **not** limit the accepted size.
- `MAX_STATEMENT_UPLOAD_BYTES` (default 15 MiB) is the maximum accepted statement
  file, enforced by `StatementImportUploadSerializer` together with a suffix
  allow-list. It matches the frontend guard in
  `frontend/src/components/statements.tsx`.
- The reverse proxy must allow at least `MAX_STATEMENT_UPLOAD_BYTES`
  (e.g. nginx `client_max_body_size 16m;`), otherwise clients get a 413 from the
  proxy before Django's own validation runs.

## 2. Database & connection pooling

- Strict mode mandates PostgreSQL; the app will not silently fall back to SQLite.
- `DB_CONN_MAX_AGE` defaults to **0**. A zero/persistent-connection value is the
  safe choice behind a **PgBouncer transaction pooler**, where server-side
  session state cannot be reused across requests. Raise it only when you know
  you have session pooling or a direct connection pool.
- `CONN_HEALTH_CHECKS=True` avoids handing out stale connections.
- `DB_CONNECT_TIMEOUT_SECONDS` (default 10) sets `connect_timeout` for Postgres.

## 2a. Runtime pin (Python)

- The supported interpreter is **Python 3.14.4**, recorded once in the repository
  root `.python-version`. CI installs that exact release via
  `actions/setup-python` (`python-version-file: .python-version`) and carries no
  second hardcoded version, so the workflow cannot drift from the pin.
- **Platforms must honour the pin explicitly.** `.python-version` is read by
  pyenv, uv, and similar local tools; it is *not* a manifest that every PaaS
  understands. Heroku-style buildpacks expect `runtime.txt`, container builds must
  pin the base image (`python:3.14.4-slim`), and providers with a console-level
  Python setting must be set to 3.14.4 there. Silently inheriting the platform
  default is the failure mode this pin exists to prevent.
- After the first staging release, confirm `python -V` inside the running
  dyno/container reports 3.14.4.
- PostgreSQL majors: staging runs **16**. CI runs the backend suite on both 16 and
  18 and pins the `browser-e2e` job to 16, so the browser journey exercises the
  same major staging uses.

## 3. Authentication hardening

- SimpleJWT with `ROTATE_REFRESH_TOKENS=True` and
  `BLACKLIST_AFTER_ROTATION=True`; the `token_blacklist` app is installed.
- `POST /api/v1/auth/logout/` (authenticated) blacklists the supplied refresh
  token and returns HTTP 205. The frontend discards tokens from memory on
  sign-out.
- **Throttling** (DRF `ScopedRateThrottle`):
  - `login` scope: `20/min` (overridable via `THROTTLE_LOGIN_RATE`).
  - `register` scope: `10/hour` (overridable via `THROTTLE_REGISTER_RATE`).
- **Cache/topology note:** throttling state lives in the Django cache.
  - If `REDIS_URL` is set, Redis is used and limits are shared across workers.
  - Without Redis the app falls back to in-process `LocMemCache`, which is
    per-process only. **Do not represent LocMemCache throttling as reliable
    distributed rate limiting** across multiple Gunicorn workers/instances —
    provision Redis for staging.
  - The setting is validated at boot (`validate_cache_url`): only `redis://`,
    `rediss://`, and `unix://` are accepted, and in strict mode a *remote*
    plaintext `redis://` URL is rejected (use `rediss://`). Validation errors
    name the setting, never the URL, because cache URLs embed a password.
  - Set `CACHE_REQUIRE_SHARED=True` on any multi-worker deployment so a missing
    `REDIS_URL` aborts the boot instead of silently degrading to per-process
    limits.
  - The backend is **fail-closed**: if the cache is unreachable, throttled
    endpoints return 5xx rather than letting requests through unthrottled.
  - The `redis` client is a pinned dependency (`requirements.txt`). A configured
    `REDIS_URL` with no client installed aborts the boot instead of surfacing as
    a 5xx on the first login.
  - Setting `REDIS_URL` is not evidence that sharing works. Verify it:

    ```bash
    python scripts/check_shared_cache.py --require-shared
    ```

    Only a passing round-trip justifies the phrase "distributed rate limiting".
  - Full operational detail: `docs/ci_release_gates.md` §4.

## 4. Daraja (M-Pesa) callback integrity

Inbound Safaricom callbacks are **unauthenticated**: Daraja does not sign the
callback body with an HMAC we can verify. Therefore:

- Organization resolution (`_resolve_daraja_org`) **ignores** the caller-supplied
  `X-Organization-ID` header/query. It binds only to server-side state the
  business itself created:
  - C2B → the payload `BusinessShortCode` maps to that org's `DarajaConfig`.
  - STK → the `CheckoutRequestID`/`MerchantRequestID` matches a **PENDING**
    payment this system initiated.
  - If neither binds, the callback is unattributable and returns HTTP 400.
- An unauthenticated confirmation **never** settles an invoice on its own. A
  bare `BillRefNumber` match is held as a **PENDING** payment with a
  `payment.verification_required` outbox event; no settlement journal entry is
  posted. Automatic promotion happens only when the callback matches an
  initiated PENDING payment and agrees on amount, currency, method and invoice
  state (via `settle_pending_payment`).
- STK success callbacks likewise require a bound initiated payment and a
  matching amount; mismatches and orphan callbacks are held for review, and a
  `payment.confirmed` event is emitted only on a verified `CONFIRMED` payment.

**Remaining limitation:** without a provider-supplied signature or a synchronous
independent transaction-status query, a correctly-bound callback is corroborated
by our own initiated state, not cryptographically proven by Safaricom. Payments
that cannot be independently verified are held for human/reconciliation review.
Keep live Daraja credentials disabled in staging until this verification flow
has passed its tests in the target environment.

## 5. Production server & static files

- **Gunicorn** (`gunicorn==26.2.0`) is the WSGI server. Django's development
  server must never be used for staging/production.
  Start command (see `Procfile`):
  `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 3 --timeout 60`
- **WhiteNoise** (`whitenoise==6.12.0`) serves Django's collected static assets
  (admin, etc.) from the app process — no separate static file server is needed.
  Outside `DEBUG`, `STORAGES["staticfiles"]` uses
  `CompressedManifestStaticFilesStorage` (compressed, content-hashed). Run
  `python manage.py collectstatic` at release time.

## 6. Environment variables (see `.env.example`)

Required in staging: `SECRET_KEY` (50+ chars, not a placeholder), `ALLOWED_HOSTS`,
`DATABASE_URL` (PostgreSQL), `DJANGO_ENV=staging` (or `production`), `DEBUG=False`.
Recommended: `REDIS_URL` (for shared throttling), `CSRF_TRUSTED_ORIGINS`,
`USE_X_FORWARDED_PROTO`, and the throttle / upload / DB-pooling knobs.
Never commit a real `.env`; it is git-ignored and a regression test asserts that.

## 6a. Provider secrets at rest — known limitation

`DarajaConfig`, `MauzoPilotConfig`, and `WebhookSubscription` store provider
credentials as plaintext model fields:

- Serializers treat them as **write-only**, never return them, ignore blank
  submissions on update, and only clear them via an explicit
  `{"clear_secrets": true}` request.
- They are **not** encrypted at rest by the application. Database/storage-level
  encryption is an infrastructure control, not something this code provides,
  and TLS in transit must **not** be described as encryption at rest.
- Until envelope encryption (application-level, key outside the database and
  repository) is implemented, treat the database as secret-bearing: restrict
  access, keep provider credentials disabled in staging, and never enable a live
  connector without that review.

## 6b. Payment callback trust model

An inbound Safaricom callback is unauthenticated evidence, never proof. See
section 4. `DarajaCallbackStatus.HELD` marks callbacks that were attributed but
could not be corroborated: they are held for review, emit
`payment.verification_required`, and are **excluded** from
`verify_payment_evidence`, so a held callback can never be reused as trusted
evidence for another payment.

`DarajaClient.verify_payment()` raises `DarajaVerificationUnavailable` — a
synchronous provider confirmation is deliberately not implemented, and
`query_transaction_status()` reports `UNVERIFIED` rather than fabricating a
successful result. `settle_from_verified_result()` is the reserved entry point
for a future genuine verification result; it re-validates tenant, reference,
amount, currency and invoice state before settling.

## 6c. Committed-credential rotation (required before staging)

Earlier commits on this branch contained a **working** local PostgreSQL URL in the
tracked `.env.example` (`DATABASE_URL` with a real role/password pair), plus a
development-looking `SECRET_KEY`. Both are now replaced with placeholders.

- **Status check performed:** the historical local credential was tested against
  the local PostgreSQL instance without printing it and is **still accepted** for
  the `avestellar` role on `127.0.0.1` (local-only role, not a hosted service).
- **Required operator action (not performed here — it would break the local dev
  environment and is outside this milestone's mandate):**
  1. Rotate the role password:
     `ALTER ROLE avestellar WITH PASSWORD '<new-unique-value>';`
  2. Update the local, untracked `.env` with the new URL and restart any dev
     process that holds a connection.
  3. Issue a fresh `SECRET_KEY` for every environment and redeploy — the old
     value must be considered public, so any token signed with it is forgeable.
- **Git history is intentionally not rewritten.** The value stays in history;
  rotation, not erasure, is the mitigation. Anyone with a clone of the repository
  must assume the historical local credential was public.
- `tests/test_no_committed_credentials.py` fails the build if a credential-shaped
  value, a literal `SECRET_KEY`, a provider token prefix, or a tracked `.env`
  reappears.

## 7. Deployment checks before go-live

1. `python manage.py check --deploy` and resolve warnings for your topology.
2. `python manage.py migrate` against the staging database.
3. `python manage.py collectstatic`.
4. Confirm the app refuses to start with a missing secret/host/database (fail
   closed) — this is expected and protective. Expect `security.W021`
   (HSTS preload) to remain a warning: submitting to the browser preload list is
   an external, inbound-only action, so `SECURE_HSTS_PRELOAD` is deliberately not
   forced on. Enable it only after deciding to submit the domain.
5. Keep real payment-provider connections disabled until the callback
   verification flow passes its tests in staging.
6. Confirm the running interpreter matches `.python-version` (`python -V` →
   3.14.4) and that the staging database server reports PostgreSQL 16
   (`SHOW server_version`).
