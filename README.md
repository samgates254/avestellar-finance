# AVESTELLAR FINANCE

**Intelligent Financial Operating System & Accounting Backend**  
*AVESTELLAR TECHNOLOGIES*

---

## 1. Executive Summary

AVESTELLAR FINANCE is an API-first accounting, double-entry general ledger, and financial intelligence backend. It combines rigorous double-entry accounting invariants, multi-tenant SaaS architecture, transactional outbox event streaming, MauzoPilot integration adapters, and deterministic AI financial analysis in a maintainable modular monolith.

---

## 2. Technology Stack

- **Language**: Python 3.14.4, pinned once in `.python-version`; CI installs
exactly that release
- **Framework**: Django 5.2 LTS & Django REST Framework 3.16
- **Database**: PostgreSQL 16–18 (via `psycopg 3.3.6`); CI runs the backend suite
  on both 16 and 18 and pins browser E2E to 16, the staging major
- **Authentication**: JWT (`djangorestframework-simplejwt 5.5`)
- **API Documentation**: OpenAPI 3.0 via `drf-spectacular 0.28`
- **Environment**: `django-environ 0.12`
- **Testing**: `pytest 9.1`, `pytest-django 4.14`, and `httpx 0.28`

---

## 3. Core Architecture & Modules

```
avestellar-finance/
├── identity/          # Custom User model, JWT authentication, user profiles
├── organizations/     # Multi-tenant scoping, roles (Owner, Admin, Accountant, Viewer)
├── ledger/            # Chart of Accounts, Accounting Periods, Journal Entries, Reports
├── invoicing/         # Customers, Sales Invoices, Lines, AR recognition
├── purchases/         # Suppliers, Vendor Bills, Accounts Payable
├── payments/          # Payment settlement, partial payments, deposit reconciliation
├── automation/        # Transactional outbox, webhook subscriptions, HMAC signatures
├── integrations/      # MauzoPilot adapter, canonical object mappings, n8n receiver
├── analytics/         # Cash flow liquidity, receivables aging, anomaly detection, AI assistant
├── auditlog/          # Audit trail tracking for sensitive financial mutations
├── config/            # Django settings, root routing, OpenAPI schema, health check
├── docs/              # Integration and architecture guides
└── tests/             # Comprehensive automated test suite
```

---

## 4. Getting Started

### 4.1 Prerequisites
- Python 3.14.4 (the exact release named in `.python-version`)
- PostgreSQL 16+ running locally or in Docker

### 4.2 Configuration
Copy the environment template:
```bash
cp .env.example .env
```

Ensure your PostgreSQL database is created:
```bash
# Example local postgres connection. Substitute your OWN local role/password;
# never commit or share a working credential.
export PGPASSWORD=YOUR_LOCAL_POSTGRES_PASSWORD
psql -h 127.0.0.1 -p 5433 -U YOUR_LOCAL_USER -c "CREATE DATABASE avestellar_finance;"
```

Update `.env` database URL:
```ini
DATABASE_URL=postgresql://YOUR_LOCAL_USER:YOUR_LOCAL_PASSWORD@127.0.0.1:5433/avestellar_finance
```

Configuration is profile-driven. `.env.example` ships `DJANGO_ENV=development` and
`DEBUG=True`; `staging` / `production` (or any run with `DEBUG=False`) enforce
strict validation and refuse to boot without a real `SECRET_KEY`, explicit
`ALLOWED_HOSTS`, and a PostgreSQL `DATABASE_URL`. Unknown `DJANGO_ENV` values and
known placeholder secrets are rejected. See `docs/staging_deployment.md` for the
full fail-closed matrix and the provider-secret limitations.

> `.env` is gitignored and must never be committed. `SECRET_KEY`, `DATABASE_URL`,
> and provider credentials are environment-specific and are supplied out of band
> (secret manager / platform env), not from `.env.example`.

### 4.3 Database Migrations
Apply migrations to PostgreSQL:
```bash
python manage.py migrate
```

