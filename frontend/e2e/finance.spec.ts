import { expect, test, type Page, type APIRequestContext } from "@playwright/test";

// Real end-to-end journeys driven through the actual Next.js application and the
// actual Django REST API, backed by a disposable isolated PostgreSQL test database
// seeded with synthetic tenants only. Financial API calls are NOT mocked; only the
// browser confirm()/prompt() dialogs are auto-accepted.

const PW = "synthetic-browser-e2e";
const ACCOUNTANT = "accountant@browser-e2e.invalid";
const VIEWER = "viewer@browser-e2e.invalid";

async function signIn(page: Page, email = ACCOUNTANT) {
  await page.goto("/");
  await page.getByLabel("Email address").fill(email);
  await page.getByLabel("Password").fill(PW);
  await page.getByRole("button", { name: "Sign in securely" }).click();
  await expect(page.getByLabel("Organization", { exact: true })).toBeVisible();
}

// Session tokens live only in memory and are cleared by a full document reload,
// so every post-login navigation must be a client-side sidebar link click.
async function nav(page: Page, linkName: string) {
  await page.getByRole("link", { name: linkName }).click();
}

async function tokenFor(request: APIRequestContext, email = ACCOUNTANT) {
  const res = await request.post("/api/v1/auth/token/", { data: { email, password: PW } });
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  return body.access as string;
}

function auth(access: string, org?: string) {
  const headers: Record<string, string> = { Authorization: `Bearer ${access}` };
  if (org) headers["X-Organization-ID"] = org;
  return headers;
}

async function orgIdByName(request: APIRequestContext, access: string, name: string) {
  const res = await request.get("/api/v1/organizations/", { headers: auth(access) });
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  const list = Array.isArray(body) ? body : body.results;
  const match = list.find((o: { name: string }) => o.name === name);
  expect(match, `organization ${name} should be visible to this user`).toBeTruthy();
  return match.id as string;
}

// Journal creation via the composer form; returns the created entry number.
async function createJournalDraft(page: Page, entryNumber: string, amount = "250.00") {
  await page.getByRole("button", { name: "New journal entry" }).click();
  await page.getByLabel("Entry number").fill(entryNumber);
  await page.getByLabel("Description").fill("Synthetic browser E2E accrual");
  await page.getByLabel("Accounting period").selectOption({ index: 1 });
  await page.getByLabel("Line 1 account").selectOption({ label: "1000 · Cash on Hand" });
  await page.getByLabel("Line 2 account").selectOption({ label: "4000 · Sales & Services Revenue" });
  await page.getByLabel("Debit").first().fill(amount);
  await page.getByLabel("Credit").nth(1).fill(amount);
  await page.getByRole("button", { name: "Create draft entry" }).click();
  await expect(page.getByRole("status").filter({ hasText: `Draft ${entryNumber}` })).toBeVisible();
  return entryNumber;
}

function rowFor(page: Page, text: string) {
  return page.getByRole("row").filter({ hasText: text });
}

test.describe.configure({ mode: "serial" });

// Financial confirm()/prompt() dialogs are auto-accepted so intended mutations
// proceed; the API calls behind them are still the real, unmocked Django ones.
test.beforeEach(async ({ page }) => {
  page.on("dialog", (dialog) => void dialog.accept("Synthetic E2E reason"));
});

test("journey 1 — signs in through the real auth endpoint and refreshes the access token", async ({ page, request }) => {
  await signIn(page);
  // The dashboard shell is present once the authenticated org context is loaded.
  await expect(page.getByText("Financial controls active")).toBeVisible();
  // Refresh authentication: a real refresh exchange returns a fresh access token.
  const login = await request.post("/api/v1/auth/token/", { data: { email: ACCOUNTANT, password: PW } });
  const { refresh } = await login.json();
  const refreshed = await request.post("/api/v1/auth/token/refresh/", { data: { refresh } });
  expect(refreshed.status()).toBe(200);
  const { access } = await refreshed.json();
  expect(typeof access).toBe("string");
  expect(access.length).toBeGreaterThan(20);
});

