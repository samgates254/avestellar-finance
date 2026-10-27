"use client";
import { useState } from "react";
import { api, Page } from "@/lib/api";
import { money, dateOnly } from "@/lib/format";
import { useSession } from "@/components/session";
import { Button, Card, Empty, ErrorBox, Field, Loading, StatusPill, useLoad } from "@/components/ui";

type Row = Record<string, unknown>;
const ACCOUNTANT_ROLES = new Set(["OWNER", "ADMIN", "ACCOUNTANT"]);
const ADMIN_ROLES = new Set(["OWNER", "ADMIN"]);

function SessionComposer({ accounts, onCreated }: { accounts: Row[]; onCreated: () => void }) {
  const { organization } = useSession();
  const [accountId, setAccountId] = useState("");
  const [start, setStart] = useState(`${new Date().getFullYear()}-01-01`);
  const [end, setEnd] = useState(dateOnly());
  const [opening, setOpening] = useState("0.00");
  const [closing, setClosing] = useState("0.00");
  const [sessionNumber, setSessionNumber] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    if (!accountId) { setError(new Error("Choose the bank, cash or clearing account to reconcile.")); return; }
    if (!closing.trim()) { setError(new Error("Enter the statement closing balance exactly as shown on the source statement.")); return; }
    if (!window.confirm("Create this reconciliation session? The ledger balance and variance are calculated by the accounting service, not this form.")) return;
    const body = { financial_account_id: accountId, period_start: start, period_end: end, statement_opening_balance: opening || "0", statement_closing_balance: closing, session_number: sessionNumber.trim() || undefined, notes: notes.trim() };
    setBusy(true);
    try { await api("/payments/reconciliation/sessions/", { method: "POST", body: JSON.stringify(body) }); setSessionNumber(""); setNotes(""); onCreated(); }
    catch (reason) { setError(reason); } finally { setBusy(false); }
  }

  return <Card title="Open a reconciliation session" description="A session compares a statement period against the general-ledger balance. The server computes the ledger balance and the variance.">
    <form onSubmit={(event) => void submit(event)}>
    <div className="form-grid">
      <Field label="Account"><select required value={accountId} onChange={(e) => setAccountId(e.target.value)}><option value="">Choose account</option>{accounts.filter((account) => account.is_active && ["CASH", "BANK", "CLEARING"].includes(String(account.system_role))).map((account) => <option key={String(account.id)} value={String(account.id)}>{String(account.code)} · {String(account.name)}</option>)}</select></Field>
      <Field label="Session number (optional)"><input value={sessionNumber} onChange={(e) => setSessionNumber(e.target.value)} placeholder="Auto-generated if blank" /></Field>
      <Field label="Period start"><input type="date" required value={start} onChange={(e) => setStart(e.target.value)} /></Field>
      <Field label="Period end"><input type="date" required value={end} onChange={(e) => setEnd(e.target.value)} /></Field>
      <Field label="Statement opening balance"><input inputMode="decimal" value={opening} onChange={(e) => setOpening(e.target.value)} /></Field>
      <Field label="Statement closing balance"><input inputMode="decimal" required value={closing} onChange={(e) => setClosing(e.target.value)} /></Field>
    </div>
    <Field label="Notes"><input value={notes} onChange={(e) => setNotes(e.target.value)} /></Field>
    <ErrorBox error={error} />
    <Button type="submit" disabled={busy}>{busy ? "Creating session…" : "Create session"}</Button>
    <p className="subtle-note">Currency: {organization?.currency || "KES"}. Balances are transmitted as entered to preserve decimal precision.</p>
    </form>
  </Card>;
}

