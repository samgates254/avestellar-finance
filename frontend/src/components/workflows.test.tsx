import * as React from "react";
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setOrganization, setSession } from "@/lib/api";
import { SessionProvider, useSession } from "@/components/session";
import { Journals } from "@/components/journals";
import { StatementImports } from "@/components/statements";
import { ReconciliationSessions } from "@/components/sessions";
import { InvoiceWorkspace } from "@/components/invoice-workspace";

const ORG = { id: "org-1", name: "Synthetic Finance", currency: "KES", currency_symbol: "KSh", decimal_places: 2, user_role: "ACCOUNTANT" };

// Signs in through the real session flow so components render with an active organization.
function Harness({ children }: { children: React.ReactNode }) {
  const { tokens, organization, login } = useSession();
  React.useEffect(() => { if (!tokens) void login("finance@example.test", "long-test-password"); }, [tokens, login]);
  return organization ? <>{children}</> : null;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function mockFetch(routes: Record<string, () => Response>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  // Prefer the most specific (longest) matching route so a list key such as
  // "/payments/reconciliation/sessions/" does not shadow ".../s1/finalize/".
  const ordered = Object.entries(routes).sort((a, b) => b[0].length - a[0].length);
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    calls.push({ url, init });
    for (const [match, build] of ordered) if (url.includes(match)) return build();
    return json([]);
  });
  return { calls, spy };
}

function baseRoutes() {
  return {
    "/auth/token/": () => json({ access: "access-test", refresh: "refresh-test" }),
    "/auth/me/": () => json({ email: "finance@example.test", full_name: "Finance User" }),
    "/organizations/": () => json({ count: 1, results: [ORG] }),
  } as Record<string, () => Response>;
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); setSession(null, null); setOrganization(null); vi.unstubAllGlobals(); });

describe("journal entry workflow", () => {
  it("creates a balanced draft with string amounts through the ledger API", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 2, results: [{ id: "a1", code: "1000", name: "Cash", is_active: true, category: "ASSET", system_role: "CASH" }, { id: "a2", code: "4000", name: "Sales", is_active: true, category: "REVENUE", system_role: "" }] };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/periods/": () => json({ count: 1, results: [{ id: "p1", name: "2026-P1", status: "OPEN" }] }), "/ledger/accounts/": () => json(accounts), "/ledger/journals/?": () => json({ count: 0, results: [] }) });

    render(<SessionProvider><Harness><Journals /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("New journal entry"));
    await screen.findByText("2026-P1");
    await waitFor(() => expect(screen.getAllByText("1000 · Cash").length).toBeGreaterThan(0));
    fireEvent.change(screen.getByLabelText(/Entry number/i), { target: { value: "JE-2026-0001" } });
    fireEvent.change(screen.getByLabelText(/Description/i), { target: { value: "Manual accrual" } });
    fireEvent.change(screen.getByLabelText(/Accounting period/i), { target: { value: "p1" } });
    const accountSelects = screen.getAllByLabelText(/account$/i);
    fireEvent.change(accountSelects[0], { target: { value: "a1" } });
    fireEvent.change(accountSelects[1], { target: { value: "a2" } });
    const debits = screen.getAllByLabelText(/^Debit$/i);
    const credits = screen.getAllByLabelText(/^Credit$/i);
    fireEvent.change(debits[0], { target: { value: "250.00" } });
    fireEvent.change(credits[1], { target: { value: "250.00" } });
    fireEvent.click(screen.getByText("Create draft entry"));

    await waitFor(() => {
      const post = calls.find((c) => c.url.endsWith("/ledger/journals/") && c.init?.method === "POST");
      expect(post).toBeTruthy();
      const body = JSON.parse(String(post?.init?.body));
      expect(body.entry_number).toBe("JE-2026-0001");
      expect(body.period_id).toBe("p1");
      expect(body.lines).toHaveLength(2);
      // Decimal precision preserved: amounts are transmitted as strings, not floats.
      expect(typeof body.lines[0].debit).toBe("string");
      expect(body.lines[0].debit).toBe("250.00");
      // A replay-safe Idempotency-Key is transmitted for the creation.
      const headers = new Headers(post?.init?.headers as HeadersInit);
      expect(headers.get("Idempotency-Key")).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
    });
  });

  it("posts a draft entry only via the dedicated post endpoint after confirmation", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const draft = { id: "j1", entry_number: "JE-1", date: "2026-01-05", description: "Test", status: "DRAFT", currency: "KES", total_debit: "0.0000", total_credit: "0.0000" };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/periods/": () => json({ results: [] }), "/ledger/accounts/": () => json({ results: [] }), "/ledger/journals/?": () => json({ count: 1, results: [draft] }), "/ledger/journals/j1/post/": () => json({ ...draft, status: "POSTED", total_debit: "100.0000", total_credit: "100.0000" }) });

    render(<SessionProvider><Harness><Journals /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("Post"));
    await waitFor(() => {
      const post = calls.find((c) => c.url.endsWith("/ledger/journals/j1/post/") && c.init?.method === "POST");
      expect(post).toBeTruthy();
    });
  });
});