### 4.4 Start the Development Server
```bash
python manage.py runserver 0.0.0.0:8000
```

Access the interactive OpenAPI Documentation at:
- Swagger UI: `http://localhost:8000/api/docs/`
- ReDoc: `http://localhost:8000/api/redoc/`
- Schema JSON/YAML: `http://localhost:8000/api/schema/`

---

## 5. Verification & Testing

Run all automated checks:
```bash
# 1. Django system check
python manage.py check

# 2. Check for unapplied migrations
python manage.py makemigrations --check --dry-run
python manage.py migrate --check

# 3. Run full automated test suite (269 backend tests, 21 frontend unit tests,
#    10 browser E2E journeys)
pytest -q --create-db

# 4. Validate OpenAPI documentation schema
python manage.py spectacular --validate --file /tmp/avestellar-openapi.yaml
```

Security-focused suites:
```bash
pytest tests/test_settings_hardening.py tests/test_cache_configuration.py \
       tests/test_auth_security.py tests/test_daraja_callback.py \
       tests/test_provider_secrets.py tests/test_statement_upload_limits.py \
       tests/test_no_committed_credentials.py tests/test_dependency_pinning.py \
       tests/test_ci_workflows.py -q
```

### 5.1 Continuous integration

`.github/workflows/ci.yml` runs three independent jobs on every push to `main`
and `feat/**`, on every pull request, and on demand:

| Job | Gates |
| --- | --- |
| `backend` | system checks, migration drift, the security suites above, the full PostgreSQL suite on **16 and 18**, OpenAPI validation, staging `check --deploy` |
| `frontend` | `pnpm install --frozen-lockfile`, lint, typecheck, unit tests, production build |
| `browser-e2e` | the 10 real-HTTP Playwright journeys via `scripts/run_browser_e2e.sh` against a disposable PostgreSQL 16 database |

Each job gets its own throwaway PostgreSQL service container and synthetic
credentials; no job reads a `.env` file, references a repository secret, or
reaches a payment provider. The interpreter version is read from
`.python-version`, so the workflow holds no competing hardcoded Python version.
See [docs/ci_release_gates.md](docs/ci_release_gates.md)
for the blocking criteria, local reproduction of failures, the database
isolation model, the CI secrets policy, and how staging enables shared-cache
throttling.

---

## 6. Key API Endpoints (`/api/v1/`)

### Authentication & Identity
- `POST /api/v1/auth/register/` - Register new user
- `POST /api/v1/auth/token/` - Obtain JWT token pair
- `POST /api/v1/auth/token/refresh/` - Refresh JWT token (rotates + blacklists the old one)
- `POST /api/v1/auth/logout/` - Revoke the caller's refresh token (authenticated, idempotent)
- `GET /api/v1/auth/me/` - Retrieve authenticated user profile

### Organizations & Multi-Tenancy
- `GET /api/v1/organizations/` - List organizations for current user
- `POST /api/v1/organizations/` - Create organization and initialize default Chart of Accounts
- `GET /api/v1/organizations/<id>/` - Retrieve organization details
- `GET /api/v1/organizations/<id>/members/` - List organization members
- `POST /api/v1/organizations/<id>/members/` - Add user to organization with role

### Double-Entry General Ledger
- `GET /api/v1/ledger/accounts/` - Chart of Accounts
- `POST /api/v1/ledger/accounts/` - Create ledger account
- `GET /api/v1/ledger/periods/` - List accounting periods
- `POST /api/v1/ledger/periods/<id>/close/` - Close accounting period
- `GET /api/v1/ledger/journals/` - Query journal entries
- `POST /api/v1/ledger/journals/` - Create draft journal entry with lines
- `POST /api/v1/ledger/journals/<id>/post/` - Validate and post entry atomically
- `POST /api/v1/ledger/journals/<id>/reverse/` - Execute accounting reversal

### Financial Statements
- `GET /api/v1/reports/trial-balance/` - Trial Balance
- `GET /api/v1/reports/income-statement/` - Income Statement (P&L)
- `GET /api/v1/reports/balance-sheet/` - Balance Sheet (Assets = Liabilities + Equity)