export function ReconciliationSessions() {
  const { organization } = useSession();
  const role = String(organization?.user_role || "");
  const canManage = ACCOUNTANT_ROLES.has(role);
  const canReopen = ADMIN_ROLES.has(role);
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState("");
  const [message, setMessage] = useState("");
  const [busyId, setBusyId] = useState("");
  const [force, setForce] = useState(false);
  const [showComposer, setShowComposer] = useState(false);
  const accounts = useLoad(() => api<Page<Row>>("/ledger/accounts/?page=1"), [organization?.id]);
  const sessions = useLoad(() => api<Row[]>("/payments/reconciliation/sessions/"), [organization?.id, refresh]);
  const detail = useLoad(() => selected ? api<Row>(`/payments/reconciliation/sessions/${selected}/`) : Promise.resolve(null as unknown as Row), [organization?.id, selected]);

  const rows = sessions.data ?? [];
  const currency = String(organization?.currency || "KES");
  const current = (detail.data || {}) as Row;
  const difference = current.difference != null ? Number(current.difference) : 0;

  async function finalize(session: Row) {
    const number = String(session.session_number || "this session");
    const unbalanced = Number(session.difference) !== 0;
    if (unbalanced && !force) { setMessage("This session still shows an unexplained difference. Tick “force finalize” only if the variance is understood and approved — the backend will otherwise reject finalization."); return; }
    if (!window.confirm(`Finalize reconciliation session ${number}${unbalanced ? " with a non-zero difference (force)" : ""}? Finalizing locks the session; only an administrator or owner can reopen it afterwards.`)) return;
    setBusyId(String(session.id)); setMessage("");
    try { const done = await api<Row>(`/payments/reconciliation/sessions/${session.id}/finalize/`, { method: "POST", body: JSON.stringify({ force: unbalanced ? true : false }) }); setMessage(`Session ${String(done.session_number || number)} finalized. Server difference is ${money(String(done.difference ?? "0"), currency)} and status is ${String(done.status)}.`); void sessions.retry(); if (selected === String(session.id)) void detail.retry(); }
    catch (reason) { setMessage(reason instanceof Error ? reason.message : "Finalization failed."); } finally { setBusyId(""); }
  }

  async function reopen(session: Row) {
    const number = String(session.session_number || "this session");
    const reason = window.prompt(`Reason for reopening reconciliation session ${number}?`);
    if (reason === null) return;
    if (!reason.trim()) { setMessage("A reopening reason is required by the backend."); return; }
    if (!window.confirm(`Reopen reconciliation session ${number}?`)) return;
    setBusyId(String(session.id)); setMessage("");
    try { const done = await api<Row>(`/payments/reconciliation/sessions/${session.id}/reopen/`, { method: "POST", body: JSON.stringify({ reason: reason.trim() }) }); setMessage(`Session ${String(done.session_number || number)} reopened. Status is now ${String(done.status)}.`); void sessions.retry(); if (selected === String(session.id)) void detail.retry(); }
    catch (reasonError) { setMessage(reasonError instanceof Error ? reasonError.message : "Reopening failed."); } finally { setBusyId(""); }
  }

  if (!organization) return <Card><Empty>Select an organization to manage its reconciliation sessions.</Empty></Card>;

  return <>
    {message && <div className="notice" role="status">{message}</div>}
    {canManage && <div className="button-row" style={{ marginBottom: 14 }}><Button variant={showComposer ? "secondary" : "primary"} onClick={() => setShowComposer((value) => !value)}>{showComposer ? "Hide session form" : "New reconciliation session"}</Button></div>}
    {showComposer && canManage && <SessionComposer accounts={accounts.data?.results ?? []} onCreated={() => { setShowComposer(false); setRefresh((value) => value + 1); }} />}

    <div className="dashboard-grid">
      <Card title="Reconciliation sessions" description="Statement versus general-ledger sessions for the active organization.">
        {sessions.loading ? <Loading /> : <><ErrorBox error={sessions.error} retry={sessions.retry} />{rows.length ? <div className="queue">{rows.map((session) => <button className={`queue-row ${selected === session.id ? "active" : ""}`} key={String(session.id)} onClick={() => setSelected(String(session.id))}><span><b>{String(session.session_number)}</b><small>{String(session.financial_account_code)} · {String(session.period_start)} → {String(session.period_end)}</small></span><span className="queue-amount">{money(String(session.statement_closing_balance), currency)}</span><StatusPill value={String(session.status)} /></button>)}</div> : <Empty>No reconciliation sessions have been created yet.</Empty>}</>}
      </Card>
      <Card title="Session variance" description="Ledger balance, reconciled balance and difference are all computed and returned by the accounting service.">
        {!selected ? <Empty>Select a session to review its balances.</Empty> : detail.loading ? <Loading label="Loading session…" /> : <><ErrorBox error={detail.error} retry={detail.retry} />{detail.data && <>
          <div className="context-pairs"><span>Status</span><b><StatusPill value={String(current.status)} /></b><span>Account</span><b>{String(current.financial_account_code)} · {String(current.financial_account_name)}</b><span>Statement opening</span><b>{money(String(current.statement_opening_balance), currency)}</b><span>Statement closing</span><b>{money(String(current.statement_closing_balance), currency)}</b><span>Ledger balance</span><b>{money(String(current.ledger_balance), currency)}</b><span>Reconciled balance</span><b>{money(String(current.reconciled_balance), currency)}</b><span>Difference</span><b>{money(String(current.difference ?? "0"), currency)}</b><span>Finalized by</span><b>{String(current.finalized_by_email || "—")}</b></div>
          {current.notes ? <p className="plain-copy">Notes: {String(current.notes)}</p> : null}
          {difference !== 0 && <p className="subtle-note">A non-zero difference blocks normal finalization; the backend requires an explained or forced variance.</p>}
          {canManage && String(current.status) !== "FINALIZED" && <div className="button-row">
            <label className="field" style={{ maxWidth: 240 }}><span>Force finalize (variance understood)</span><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /></label>
            <Button disabled={busyId === String(current.id)} onClick={() => void finalize(current)}>{busyId === String(current.id) ? "Finalizing…" : "Finalize session"}</Button>
          </div>}
          {canReopen && String(current.status) === "FINALIZED" && <div className="button-row"><Button variant="danger" disabled={busyId === String(current.id)} onClick={() => void reopen(current)}>{busyId === String(current.id) ? "Reopening…" : "Reopen session"}</Button></div>}
          {!canReopen && String(current.status) === "FINALIZED" && <p className="subtle-note">Only an organization admin or owner can reopen a finalized session.</p>}
        </>}</>}
      </Card>
    </div>
  </>;
}
