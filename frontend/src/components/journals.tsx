"use client";
import { useState } from "react";
import { api, Page } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { money, dateOnly } from "@/lib/format";
import { useSession } from "@/components/session";
import { Button, Card, Empty, ErrorBox, Field, Loading, StatusPill, useLoad } from "@/components/ui";

type Row = Record<string, unknown>;

// Roles permitted by the backend to create/post/reverse journals (IsAccountantOrAbove).
// This only gates UI affordances; the server enforces authorization independently.
const EDIT_ROLES = new Set(["OWNER", "ADMIN", "ACCOUNTANT"]);

type LineDraft = { account_id: string; description: string; debit: string; credit: string };

function JournalComposer({ accounts, onCreated }: { accounts: Row[]; onCreated: () => void }) {
  const { organization } = useSession();
  const idempotency = useIdempotencyKey();
  const canEdit = !!organization && EDIT_ROLES.has(String(organization.user_role));
  const [entryNumber, setEntryNumber] = useState("");
  const [entryDate, setEntryDate] = useState(dateOnly());
  const [description, setDescription] = useState("");
  const [reference, setReference] = useState("");
  const [periodId, setPeriodId] = useState("");
  const [lines, setLines] = useState<LineDraft[]>([{ account_id: "", description: "", debit: "", credit: "" }, { account_id: "", description: "", debit: "", credit: "" }]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<Row | null>(null);

  const periods = useLoad(() => api<Page<Row>>("/ledger/periods/?page=1"), [organization?.id]);
  const openPeriods = (periods.data?.results ?? []).filter((period) => String(period.status) === "OPEN");

  function updateLine(index: number, patch: Partial<LineDraft>) {
    setLines((current) => current.map((line, i) => (i === index ? { ...line, ...patch } : line)));
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null); setResult(null);
    if (!entryNumber.trim() || !description.trim() || !periodId) {
      setError(new Error("Provide an entry number, a description, and an open accounting period."));
      return;
    }
    const payloadLines = lines.filter((line) => line.account_id && (line.debit || line.credit));
    if (payloadLines.length < 2) {
      setError(new Error("A journal entry needs at least two lines with an account and an amount."));
      return;
    }
    if (!window.confirm(`Create draft journal ${entryNumber} dated ${entryDate}? Amounts and balance are validated by the accounting API; nothing is posted until you post it.`)) return;
    // Amounts are submitted as strings to preserve decimal precision. The backend, not this
    // form, decides whether the entry is balanced or valid.
    const body = {
      entry_number: entryNumber.trim(),
      date: entryDate,
      description: description.trim(),
      reference: reference.trim(),
      period_id: periodId,
      lines: payloadLines.map((line, index) => ({ account_id: line.account_id, description: line.description, debit: line.debit || "0", credit: line.credit || "0", sequence: index + 1 })),
    };
    setBusy(true);
    // A stable key per intended creation lets an uncertain retry replay the original
    // result instead of creating a duplicate draft. The key changes only for a new
    // payload and is released after a confirmed success.
    const payload = JSON.stringify(body);
    try {
      const created = await api<Row>("/ledger/journals/", { method: "POST", headers: { "Idempotency-Key": idempotency.keyFor(payload) }, body: payload });
      setResult(created);
      idempotency.reset();
      onCreated();
    } catch (reason) { setError(reason); } finally { setBusy(false); }
  }

  return <Card title="New draft journal entry" description="Create an unposted double-entry draft. Posting is a separate, confirmed action validated by the backend.">
    {!canEdit ? <Empty>Your organization role does not permit creating journal entries.</Empty>
      : <form onSubmit={(event) => void submit(event)}>
        <div className="form-grid">
          <Field label="Entry number"><input required value={entryNumber} onChange={(e) => setEntryNumber(e.target.value)} placeholder="e.g. JE-2026-0001" /></Field>
          <Field label="Date"><input type="date" required value={entryDate} onChange={(e) => setEntryDate(e.target.value)} /></Field>
          <Field label="Accounting period"><select required value={periodId} onChange={(e) => setPeriodId(e.target.value)}><option value="">{periods.loading ? "Loading periods…" : "Choose an open period"}</option>{openPeriods.map((period) => <option key={String(period.id)} value={String(period.id)}>{String(period.name)}</option>)}</select></Field>
          <Field label="Reference (optional)"><input value={reference} onChange={(e) => setReference(e.target.value)} /></Field>
        </div>
        <Field label="Description"><input required value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
        <div className="spacer" />
        {lines.map((line, index) => <div className="form-grid" key={index}>
          <Field label={`Line ${index + 1} account`}><select value={line.account_id} onChange={(e) => updateLine(index, { account_id: e.target.value })}><option value="">Choose account</option>{accounts.filter((account) => account.is_active).map((account) => <option key={String(account.id)} value={String(account.id)}>{String(account.code)} · {String(account.name)}</option>)}</select></Field>
          <Field label="Memo"><input value={line.description} onChange={(e) => updateLine(index, { description: e.target.value })} /></Field>
          <Field label="Debit"><input inputMode="decimal" value={line.debit} onChange={(e) => updateLine(index, { debit: e.target.value, credit: e.target.value ? "" : line.credit })} placeholder="0.00" /></Field>
          <Field label="Credit"><input inputMode="decimal" value={line.credit} onChange={(e) => updateLine(index, { credit: e.target.value, debit: e.target.value ? "" : line.debit })} placeholder="0.00" /></Field>
        </div>)}
        <div className="button-row">
          <Button variant="secondary" onClick={() => setLines((current) => [...current, { account_id: "", description: "", debit: "", credit: "" }])}>Add line</Button>
          <Button variant="secondary" disabled={lines.length <= 2} onClick={() => setLines((current) => current.slice(0, -1))}>Remove line</Button>
          <Button type="submit" disabled={busy}>{busy ? "Creating draft…" : "Create draft entry"}</Button>
        </div>
        <ErrorBox error={error} />
      </form>}
    {result && <div className="notice" role="status">Draft <b>{String(result.entry_number)}</b> created by the API with status <StatusPill value={String(result.status)} />. Debit total {money(String(result.total_debit), String(result.currency || organization?.currency || "KES"))}, credit total {money(String(result.total_credit), String(result.currency || organization?.currency || "KES"))}. Post it from the register below to affect the ledger.</div>}
  </Card>;
}