test("journey 2 — selects an organization and verifies organization-scoped data", async ({ page, request }) => {
  await signIn(page);
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  const beta = await orgIdByName(request, access, "Browser Tenant Beta");
  const stamp = Date.now().toString().slice(-6);
  const entryNumber = `JE-A-${stamp}`;

  // Create a journal in Alpha through the UI.
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  await createJournalDraft(page, entryNumber);

  // It persists in Alpha (server read of the isolated DB) and is absent from Beta.
  const inAlpha = await request.get("/api/v1/ledger/journals/", { headers: auth(access, alpha) });
  const inBeta = await request.get("/api/v1/ledger/journals/", { headers: auth(access, beta) });
  const alphaBody = await inAlpha.json();
  const betaBody = await inBeta.json();
  expect(JSON.stringify(alphaBody)).toContain(entryNumber);
  expect(JSON.stringify(betaBody)).not.toContain(entryNumber);
});

test("journey 3 — creates a draft and posts a balanced journal, verifying returned accounting values", async ({ page, request }) => {
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  const stamp = Date.now().toString().slice(-6);
  const entryNumber = `JE-P-${stamp}`;
  await createJournalDraft(page, entryNumber);

  // Draft totals are zero until posting is validated by the accounting service.
  await rowFor(page, entryNumber).getByRole("button", { name: "Post" }).click();
  await expect(page.getByRole("status").filter({ hasText: `${entryNumber} posted` })).toContainText("POSTED");

  const list = await request.get("/api/v1/ledger/journals/", { headers: auth(access, alpha) });
  const found = ((await list.json()).results as Record<string, unknown>[]).find((r) => r.entry_number === entryNumber);
  expect(found).toBeTruthy();
  expect(String(found!.status)).toBe("POSTED");
  expect(String(found!.total_debit)).toBe("250.0000");
  expect(String(found!.total_credit)).toBe("250.0000");
});

