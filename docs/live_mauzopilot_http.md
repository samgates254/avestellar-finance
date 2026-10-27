# Local MauzoPilot ↔ AVESTELLAR HTTP integration

This fixture uses the actual Django development servers and HTTP APIs. It requires a disposable AVESTELLAR PostgreSQL database and a new MauzoPilot SQLite file under `/tmp`; it refuses to seed AVESTELLAR unless the database name begins `avestellar_live_http_`. It creates synthetic tenants and identifiers, uses no Daraja credentials, and never calls a live payment provider.

## Prepare isolated data

Choose a new unique database name and state directory for each clean run. The AVESTELLAR base `.env` must point at a local development PostgreSQL server with permission to create databases. The create helper refuses an existing name.

```sh
cd ~/Projects/avestellar-finance
export E2E_AV_DATABASE_NAME=avestellar_live_http_20261027_run1
export E2E_STATE_DIR=/tmp/mauzo-avestellar-e2e-run1
mkdir -m 700 -p "$E2E_STATE_DIR"
python3 -c 'from pathlib import Path; import secrets; Path("/tmp/mauzo-avestellar-e2e-run1/webhook_secret").write_text(secrets.token_urlsafe(32))'
chmod 600 "$E2E_STATE_DIR/webhook_secret"
export E2E_WEBHOOK_SECRET="$(cat "$E2E_STATE_DIR/webhook_secret")"
.venv/bin/python scripts/create_live_http_database.py
```

Prepare and seed MauzoPilot's test-only SQLite database:

```sh
cd ~/Projects/mauzopilot/backend
export E2E_MAUZO_SQLITE_PATH=/tmp/mauzopilot_live_http_20261027_run1.sqlite3
export DJANGO_SETTINGS_MODULE=core.live_http_e2e_settings
export DJANGO_SECRET_KEY=synthetic-local-http-integration-only
export DJANGO_DEBUG=true
export PYTHONPATH=/tmp:$PWD
export UV_CACHE_DIR=/tmp/mauzopilot-uv-cache
uv run --offline python manage.py migrate --noinput
E2E_STATE_DIR=/tmp/mauzo-avestellar-e2e-run1 E2E_WEBHOOK_SECRET="$E2E_WEBHOOK_SECRET" uv run --offline python manage.py shell -c 'import core.live_http_e2e_seed'
```

Migrate and seed AVESTELLAR with the same synthetic shared secret and MauzoPilot business credentials:

```sh
cd ~/Projects/avestellar-finance
export DJANGO_SETTINGS_MODULE=config.live_http_e2e_settings
export PYTHONPATH=/tmp:$PWD
.venv/bin/python manage.py migrate --noinput
E2E_STATE_DIR=/tmp/mauzo-avestellar-e2e-run1 E2E_WEBHOOK_SECRET="$E2E_WEBHOOK_SECRET" .venv/bin/python scripts/seed_live_mauzopilot_http.py
```

## Start the real applications

In separate terminals, keep both servers running for the full test:

```sh
# MauzoPilot, from backend/
UV_CACHE_DIR=/tmp/mauzopilot-uv-cache E2E_MAUZO_SQLITE_PATH=/tmp/mauzopilot_live_http_20261027_run1.sqlite3 PYTHONPATH=/tmp:$PWD DJANGO_SETTINGS_MODULE=core.live_http_e2e_settings DJANGO_SECRET_KEY=synthetic-local-http-integration-only DJANGO_DEBUG=true uv run --offline python manage.py runserver 127.0.0.1:8101 --noreload

# AVESTELLAR, from avestellar-finance/
E2E_AV_DATABASE_NAME=avestellar_live_http_20261027_run1 PYTHONPATH=/tmp:$PWD DJANGO_SETTINGS_MODULE=config.live_http_e2e_settings .venv/bin/python manage.py runserver 127.0.0.1:8100 --noreload
```

## Run the workflow

In another AVESTELLAR terminal, export credentials without printing them and run the fixture:

```sh
cd ~/Projects/avestellar-finance
export E2E_AV_DATABASE_NAME=avestellar_live_http_20261027_run1
export E2E_STATE_DIR=/tmp/mauzo-avestellar-e2e-run1
export E2E_WEBHOOK_SECRET="$(cat "$E2E_STATE_DIR/webhook_secret")"
export E2E_MAUZO_TOKEN="$(cat "$E2E_STATE_DIR/mauzo_token")"
export E2E_MAUZO_BUSINESS_ID="$(cat "$E2E_STATE_DIR/mauzo_business_id")"
export E2E_AV_ACCESS_TOKEN="$(cat "$E2E_STATE_DIR/av_access_token")"
export E2E_AV_ORGANIZATION_ID="$(cat "$E2E_STATE_DIR/av_organization_id")"
export E2E_MAUZO_BASE_URL=http://127.0.0.1:8101
export E2E_AV_BASE_URL=http://127.0.0.1:8100
PYTHONPATH=/tmp:$PWD DJANGO_SETTINGS_MODULE=config.live_http_e2e_settings .venv/bin/python scripts/live_mauzopilot_http.py
```

The runner creates a customer and product through MauzoPilot HTTP, posts an itemized integer-cent sale, observes the signed outbound event at the live AVESTELLAR receiver, exercises the authenticated incremental connector, then issues the draft invoice and uses a synthetic Daraja callback to verify the pending M-Pesa payment. It also tests duplicate and rejected events, reporting invariants, and one failed-then-successful outbox HTTP delivery. It refuses to run against non-PostgreSQL AVESTELLAR or a database outside the dedicated name prefix.

The fixture is intended for a fresh disposable database. Remove only the named test database and `/tmp` fixture files after stopping both servers. Do not reuse credentials from a real tenant or enable live Daraja configuration.