function JournalDetail({ id }: { id: string }) {
  const { organization } = useSession();
  const { data, loading, error, retry } = useLoad(() => api<Row>(`/ledger/journals/${id}/`), [id]);
  if (loading) return <Loading label="Loading entry lines…" />;
  if (error) return <ErrorBox error={error} retry={retry} />;
  if (!data) return null;
  const currency = String(data.currency || organization?.currency || "KES");
  const lines = (data.lines as Row[]) || [];
  return <div className="report-grid">
    <div className="report-cell"><span>Status</span><b><StatusPill value={String(data.status)} /></b></div>
    <div className="report-cell"><span>Entry number</span><b>{String(data.entry_number)}</b></div>
    <div className="report-cell"><span>Date</span><b>{String(data.date)}</b></div>
    <div className="report-cell"><span>Period</span><b>{String(data.period_name ?? "—")}</b></div>
    <div className="report-cell"><span>Total debit</span><b>{money(String(data.total_debit), currency)}</b></div>
    <div className="report-cell"><span>Total credit</span><b>{money(String(data.total_credit), currency)}</b></div>
    <div className="report-cell" style={{ gridColumn: "1 / -1" }}><span>Lines</span>{lines.length ? <div className="table-wrap"><table><thead><tr><th>Account</th><th>Memo</th><th>Debit</th><th>Credit</th></tr></thead><tbody>{lines.map((line, index) => <tr key={String(line.id ?? index)}><td>{String(line.account_code)} · {String(line.account_name)}</td><td>{String(line.description || "—")}</td><td>{money(String(line.debit), currency)}</td><td>{money(String(line.credit), currency)}</td></tr>)}</tbody></table></div> : <b>No lines returned.</b>}</div>
  </div>;
}