### Invoicing & Payments
- `GET|POST /api/v1/invoicing/customers/` - Customer directory
- `GET|POST /api/v1/invoicing/invoices/` - Sales invoices with line items
- `POST /api/v1/invoicing/invoices/<id>/issue/` - Issue invoice and post AR journal
- `GET|POST /api/v1/payments/` - Record and settle customer/supplier payments

### Integrations & Canonical Data Flywheel
- `GET|PATCH /api/v1/integrations/mauzopilot/config/` - Manage MauzoPilot connector credentials, business ID, and active state
- `POST /api/v1/integrations/mauzopilot/test-connection/` - Upstream ping to `/api/health/` verifying tokens and latency
- `POST /api/v1/integrations/mauzopilot/sync/` - On-demand incremental synchronization of customers and sales
- `GET /api/v1/integrations/mauzopilot/mappings/` - Inspect canonical external ID mappings
- `GET /api/v1/integrations/health/` - Comprehensive integration telemetry (MauzoPilot, Daraja, Outbox queues)
- `POST /api/v1/integrations/n8n/webhook/` - Inbound n8n execution webhook (HMAC-SHA256 authenticated)

### Automation & Webhooks
- `GET|POST /api/v1/automation/webhooks/` - Manage outbound webhook subscriptions
- `GET /api/v1/automation/outbox/` - Inspect transactional outbox events

### Financial Intelligence & AI Assistant
- `GET /api/v1/analytics/financial-dashboard/` - Unified executive financial intelligence dashboard
- `GET /api/v1/analytics/metrics/` - Registered metric catalog with formulas, units, and limitations
- `GET /api/v1/analytics/profitability/` - Multi-tier margins, gross/net revenue, EBIT, EBITDA reconciliation bridge
- `GET|POST /api/v1/analytics/capital-expenditure/` - CAPEX tracking, project records, CAPEX-to-revenue/depreciation
- `GET /api/v1/analytics/returns/` - ROA, ROE, ROIC, and ROCE return metrics
- `GET /api/v1/analytics/liquidity-leverage/` - Working Capital, Current/Quick/Cash ratios, Total/Net Debt, Interest Coverage
- `GET /api/v1/analytics/efficiency/` - Inventory Turnover, DIO, Receivables Turnover, DSO, DPO, Cash Conversion Cycle
- `POST /api/v1/analytics/calculators/npv/` - Deterministic Net Present Value calculation
- `POST /api/v1/analytics/calculators/irr/` - Hybrid Newton-Raphson / Bisection IRR numerical solver
- `POST /api/v1/analytics/calculators/investment/` - Comprehensive investment appraisal suite (NPV, IRR, Payback, PI)
- `POST /api/v1/analytics/calculators/break-even/` - Single-product Contribution Margin, Break-Even, Margin of Safety
- `POST /api/v1/analytics/calculators/break-even-multiproduct/` - Multi-product composite break-even sales mix
- `GET|POST /api/v1/analytics/scenarios/` - Pro-forma financial scenario simulations (strictly ledger-immutable)
- `GET|POST /api/v1/analytics/account-mappings/` - Organization-specific Chart of Accounts classification mappings
- `GET /api/v1/analytics/account-mappings/completeness/` - Chart of accounts mapping completeness audit
- `GET /api/v1/analytics/cash-flow/` - Cash flow liquidity and 30-day projection
- `GET /api/v1/analytics/receivables-aging/` - 30-day aging brackets
- `GET /api/v1/analytics/anomalies/` - Anomaly and financial risk detection
- `GET /api/v1/analytics/ai/management-summary/` - Deterministic AI executive summary
- `GET /api/v1/analytics/ai/explain-pnl/` - Narrative P&L explanation
- `GET /api/v1/analytics/ai/explain-ebitda/` - Reconciled EBITDA bridge narrative explanation

