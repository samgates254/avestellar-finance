"use client";
import { useState } from "react";
import { api, Page } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { money, dateOnly } from "@/lib/format";
import { useSession } from "@/components/session";
import { Button, Card, Empty, ErrorBox, Field, Loading, StatusPill, useLoad } from "@/components/ui";

type Row = Record<string, unknown>;
const EDIT_ROLES = new Set(["OWNER", "ADMIN", "ACCOUNTANT"]);
const DAY = 86400000;

type ItemDraft = { revenue_account_id: string; description: string; quantity: string; unit_price: string; tax_rate: string };

function InvoiceComposer({ customers, revenueAccounts, onCreated }: { customers: Row[]; revenueAccounts: Row[]; onCreated: () => void }) {
  const { organization } = useSession();
  const idempotency = useIdempotencyKey();
  const [invoiceNumber, setInvoiceNumber] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [issueDate, setIssueDate] = useState(dateOnly());
  const [dueDate, setDueDate] = useState(dateOnly(new Date(Date.now() + 30 * DAY)));
  const [notes, setNotes] = useState("");
  const [items, setItems] = useState<ItemDraft[]>([{ revenue_account_id: "", description: "", quantity: "1", unit_price: "", tax_rate: "0" }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<Row | null>(null);

  function updateItem(index: number, patch: Partial<ItemDraft>) {
    setItems((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null); setResult(null);
    if (!invoiceNumber.trim() || !customerId) { setError(new Error("Provide an invoice number and select a customer.")); return; }
    const payloadItems = items.filter((item) => item.revenue_account_id && item.unit_price);
    if (!payloadItems.length) { setError(new Error("Add at least one line item with a revenue account and a unit price.")); return; }
    if (!window.confirm(`Create draft invoice ${invoiceNumber}? Totals are calculated and validated by the accounting API. Issuing to post receivables is a separate, confirmed action.`)) return;
    const body = {
      invoice_number: invoiceNumber.trim(),
      customer_id: customerId,
      issue_date: issueDate,
      due_date: dueDate,
      currency: organization?.currency || "KES",
      notes: notes.trim(),
      items: payloadItems.map((item) => ({ revenue_account_id: item.revenue_account_id, description: item.description, quantity: item.quantity || "1", unit_price: item.unit_price, tax_rate: item.tax_rate || "0" })),
    };
    setBusy(true);
    // Stable key per intended creation so an uncertain retry replays the original draft
    // instead of creating a duplicate invoice; a changed payload gets a new key.
    const payload = JSON.stringify(body);
    try { const created = await api<Row>("/invoicing/invoices/", { method: "POST", headers: { "Idempotency-Key": idempotency.keyFor(payload) }, body: payload }); setResult(created); idempotency.reset(); onCreated(); }
    catch (reason) { setError(reason); } finally { setBusy(false); }
  }

  const currency = String(organization?.currency || "KES");
  return <Card title="New draft invoice" description="Create an unposted draft invoice with line items. Issue it from the register to recognize receivables and revenue through the backend.">
    <form onSubmit={(event) => void submit(event)}>
      <div className="form-grid">
        <Field label="Invoice number"><input required value={invoiceNumber} onChange={(e) => setInvoiceNumber(e.target.value)} placeholder="e.g. INV-2026-0001" /></Field>
        <Field label="Customer"><select required value={customerId} onChange={(e) => setCustomerId(e.target.value)}><option value="">Choose customer</option>{customers.filter((customer) => customer.is_active).map((customer) => <option key={String(customer.id)} value={String(customer.id)}>{String(customer.name)}</option>)}</select></Field>
        <Field label="Issue date"><input type="date" required value={issueDate} onChange={(e) => setIssueDate(e.target.value)} /></Field>
        <Field label="Due date"><input type="date" required value={dueDate} onChange={(e) => setDueDate(e.target.value)} /></Field>
      </div>
      <Field label="Notes"><input value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
      <div className="spacer" />
      {items.map((item, index) => <div className="form-grid" key={index}>
        <Field label={`Item ${index + 1} revenue account`}><select value={item.revenue_account_id} onChange={(e) => updateItem(index, { revenue_account_id: e.target.value })}><option value="">Choose revenue account</option>{revenueAccounts.map((account) => <option key={String(account.id)} value={String(account.id)}>{String(account.code)} · {String(account.name)}</option>)}</select></Field>
        <Field label="Description"><input value={item.description} onChange={(e) => updateItem(index, { description: e.target.value })} /></Field>
        <Field label="Quantity"><input inputMode="decimal" value={item.quantity} onChange={(e) => updateItem(index, { quantity: e.target.value })} /></Field>
        <Field label="Unit price"><input inputMode="decimal" value={item.unit_price} onChange={(e) => updateItem(index, { unit_price: e.target.value })} placeholder="0.00" /></Field>
        <Field label="Tax rate (decimal)"><input inputMode="decimal" value={item.tax_rate} onChange={(e) => updateItem(index, { tax_rate: e.target.value })} placeholder="e.g. 0.16" /></Field>
      </div>)}
      <div className="button-row">
        <Button variant="secondary" onClick={() => setItems((current) => [...current, { revenue_account_id: "", description: "", quantity: "1", unit_price: "", tax_rate: "0" }])}>Add item</Button>
        <Button variant="secondary" disabled={items.length <= 1} onClick={() => setItems((current) => current.slice(0, -1))}>Remove item</Button>
        <Button type="submit" disabled={busy}>{busy ? "Creating draft…" : "Create draft invoice"}</Button>
      </div>
      <ErrorBox error={error} />
    </form>
    {result && <div className="notice" role="status">Draft <b>{String(result.invoice_number)}</b> created by the API with status <StatusPill value={String(result.status)} />. Total {money(String(result.total_amount), String(result.currency || currency))}, outstanding {money(String(result.outstanding_balance), String(result.currency || currency))}. Issue it from the register to post receivables.</div>}
  </Card>;
}

function InvoiceDetail({ id }: { id: string }) {
  const { organization } = useSession();
  const { data, loading, error, retry } = useLoad(() => api<Row>(`/invoicing/invoices/${id}/`), [id]);
  if (loading) return <Loading label="Loading invoice…" />;
  if (error) return <ErrorBox error={error} retry={retry} />;
  if (!data) return null;
  const currency = String(data.currency || organization?.currency || "KES");
  const items = (data.items as Row[]) || [];
  return <div>
    <div className="context-pairs"><span>Status</span><b><StatusPill value={String(data.status)} /></b><span>Customer</span><b>{String(data.customer_name ?? "—")}</b><span>Issue date</span><b>{String(data.issue_date)}</b><span>Due date</span><b>{String(data.due_date)}</b><span>Subtotal</span><b>{money(String(data.subtotal), currency)}</b><span>Tax</span><b>{money(String(data.tax_amount), currency)}</b><span>Total</span><b>{money(String(data.total_amount), currency)}</b><span>Paid</span><b>{money(String(data.paid_amount), currency)}</b><span>Outstanding</span><b>{money(String(data.outstanding_balance), currency)}</b></div>
    {items.length ? <div className="table-wrap" style={{ marginTop: 12 }}><table><thead><tr><th>Account</th><th>Description</th><th>Qty</th><th>Unit price</th><th>Tax</th><th>Line total</th></tr></thead><tbody>{items.map((item, index) => <tr key={String(item.id ?? index)}><td>{String(item.revenue_account_code ?? "—")}</td><td>{String(item.description || "—")}</td><td>{String(item.quantity)}</td><td>{money(String(item.unit_price), currency)}</td><td>{money(String(item.tax_amount), currency)}</td><td>{money(String(item.line_total), currency)}</td></tr>)}</tbody></table></div> : <Empty>This invoice has no line items.</Empty>}
  </div>;
}

export function InvoiceWorkspace() {
  const { organization } = useSession();
  const canEdit = !!organization && EDIT_ROLES.has(String(organization.user_role));
  const [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState("invoices");
  const [showComposer, setShowComposer] = useState(false);
  const [detailId, setDetailId] = useState("");
  const [message, setMessage] = useState("");
  const [busyId, setBusyId] = useState("");

  const path = tab === "invoices" ? "/invoicing/invoices/" : "/invoicing/customers/";
  const list = useLoad(() => api<Page<Row>>(`${path}?page=1`), [organization?.id, tab, refresh]);
  const customers = useLoad(() => api<Page<Row>>("/invoicing/customers/?page=1"), [organization?.id]);
  const accounts = useLoad(() => api<Page<Row>>("/ledger/accounts/?page=1"), [organization?.id]);
  const revenueAccounts = (accounts.data?.results ?? []).filter((account) => account.is_active && String(account.category) === "REVENUE");
  const rows = list.data?.results || [];

  async function issue(row: Row) {
    const number = String(row.invoice_number || "this draft");
    if (!window.confirm(`Issue invoice ${number}? This posts accounts receivable, revenue and any tax to the general ledger through the backend accounting service.`)) return;
    setBusyId(String(row.id)); setMessage("");
    try { const response = await api<Row>(`/invoicing/invoices/${row.id}/issue/`, { method: "POST" }); setMessage(`Invoice ${String(response.invoice_number || number)} issued. The API confirms status ${String(response.status)} with total ${money(String(response.total_amount), String(response.currency || organization?.currency || "KES"))}.`); setRefresh((value) => value + 1); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "Invoice issuance failed."); } finally { setBusyId(""); }
  }

  if (!organization) return <Card><Empty>Select an organization to manage invoices.</Empty></Card>;

  const columns: [string, string][] = tab === "invoices" ? [["invoice_number", "Invoice"], ["customer_name", "Customer"], ["issue_date", "Issued"], ["due_date", "Due"], ["total_amount", "Total"], ["outstanding_balance", "Outstanding"], ["status", "Status"]] : [["name", "Customer"], ["email", "Email"], ["phone", "Phone"], ["currency", "Currency"], ["is_active", "Active"]];

  return <>
    {message && <div className="notice" role="status">{message}</div>}
    {canEdit && <div className="button-row" style={{ marginBottom: 14 }}><Button variant={showComposer ? "secondary" : "primary"} onClick={() => setShowComposer((value) => !value)}>{showComposer ? "Hide invoice form" : "New draft invoice"}</Button></div>}
    {showComposer && canEdit && <InvoiceComposer customers={customers.data?.results ?? []} revenueAccounts={revenueAccounts} onCreated={() => { setShowComposer(false); setRefresh((value) => value + 1); }} />}
    <Card title="Receivables" description="Invoice balances, statuses and totals come from the accounting API. Issuance is enforced by the backend’s services and organization roles.">
      <div className="tab-bar">{[["invoices", "Invoices"], ["customers", "Customers"]].map(([key, label]) => <button key={key} className={tab === key ? "tab active" : "tab"} onClick={() => setTab(key)}>{label}</button>)}</div>
      {list.loading ? <Loading /> : <><ErrorBox error={list.error} retry={list.retry} />{rows.length ? <div className="table-wrap"><table><thead><tr>{columns.map(([, label]) => <th key={label}>{label}</th>)}{tab === "invoices" && canEdit && <th>Actions</th>}</tr></thead><tbody>
        {rows.map((row) => <tr key={String(row.id)}>
          {columns.map(([key]) => { const value = row[key]; const moneyColumn = ["total_amount", "outstanding_balance"].includes(key); return <td key={key}>{key === "status" ? <StatusPill value={String(value ?? "")} /> : moneyColumn ? money(String(value ?? "0"), String(row.currency || organization.currency)) : value == null || value === "" ? "—" : String(value).replaceAll("_", " ")}</td>; })}
          {tab === "invoices" && canEdit && <td><div className="button-row" style={{ margin: 0 }}>{String(row.status) === "DRAFT" ? <Button disabled={busyId === String(row.id)} onClick={() => void issue(row)}>{busyId === String(row.id) ? "Issuing…" : "Issue draft"}</Button> : <Button variant="secondary" onClick={() => setDetailId(String(row.id))}>View</Button>}</div></td>}
        </tr>)}
      </tbody></table></div> : <Empty>No records were returned for this view.</Empty>}</>}
    </Card>
    {detailId && <Card title="Invoice detail" description="Line items and balances as returned by the detail endpoint."><div className="button-row" style={{ marginBottom: 12, justifyContent: "flex-end" }}><Button variant="secondary" onClick={() => setDetailId("")}>Close</Button></div><InvoiceDetail id={detailId} /></Card>}
  </>;
}
