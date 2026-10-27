# Browser E2E runbook (isolated PostgreSQL)

These tests drive the **real** Next.js app and the **real** Django REST API
against a **disposable, explicitly-prefixed PostgreSQL database** seeded with
synthetic tenants. No financial API call is mocked; only the browser
`confirm()`/`prompt()` dialogs are auto-accepted.

**Never** run these against the ordinary development database
(`avestellar_finance`) or the pre-existing development server on `:8000`.

## One command

From the repository root, with `DATABASE_URL` pointing at your PostgreSQL
instance (the same value you use for normal local runs):

```bash
DATABASE_URL="postgresql://<user>:<pass>@127.0.0.1:5433/<admin_db>" \
  scripts/run_browser_e2e.sh
```

`scripts/run_browser_e2e.sh` performs the full lifecycle and is the source of
truth. It exits with the Playwright status code.

### Tunable environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | (required) | PostgreSQL connection; also used as the DDL **admin** connection. |
| `E2E_AV_DATABASE_NAME` | `avestellar_live_http_browser_e2e` | Disposable DB. **Must** start with `avestellar_live_http_`. |
| `E2E_DJANGO_PORT` | `8200` | Isolated Django port (refuses `8000`). |
| `E2E_NEXT_PORT` | `3100` | Isolated Next port. |
| `E2E_DROP_DB` | `0` | Set `1` to drop the disposable DB afterwards. |

## Lifecycle (what the runner does)

1. **Validate** the venv, `DATABASE_URL` (PostgreSQL only), the disposable DB
   prefix, and that the chosen ports are free. Refuses port `8000` and refuses
   to kill anything already listening.
2. **Create/reset** the disposable DB — terminate only its own connections, then
   `DROP`/`CREATE`. Guarded by a strict `^avestellar_live_http_[a-z0-9_]+$`
   regex so it can never target `avestellar_finance`.
3. **Migrate + seed** under `DJANGO_SETTINGS_MODULE=config.live_http_e2e_settings`
   (which itself re-asserts PostgreSQL + the `avestellar_live_http_` prefix) and
   `scripts/seed_browser_e2e.py` (self-guarded the same way).
4. **Start Django** on the isolated port, wait for `/api/v1/health/`, then read
   the active `connection.settings_dict["NAME"]` and **abort unless it equals the
   disposable DB** — the key protection against writing to the dev DB.
5. **Build Next** with `API_PROXY_TARGET` pinned to the isolated Django, and
   verify `.next/routes-manifest.json` actually pins it (rewrites are frozen at
   build time, so this is required before `next start`).
6. **Start Next** on its port and run **Playwright** with `TMPDIR` inside the
   project-local `.e2e-runtime/playwright-tmp` and `E2E_BASE_URL` aimed at Next.
7. **Collect** results in `frontend/test-results` and `frontend/playwright-report`.
8. **Stop only** the Django/Next processes the runner started. Each server is
   launched in its **own session** (own process group) via a small self-reaping
   launcher, so teardown signals the *whole* tree — this reliably kills the
   `next-server` grandchild that `pnpm exec next start` forks, leaving no orphan
   holding the port. The runner never signals a process it did not start.
9. Optionally **drop** the disposable DB after re-validating its exact
   disposable name (`E2E_DROP_DB=1`).

## Manual steps that cannot be fully automated

- **Playwright browser binary.** `@playwright/test` reuses a cached Chromium
  build. If no cached browser exists on a fresh machine, run
  `pnpm exec playwright install chromium` once (needs network).
- **PostgreSQL reachability.** A running PostgreSQL server matching
  `DATABASE_URL` is assumed; the runner does not start PostgreSQL. Point
  `DATABASE_URL` at a **maintenance** database you may connect to for DDL (e.g.
  the `avestellar` role's own database), **not** at `avestellar_finance`; the
  disposable DB is created/dropped alongside it and the app DB name is overridden
  by `E2E_AV_DATABASE_NAME`.
- **Read-only `/tmp` sandboxes.** All transient files (launcher, pidfiles, server
  logs, Playwright temp) live under the project-local, git-ignored
  `.e2e-runtime/` directory, so the runner never depends on `/tmp` being writable.

## Safety model (defense in depth)

- DB **name** prefix is enforced in three independent places: the runner,
  `config/live_http_e2e_settings.py`, and `scripts/seed_browser_e2e.py`.
- Financial-write tests only ever hit the disposable DB because the runner
  asserts the live server's active DB name before Playwright runs, and pins the
  Next proxy to that server at build time.
- Generated artifacts (`frontend/test-results/`, `playwright-report/`,
  `.pwtmp/`, `.next/`, `node_modules/`) and the runner's `.e2e-runtime/` scratch
  directory are git-ignored and never committed.
