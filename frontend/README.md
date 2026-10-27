# AVESTELLAR FINANCE frontend

Separate Next.js, React, TypeScript and Tailwind application for the existing Django API. It uses the routed API and serializers in this repository; it does not replace backend accounting or verification services.

## Setup

```sh
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Set server-only `API_PROXY_TARGET` to the Django origin, for example `http://127.0.0.1:8000`. Browser requests use a same-origin `/api/v1` path through Next.js, avoiding cross-origin token/header exposure and custom-header preflight configuration. Start Django separately (`../.venv/bin/python ../manage.py runserver 127.0.0.1:8000`). `NEXT_PUBLIC_API_BASE_URL` can optionally target a direct CORS-enabled API, but the proxy is the default.

## Authentication and organization context

Sign-in uses `POST /auth/token/` with email and password. The short-lived access and refresh token pair exists in JavaScript memory only, is never persisted or written to logs, and is cleared on reload or sign-out. A `401` triggers one refresh attempt through `/auth/token/refresh/`; a failed refresh clears the session. Every organization-scoped request includes `X-Organization-ID` for an organization returned by `/organizations/`. The backend membership and role permissions remain authoritative.

## Available commands

```sh
npm run dev
npm run lint
npm run typecheck
npm test
npm run build
```

## Connected screens and current limits

- Dashboard and financial intelligence read `/analytics/financial-dashboard/`; values are rendered with API status, currency, warning, and reporting context. An unavailable result is not shown as zero.
- Financial statements call the trial balance, income statement, and balance sheet APIs. Account, period and journal tables call their ledger endpoints.
- Customers, invoices and payments are read from their organization-scoped APIs. Draft invoice issuance and authorized manual customer payment recording call the existing accounting endpoints after explicit confirmation. Payment requests carry an idempotency key; the API remains responsible for authorization, amount validation and ledger posting. Payment status cannot be edited from the browser.
- Reconciliation reads the unreconciled queue and candidate endpoint and can submit an explicitly confirmed match. Statement import/upload, investigation action and reconciliation-session finalize/reopen controls are not yet surfaced.
- The MauzoPilot screen reads connector configuration, health, mappings, and outbox and supports its test-connection and incremental sync actions. Secret fields are never echoed into the UI.
- NPV and investment appraisal inputs are sent to the backend. A live AI assistant interface is not included because the existing deterministic summary endpoints do not represent an external model connection.
- The UI does not yet expose invoice creation/details, full journal creation/post/reversal, accounting-period close, exports, statement file upload, reconciliation investigation/session controls, every metric endpoint, or an end-to-end browser test against a running API. Invoice and deposit-account selectors currently use the first paginated API page; ledger and invoice tables include backend page navigation.

Unit tests use mocked HTTP responses. They do not claim to verify a running Django server. No live payment provider is called by this frontend.