### Statement Imports & Bank Reconciliation
- `GET|POST /api/v1/payments/statement-imports/` - Upload and ingest Bank CSV, M-Pesa CSV, OFX, and MT940 files with SHA-256 deduplication
- `GET /api/v1/payments/statement-imports/<id>/` - Retrieve import batch details, validation summary, and transactions
- `GET /api/v1/payments/reconciliation/unreconciled/` - Accountant review queue of unmatched statement lines
- `GET /api/v1/payments/reconciliation/candidates/<id>/` - Ranked match candidates with rule explanations and scores
- `POST /api/v1/payments/reconciliation/match/` - Confirm proposed or manual match and trigger atomic GL settlement
- `POST /api/v1/payments/reconciliation/investigate/` - Flag transaction for manual investigation
- `GET|POST /api/v1/payments/reconciliation/sessions/` - Reconciliation sessions tracking statement vs GL balances
- `GET /api/v1/payments/reconciliation/sessions/<id>/` - Session details and variance analysis
- `POST /api/v1/payments/reconciliation/sessions/<id>/finalize/` - Lock and finalize reconciliation session
- `POST /api/v1/payments/reconciliation/sessions/<id>/reopen/` - Authorized reopening of finalized session

### Safaricom Daraja M-Pesa Integration
- `GET|PATCH /api/v1/integrations/daraja/config/` - Manage Daraja credentials and deposit clearing accounts
- `POST /api/v1/integrations/daraja/c2b-confirmation/` - Asynchronous C2B payment confirmation webhook (replay-resistant)
- `POST /api/v1/integrations/daraja/c2b-validation/` - Asynchronous C2B validation webhook
- `POST /api/v1/integrations/daraja/stk-callback/` - Lipa Na M-Pesa Online (STK Push) callback handler
- `POST /api/v1/integrations/daraja/verify-status/` - Safaricom Daraja transaction status verification query

---

## 7. Documentation & Architecture References

- MauzoPilot Verified Integration: [docs/mauzopilot_integration.md](file:///home/mr-robot/Projects/avestellar-finance/docs/mauzopilot_integration.md)
- Canonical Data Flywheel Architecture: [docs/data_flywheel.md](file:///home/mr-robot/Projects/avestellar-finance/docs/data_flywheel.md)
- n8n Automation Workflows: [docs/n8n_integration.md](file:///home/mr-robot/Projects/avestellar-finance/docs/n8n_integration.md)
- Integration Operations & Telemetry: [docs/integration_operations.md](file:///home/mr-robot/Projects/avestellar-finance/docs/integration_operations.md)
- Payment Boundary & Verification Security: [docs/payment_integration_security.md](file:///home/mr-robot/Projects/avestellar-finance/docs/payment_integration_security.md)
- Deterministic Reconciliation Engine: [docs/reconciliation_engine.md](file:///home/mr-robot/Projects/avestellar-finance/docs/reconciliation_engine.md)
- Bank Statement Imports (CSV, OFX, MT940): [docs/bank_statement_imports.md](file:///home/mr-robot/Projects/avestellar-finance/docs/bank_statement_imports.md)
- Safaricom M-Pesa Statement Reconciliation: [docs/mpesa_reconciliation.md](file:///home/mr-robot/Projects/avestellar-finance/docs/mpesa_reconciliation.md)
- Safaricom Daraja Callback Integration: [docs/daraja_callback_integration.md](file:///home/mr-robot/Projects/avestellar-finance/docs/daraja_callback_integration.md)
- Comprehensive Financial Metrics Catalog: [docs/financial_metrics_catalog.md](file:///home/mr-robot/Projects/avestellar-finance/docs/financial_metrics_catalog.md)
- Core Architectural Specifications: [docs/architecture_and_flywheel.md](file:///home/mr-robot/Projects/avestellar-finance/docs/architecture_and_flywheel.md)
- CI & Release Gates: [docs/ci_release_gates.md](file:///home/mr-robot/Projects/avestellar-finance/docs/ci_release_gates.md)

---

## 8. License & Support

Confidential and Proprietary. Copyright © 2026 AVESTELLAR TECHNOLOGIES. All rights reserved.