describe("statement import workflow", () => {
  it("blocks upload without a file and never posts to the API", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 1, results: [{ id: "a1", code: "1100", name: "Bank", is_active: true, system_role: "BANK" }] };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/accounts/": () => json(accounts), "/payments/statement-imports/": () => json([]) });

    render(<SessionProvider><Harness><StatementImports /></Harness></SessionProvider>);
    const accountSelect = await screen.findByLabelText(/Financial account/i);
    await screen.findByText("1100 · Bank");
    fireEvent.change(accountSelect, { target: { value: "a1" } });
    fireEvent.click(await screen.findByText("Upload statement"));
    await screen.findByText(/Choose a statement file/i);
    expect(calls.some((c) => c.url.endsWith("/payments/statement-imports/") && c.init?.method === "POST")).toBe(false);
  });

  it("posts a multipart upload carrying the account and source for evidence ingest", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 1, results: [{ id: "a1", code: "1100", name: "Bank", is_active: true, system_role: "BANK" }] };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/accounts/": () => json(accounts), "/payments/statement-imports/": () => json({ id: "b1", file_name: "bank.csv", import_source: "BANK_CSV", status: "COMPLETED", total_rows: 3, imported_count: 3, duplicate_count: 0, rejected_count: 0, error_details: [], validation_summary: {} }) });

    render(<SessionProvider><Harness><StatementImports /></Harness></SessionProvider>);
    const accountSelect = await screen.findByLabelText(/Financial account/i);
    await screen.findByText("1100 · Bank");
    fireEvent.change(accountSelect, { target: { value: "a1" } });
    const fileInput = screen.getByLabelText(/Statement file/i) as HTMLInputElement;
    Object.defineProperty(fileInput, "files", { value: [new File(["date,ref,amount\n2026-01-01,R1,10"], "bank.csv", { type: "text/csv" })] });
    fireEvent.click(await screen.findByText("Upload statement"));

    await waitFor(() => {
      const post = calls.find((c) => c.url.endsWith("/payments/statement-imports/") && c.init?.method === "POST");
      expect(post?.init?.body).toBeInstanceOf(FormData);
      const form = post?.init?.body as FormData;
      expect(form.get("financial_account_id")).toBe("a1");
      expect(form.get("import_source")).toBe("BANK_CSV");
      expect(form.get("file")).toBeInstanceOf(File);
    });
  });
});

describe("reconciliation session workflow", () => {
  it("creates a session and posts statement balances to the sessions API", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 1, results: [{ id: "a1", code: "1100", name: "Bank", is_active: true, system_role: "BANK" }] };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/accounts/": () => json(accounts), "/payments/reconciliation/sessions/": () => json([]) });

    render(<SessionProvider><Harness><ReconciliationSessions /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("New reconciliation session"));
    await screen.findByText("1100 · Bank");
    fireEvent.change(screen.getByLabelText(/^Account$/i), { target: { value: "a1" } });
    fireEvent.change(screen.getByLabelText(/Statement closing balance/i), { target: { value: "54321.75" } });
    fireEvent.click(screen.getByText("Create session"));

    await waitFor(() => {
      const post = calls.find((c) => c.url.endsWith("/payments/reconciliation/sessions/") && c.init?.method === "POST");
      const body = JSON.parse(String(post?.init?.body));
      expect(body.financial_account_id).toBe("a1");
      expect(body.statement_closing_balance).toBe("54321.75");
    });
  });

  it("finalizes a balanced session through the finalize endpoint", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const session = { id: "s1", session_number: "REC-1100-202601", financial_account_code: "1100", financial_account_name: "Bank", period_start: "2026-01-01", period_end: "2026-01-31", statement_closing_balance: "100.0000", ledger_balance: "100.0000", difference: "0.0000", status: "DRAFT" };
    const { calls } = mockFetch({ ...baseRoutes(), "/ledger/accounts/": () => json({ results: [] }), "/payments/reconciliation/sessions/": () => json([session]), "/payments/reconciliation/sessions/s1/": () => json(session), "/payments/reconciliation/sessions/s1/finalize/": () => json({ ...session, status: "FINALIZED" }) });

    render(<SessionProvider><Harness><ReconciliationSessions /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("REC-1100-202601"));
    fireEvent.click(await screen.findByText("Finalize session"));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/payments/reconciliation/sessions/s1/finalize/") && c.init?.method === "POST")).toBe(true);
    });
  });
});