test("journey 4 — reverses an eligible posted journal and verifies the reversal relationship and balances", async ({ page, request }) => {
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  const stamp = Date.now().toString().slice(-6);
  const entryNumber = `JE-R-${stamp}`;
  await createJournalDraft(page, entryNumber);
  await rowFor(page, entryNumber).getByRole("button", { name: "Post" }).click();
  await expect(rowFor(page, entryNumber).getByText(/POSTED/i)).toBeVisible();

  await rowFor(page, entryNumber).getByRole("button", { name: "Reverse" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Reversal recorded" })).toBeVisible();

  // Server state: the original is REVERSED and a new POSTED reversal entry exists.
  const results = ((await (await request.get("/api/v1/ledger/journals/", { headers: auth(access, alpha) })).json()).results) as Record<string, unknown>[];
  const original = results.find((r) => r.entry_number === entryNumber);
  expect(String(original!.status)).toBe("REVERSED");
  const reversal = results.find((r) => r.entry_number === `REV-${entryNumber}`);
  expect(reversal, "a REV-<original> reversing entry must be created by the backend").toBeTruthy();
  expect(String(reversal!.status)).toBe("POSTED");
});

test("journey 5 — creates an invoice, inspects details, issues it, and verifies persisted status and accounting effect", async ({ page, request }) => {
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  await nav(page, "Invoices & customers");
  const stamp = Date.now().toString().slice(-6);
  const invoiceNumber = `INV-${stamp}`;
  await page.getByRole("button", { name: "New draft invoice" }).click();
  await page.getByLabel("Invoice number").fill(invoiceNumber);
  await page.getByLabel("Customer").selectOption({ label: "Synthetic Acme Ltd" });
  await page.getByLabel("Item 1 revenue account").selectOption({ label: "4000 · Sales & Services Revenue" });
  await page.getByLabel("Description").fill("Synthetic professional services");
  await page.getByLabel("Unit price").fill("1200.00");
  await page.getByRole("button", { name: "Create draft invoice" }).click();
  // The composer closes on success; the persisted draft is confirmed by the register row.
  // Target the status pill exactly so it does not also match the "Issue draft" button.
  await expect(rowFor(page, invoiceNumber).getByText("DRAFT", { exact: true })).toBeVisible();

  // Issue it: posts receivables through the backend (a separate, confirmed action).
  await rowFor(page, invoiceNumber).getByRole("button", { name: "Issue draft" }).click();
  await expect(page.getByRole("status").filter({ hasText: `${invoiceNumber} issued` })).toBeVisible();

  // Inspect the issued invoice detail (the register now offers View for non-draft rows).
  await rowFor(page, invoiceNumber).getByRole("button", { name: "View" }).click();
  await expect(page.getByText("Invoice detail")).toBeVisible();

  const detail = await request.get(`/api/v1/invoicing/invoices/?page=1`, { headers: auth(access, alpha) });
  const found = ((await detail.json()).results as Record<string, unknown>[]).find((r) => r.invoice_number === invoiceNumber);
  expect(found).toBeTruthy();
  expect(String(found!.status)).toBe("ISSUED");
  expect(String(found!.total_amount)).toBe("1200.0000");

  // Accounting effect: issuing posts a journal to the ledger.
  const journals = await request.get("/api/v1/ledger/journals/", { headers: auth(access, alpha) });
  expect(JSON.stringify(await journals.json())).toContain(`JE-INV-${invoiceNumber}`);
});

test("journey 6 — uploads a synthetic bank statement and reviews the import results (dedup on repeat)", async ({ page, request }) => {
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  await nav(page, "Reconciliation");
  await page.getByLabel("Financial account").selectOption({ label: "1010 · Operating Bank Account" });
  await page.getByLabel("Statement format").selectOption("BANK_CSV");
  await page.getByLabel("Statement file").setInputFiles("e2e/fixtures/statement.csv");
  await page.getByRole("button", { name: "Upload statement" }).click();
  await expect(page.getByRole("status").filter({ hasText: "statement.csv" })).toContainText("imported 3");

  // Re-uploading the identical file is rejected by the server (SHA-256 batch-hash
  // dedup): the API returns 400 and the UI surfaces it as a role=alert error, not a
  // success notice — proving the same statement is never double-ingested.
  await page.getByLabel("Statement file").setInputFiles("e2e/fixtures/statement.csv");
  await page.getByRole("button", { name: "Upload statement" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /already been imported/i })).toBeVisible();

  const batches = await request.get("/api/v1/payments/statement-imports/", { headers: auth(access, alpha) });
  const list = (await batches.json()) as Record<string, unknown>[];
  const mine = list.filter((b) => String(b.file_name) === "statement.csv");
  // Exactly one batch persisted despite two upload attempts.
  expect(mine.length).toBe(1);
});

test("journey 7 — creates a reconciliation session, finalizes a balanced session, and reopens it", async ({ page, request }) => {
  const access = await tokenFor(request);
  await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  await nav(page, "Reconciliation");
  await page.getByRole("button", { name: "New reconciliation session" }).click();
  await page.getByRole("combobox", { name: "Account", exact: true }).selectOption({ label: "1010 \u00b7 Operating Bank Account" });
  await page.getByLabel("Statement closing balance").fill("0.00");
  await page.getByRole("button", { name: "Create session" }).click();

  // Select the freshly created session in the queue. Both the transactions panel and
  // the sessions panel render .queue-row buttons, so scope by the REC- session number.
  const sessionRow = page.getByRole("button").filter({ hasText: /REC-/ }).first();
  await expect(sessionRow).toBeVisible();
  const sessionNumber = (await sessionRow.locator("b").first().innerText()).trim();
  await sessionRow.click();

  await page.getByRole("button", { name: "Finalize session" }).click();
  await expect(page.getByRole("status").filter({ hasText: sessionNumber })).toContainText("finalized");
  expect(sessionNumber).toMatch(/^REC/);

  await page.getByRole("button", { name: "Reopen session" }).click();
  await expect(page.getByRole("status").filter({ hasText: sessionNumber })).toContainText("reopened");
});

test("journey 8 — surfaces meaningful validation, success, and failure feedback", async ({ page }) => {
  await signIn(page);
  // Validation feedback without hitting the API: upload with no file selected.
  await nav(page, "Reconciliation");
  await page.getByLabel("Financial account").selectOption({ label: "1010 · Operating Bank Account" });
  await page.getByRole("button", { name: "Upload statement" }).click();
  await expect(page.getByRole("alert").filter({ hasText: /Choose a statement file/i })).toBeVisible();

  // Success feedback: creating a draft journal shows a role=status notice.
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  const stamp = Date.now().toString().slice(-6);
  await createJournalDraft(page, `JE-FB-${stamp}`);
  await expect(page.getByRole("status").filter({ hasText: `Draft JE-FB-${stamp}` })).toBeVisible();
});

test("journey 9 — retrying an uncertain creation with the same idempotency key does not duplicate financial records", async ({ page, request }) => {
  const access = await tokenFor(request);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  await signIn(page);
  const stamp = Date.now().toString().slice(-6);
  const entryNumber = `JE-IDEM-${stamp}`;

  let periodId = "";
  const periods = await request.get("/api/v1/ledger/periods/", { headers: auth(access, alpha) });
  const pl = (await periods.json());
  const periodsList = Array.isArray(pl) ? pl : pl.results;
  periodId = String(periodsList.find((p: { status: string }) => p.status === "OPEN")!.id);
  const accountsResp = await request.get("/api/v1/ledger/accounts/?page=1", { headers: auth(access, alpha) });
  const accList = (await accountsResp.json()).results as Record<string, unknown>[];
  const cash = String(accList.find((a) => a.code === "1000")!.id);
  const revenue = String(accList.find((a) => a.code === "4000")!.id);

  const payload = {
    entry_number: entryNumber,
    period_id: periodId,
    date: "2026-01-15",
    description: "Idempotent uncertain retry",
    reference: "E2E",
    currency: "KES",
    lines: [
      { account_id: cash, description: "Dr", debit: "500.0000", credit: "0.0000", sequence: 1 },
      { account_id: revenue, description: "Cr", debit: "0.0000", credit: "500.0000", sequence: 2 },
    ],
  };
  const key = `e2e-idem-${stamp}`;

  const first = await request.post("/api/v1/ledger/journals/", { headers: { ...auth(access, alpha), "Idempotency-Key": key }, data: payload });
  expect(first.status()).toBe(201);
  const firstId = (await first.json()).id;

  // A retry of the same uncertain request replays the original result.
  const replay = await request.post("/api/v1/ledger/journals/", { headers: { ...auth(access, alpha), "Idempotency-Key": key }, data: payload });
  expect(replay.status()).toBe(201);
  expect((await replay.json()).id).toBe(firstId);

  // Exactly one persisted financial record exists despite two deliveries.
  const list = await request.get("/api/v1/ledger/journals/", { headers: auth(access, alpha) });
  const matches = ((await list.json()).results as Record<string, unknown>[]).filter((r) => r.entry_number === entryNumber);
  expect(matches.length).toBe(1);

  // The UI register agrees with the persisted single record.
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  await expect(rowFor(page, entryNumber)).toHaveCount(1);
});

test("journey 10 — a Viewer cannot perform restricted mutations or reach another organization by changing X-Organization-ID", async ({ page, request }) => {
  const access = await tokenFor(request, VIEWER);
  const alpha = await orgIdByName(request, access, "Browser Tenant Alpha");
  // The viewer is only a member of Alpha; Beta must not be discoverable.
  const orgsResp = await request.get("/api/v1/organizations/", { headers: auth(access) });
  const orgList = (await orgsResp.json());
  const orgsArr = Array.isArray(orgList) ? orgList : orgList.results;
  expect(orgsArr.some((o: { name: string }) => o.name === "Browser Tenant Beta")).toBe(false);
  const beta = await tokenFor(request, ACCOUNTANT).then((a) => orgIdByName(request, a, "Browser Tenant Beta"));

  // Restricted mutation on their own org is rejected by the server (Viewer is read-only).
  const forbidden = await request.post("/api/v1/ledger/journals/", { headers: auth(access, alpha), data: {} });
  expect(forbidden.status()).toBe(403);

  // Cross-organization isolation: spoofing X-Organization-ID to Beta is rejected.
  const crossOrg = await request.get("/api/v1/ledger/journals/", { headers: auth(access, beta) });
  expect(crossOrg.status()).toBe(403);

  // The UI reflects the restriction: no creation affordance for a Viewer.
  await signIn(page, VIEWER);
  await nav(page, "Chart of accounts & journals");
  await page.getByRole("button", { name: "Journal entries" }).click();
  await expect(page.getByRole("button", { name: "New journal entry" })).toHaveCount(0);
});