export function Journals() {
  const { organization } = useSession();
  const canEdit = !!organization && EDIT_ROLES.has(String(organization.user_role));
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [expanded, setExpanded] = useState("");
  const [message, setMessage] = useState("");
  const [busyId, setBusyId] = useState("");
  const [showComposer, setShowComposer] = useState(false);

  const query = statusFilter ? `/ledger/journals/?page=${page}&status=${statusFilter}` : `/ledger/journals/?page=${page}`;
  const list = useLoad(() => api<Page<Row>>(query), [organization?.id, page, statusFilter, refresh]);
  const accounts = useLoad(() => api<Page<Row>>("/ledger/accounts/?page=1"), [organization?.id]);
  const rows = list.data?.results || [];

  async function post(row: Row) {
    const number = String(row.entry_number || "this entry");
    if (!window.confirm(`Post journal ${number}? Posting is permanent for the ledger: it writes the double-entry to the general ledger and the entry can then only be corrected by a reversal.`)) return;
    setBusyId(String(row.id)); setMessage("");
    try { const posted = await api<Row>(`/ledger/journals/${row.id}/post/`, { method: "POST" }); setMessage(`Journal ${String(posted.entry_number || number)} posted. The API returned status ${String(posted.status)} with debit ${money(String(posted.total_debit), String(posted.currency || organization?.currency || "KES"))} and credit ${money(String(posted.total_credit), String(posted.currency || organization?.currency || "KES"))}.`); setRefresh((value) => value + 1); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "Posting failed."); } finally { setBusyId(""); }
  }

  async function reverse(row: Row) {
    const number = String(row.entry_number || "this entry");
    const reason = window.prompt(`Reversal reason for journal ${number}? A reversing entry will be created and posted by the backend.`);
    if (reason === null) return;
    if (!window.confirm(`Reverse journal ${number}? This posts an offsetting reversal entry and marks the original as REVERSED.`)) return;
    setBusyId(String(row.id)); setMessage("");
    try { const reversal = await api<Row>(`/ledger/journals/${row.id}/reverse/`, { method: "POST", body: JSON.stringify({ reason: reason.trim() || undefined }) }); setMessage(`Reversal recorded by the API as ${String(reversal.entry_number)} with status ${String(reversal.status)}.`); setRefresh((value) => value + 1); }
    catch (reasonError) { setMessage(reasonError instanceof Error ? reasonError.message : "Reversal failed."); } finally { setBusyId(""); }
  }

  if (!organization) return <Card><Empty>Select an organization to browse its general ledger.</Empty></Card>;

  return <>
    <div className="notice-wrap">{message && <div className="notice" role="status">{message}</div>}</div>
    {canEdit && <div className="button-row" style={{ marginBottom: 14 }}><Button variant={showComposer ? "secondary" : "primary"} onClick={() => setShowComposer((value) => !value)}>{showComposer ? "Hide new entry form" : "New journal entry"}</Button></div>}
    {showComposer && canEdit && <JournalComposer accounts={accounts.data?.results ?? []} onCreated={() => setRefresh((value) => value + 1)} />}
    <Card title="Journal register" description="Entries are posted and reversed only by the accounting service. Posted and reversed entries are immutable in this interface.">
      <div className="form-row" style={{ marginBottom: 12 }}>
        <Field label="Status filter"><select value={statusFilter} onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}><option value="">All</option><option value="DRAFT">Draft</option><option value="POSTED">Posted</option><option value="REVERSED">Reversed</option></select></Field>
      </div>
      {list.loading ? <Loading /> : <><ErrorBox error={list.error} retry={list.retry} />{rows.length ? <div className="table-wrap"><table><thead><tr><th>Entry</th><th>Date</th><th>Description</th><th>Status</th><th>Debit</th><th>Credit</th>{canEdit && <th>Actions</th>}</tr></thead><tbody>
        {rows.map((row) => <tr key={String(row.id)}>
          <td>{String(row.entry_number)}</td><td>{String(row.date)}</td><td>{String(row.description || "—")}</td><td><StatusPill value={String(row.status)} /></td>
          <td>{money(String(row.total_debit), String(row.currency || organization.currency))}</td><td>{money(String(row.total_credit), String(row.currency || organization.currency))}</td>
          {canEdit && <td><div className="button-row" style={{ margin: 0 }}>{String(row.status) === "DRAFT" && <Button disabled={busyId === String(row.id)} onClick={() => void post(row)}>{busyId === String(row.id) ? "Posting…" : "Post"}</Button>}{String(row.status) === "POSTED" && <Button variant="danger" disabled={busyId === String(row.id)} onClick={() => void reverse(row)}>{busyId === String(row.id) ? "Reversing…" : "Reverse"}</Button>}</div></td>}
        </tr>)}
      </tbody></table></div> : <Empty>No journal entries were returned for this filter.</Empty>}
      {list.data && <div className="pagination"><span>{list.data.count ? `Page ${page} · ${list.data.count} entries` : "No entries"}</span><div><Button variant="secondary" disabled={!list.data.previous} onClick={() => setPage((value) => Math.max(1, value - 1))}>Previous</Button><Button variant="secondary" disabled={!list.data.next} onClick={() => setPage((value) => value + 1)}>Next</Button></div></div>}</>}
    </Card>
    {canEdit && <Card title="Entry detail" description="Expand a register row to inspect the exact lines returned by the API.">
      <div className="form-row"><Field label="Inspect entry"><select value={expanded} onChange={(e) => setExpanded(e.target.value)}><option value="">Choose an entry</option>{rows.map((row) => <option key={String(row.id)} value={String(row.id)}>{String(row.entry_number)} · {String(row.status)}</option>)}</select></Field></div>
      {expanded ? <JournalDetail id={expanded} /> : <Empty>Select a journal entry to view its lines.</Empty>}
    </Card>}
  </>;
}