describe("invoice workflow", () => {
  it("creates a draft invoice with line items via the invoicing API", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 1, results: [{ id: "r1", code: "4000", name: "Sales", is_active: true, category: "REVENUE" }] };
    const { calls } = mockFetch({ ...baseRoutes(), "/invoicing/customers/": () => json({ count: 1, results: [{ id: "c1", name: "Acme", is_active: true }] }), "/ledger/accounts/": () => json(accounts), "/invoicing/invoices/?": () => json({ count: 0, results: [] }) });

    render(<SessionProvider><Harness><InvoiceWorkspace /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("New draft invoice"));
    await screen.findByText("4000 · Sales");
    fireEvent.change(screen.getByLabelText(/Invoice number/i), { target: { value: "INV-2026-0001" } });
    fireEvent.change(screen.getByLabelText(/^Customer$/i), { target: { value: "c1" } });
    fireEvent.change(screen.getByLabelText(/revenue account/i), { target: { value: "r1" } });
    fireEvent.change(screen.getByLabelText(/Unit price/i), { target: { value: "1000.00" } });
    fireEvent.click(screen.getByText("Create draft invoice"));

    await waitFor(() => {
      const post = calls.find((c) => c.url.endsWith("/invoicing/invoices/") && c.init?.method === "POST");
      const body = JSON.parse(String(post?.init?.body));
      expect(body.invoice_number).toBe("INV-2026-0001");
      expect(body.customer_id).toBe("c1");
      expect(body.items[0].revenue_account_id).toBe("r1");
      expect(body.items[0].unit_price).toBe("1000.00");
      // A replay-safe Idempotency-Key is transmitted for the creation.
      const headers = new Headers(post?.init?.headers as HeadersInit);
      expect(headers.get("Idempotency-Key")).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i);
    });
  });

  it("reuses the same idempotency key when an uncertain creation is retried", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const accounts = { count: 1, results: [{ id: "r1", code: "4000", name: "Sales", is_active: true, category: "REVENUE" }] };
    let attempt = 0;
    const { calls } = mockFetch({
      ...baseRoutes(),
      "/invoicing/customers/": () => json({ count: 1, results: [{ id: "c1", name: "Acme", is_active: true }] }),
      "/ledger/accounts/": () => json(accounts),
      "/invoicing/invoices/?": () => json({ count: 0, results: [] }),
      "/invoicing/invoices/": () => {
        attempt += 1;
        // First submission returns an ambiguous gateway error; the retry succeeds.
        if (attempt === 1) return new Response("{}", { status: 502, headers: { "Content-Type": "application/json" } });
        return json({ id: "inv1", invoice_number: "INV-2026-0001", status: "DRAFT", currency: "KES", total_amount: "1000.0000", outstanding_balance: "1000.0000" });
      },
    });

    render(<SessionProvider><Harness><InvoiceWorkspace /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("New draft invoice"));
    await screen.findByText("4000 · Sales");
    fireEvent.change(screen.getByLabelText(/Invoice number/i), { target: { value: "INV-2026-0001" } });
    fireEvent.change(screen.getByLabelText(/^Customer$/i), { target: { value: "c1" } });
    fireEvent.change(screen.getByLabelText(/revenue account/i), { target: { value: "r1" } });
    fireEvent.change(screen.getByLabelText(/Unit price/i), { target: { value: "1000.00" } });
    fireEvent.click(screen.getByText("Create draft invoice"));
    // Wait for the failed first attempt to settle before retrying.
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/invoicing/invoices/") && c.init?.method === "POST").length).toBe(1));
    fireEvent.click(screen.getByText("Create draft invoice"));

    await waitFor(() => {
      const posts = calls.filter((c) => c.url.endsWith("/invoicing/invoices/") && c.init?.method === "POST");
      expect(posts.length).toBeGreaterThanOrEqual(2);
      const keys = posts.map((p) => new Headers(p.init?.headers as HeadersInit).get("Idempotency-Key"));
      expect(keys[0]).toBeTruthy();
      // The identical intended creation is retried with the SAME key so the backend
      // cannot double-insert the invoice.
      expect(keys[1]).toBe(keys[0]);
    });
  });

  it("issues a draft invoice only through the issue endpoint", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    const draft = { id: "i1", invoice_number: "INV-1", customer_name: "Acme", issue_date: "2026-01-05", due_date: "2026-02-04", total_amount: "500.0000", outstanding_balance: "500.0000", status: "DRAFT", currency: "KES" };
    const { calls } = mockFetch({ ...baseRoutes(), "/invoicing/customers/": () => json({ results: [] }), "/ledger/accounts/": () => json({ results: [] }), "/invoicing/invoices/?": () => json({ count: 1, results: [draft] }), "/invoicing/invoices/i1/issue/": () => json({ ...draft, status: "ISSUED" }) });

    render(<SessionProvider><Harness><InvoiceWorkspace /></Harness></SessionProvider>);
    fireEvent.click(await screen.findByText("Issue draft"));
    await waitFor(() => {
      expect(calls.some((c) => c.url.endsWith("/invoicing/invoices/i1/issue/") && c.init?.method === "POST")).toBe(true);
    });
  });
});
