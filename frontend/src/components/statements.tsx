"use client";
import { useRef, useState } from "react";
import { api, Page } from "@/lib/api";
import { money } from "@/lib/format";
import { useSession } from "@/components/session";
import { Button, Card, Empty, ErrorBox, Field, Loading, StatusPill, useLoad } from "@/components/ui";

type Row = Record<string, unknown>;
const EDIT_ROLES = new Set(["OWNER", "ADMIN", "ACCOUNTANT"]);
const MAX_BYTES = 15 * 1024 * 1024;

// Client-side convenience checks only. Parsing, deduplication and any posting decision
// are entirely the server's responsibility; an accepted upload is evidence, not confirmation.
const SOURCE_EXTENSIONS: Record<string, string[]> = {
  BANK_CSV: [".csv"],
  MPESA_CSV: [".csv"],
  OFX: [".ofx"],
  MT940: [".txt", ".mt940", ".sta"],
};

function validateFile(file: File | null, source: string): string | null {
  if (!file) return "Choose a statement file to upload.";
  if (file.size === 0) return "That file is empty.";
  if (file.size > MAX_BYTES) return `File is larger than the ${MAX_BYTES / 1024 / 1024} MB limit.`;
  const name = file.name.toLowerCase();
  const allowed = SOURCE_EXTENSIONS[source] || [];
  if (!allowed.some((suffix) => name.endsWith(suffix))) return `A ${source.replace("_", " ").toLowerCase()} file is expected (${allowed.join(", ")}).`;
  return null;
}

function BatchTransactions({ batch }: { batch: Row }) {
  const transactions = (batch.transactions as Row[]) || [];
  if (!transactions.length) return <Empty>This batch returned no ingested statement lines.</Empty>;
  return <div className="table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Reference</th><th>Inflow</th><th>Outflow</th><th>Status</th></tr></thead><tbody>
    {transactions.map((trx, index) => <tr key={String(trx.id ?? index)}>
      <td>{String(trx.transaction_date)}</td><td>{String(trx.description || "—")}</td><td>{String(trx.reference || trx.source_transaction_id || "—")}</td>
      <td>{money(String(trx.credit_amount || "0"), String(trx.currency || "KES"))}</td><td>{money(String(trx.debit_amount || "0"), String(trx.currency || "KES"))}</td>
      <td><StatusPill value={String(trx.reconciliation_status)} /></td>
    </tr>)}
  </tbody></table></div>;
}

export function StatementImports() {
  const { organization } = useSession();
  const canEdit = !!organization && EDIT_ROLES.has(String(organization.user_role));
  const accounts = useLoad(() => api<Page<Row>>("/ledger/accounts/?page=1"), [organization?.id]);
  const batches = useLoad(() => api<Row[]>("/payments/statement-imports/"), [organization?.id]);
  const [financialAccountId, setFinancialAccountId] = useState("");
  const [source, setSource] = useState("BANK_CSV");
  const [dateFormat, setDateFormat] = useState("");
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<Row | null>(null);
  const [expanded, setExpanded] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const depositAccounts = (accounts.data?.results ?? []).filter((account) => account.is_active && ["CASH", "BANK", "CLEARING"].includes(String(account.system_role)));
  const detail = useLoad(() => expanded ? api<Row>(`/payments/statement-imports/${expanded}/`) : Promise.resolve(null as unknown as Row), [organization?.id, expanded]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    setError(null); setResult(null);
    const file = fileRef.current?.files?.[0] ?? null;
    const problem = validateFile(file, source);
    if (problem) { setError(new Error(problem)); return; }
    if (!financialAccountId) { setError(new Error("Choose the bank, cash or clearing account this statement belongs to.")); return; }
    if (!window.confirm(`Upload “${file?.name}” to the imported-statement queue? Importing only records statement evidence — it does not confirm any payment or post anything to the ledger.`)) return;
    const form = new FormData();
    form.set("financial_account_id", financialAccountId);
    form.set("import_source", source);
    if (dateFormat.trim()) form.set("date_format", dateFormat.trim());
    form.set("file", file as File);
    setBusy(true);
    try {
      const batch = await api<Row>("/payments/statement-imports/", { method: "POST", body: form });
      setResult(batch);
      setFileName(""); if (fileRef.current) fileRef.current.value = "";
      void batches.retry();
    } catch (reason) { setError(reason); } finally { setBusy(false); }
  }

  if (!organization) return <Card><Empty>Select an organization to import its bank and M-Pesa statements.</Empty></Card>;

  const summary = (result?.validation_summary as Row) || undefined;
  const errors = (result?.error_details as Row[]) || [];

  return <>
    {canEdit && <Card title="Upload bank or M-Pesa statement" description="Import CSV, M-Pesa, OFX or MT940 statement files. Files are deduplicated by SHA-256 and ingested as reconciliation evidence — never as automatic payment confirmation.">
      <form onSubmit={(event) => void upload(event)}>
        <div className="form-grid">
          <Field label="Financial account"><select required value={financialAccountId} onChange={(e) => setFinancialAccountId(e.target.value)}><option value="">{accounts.loading ? "Loading accounts…" : "Choose deposit account"}</option>{depositAccounts.map((account) => <option key={String(account.id)} value={String(account.id)}>{String(account.code)} · {String(account.name)}</option>)}</select></Field>
          <Field label="Statement format"><select value={source} onChange={(e) => setSource(e.target.value)}><option value="BANK_CSV">Bank CSV</option><option value="MPESA_CSV">M-Pesa CSV</option><option value="OFX">OFX</option><option value="MT940">MT940</option></select></Field>
          {source === "BANK_CSV" && <Field label="Date format (optional)"><input value={dateFormat} onChange={(e) => setDateFormat(e.target.value)} placeholder="e.g. %d/%m/%Y" aria-describedby="date-format-hint" /><small id="date-format-hint">Leave blank to let the parser auto-detect the statement date format.</small></Field>}
          <Field label="Statement file"><input ref={fileRef} type="file" accept={SOURCE_EXTENSIONS[source]?.join(",")} onChange={(e) => setFileName(e.target.files?.[0]?.name ?? "")} /></Field>
        </div>
        <p className="subtle-note">Selected file: {fileName || "none"}.</p>
        <ErrorBox error={error} />
        <Button type="submit" disabled={busy}>{busy ? "Uploading to statement queue…" : "Upload statement"}</Button>
      </form>
      {result && <div className="notice" role="status">
        Batch for <b>{String(result.file_name)}</b> returned status <StatusPill value={String(result.status)} />.
        Rows parsed {String(result.total_rows ?? summary?.total_rows_parsed ?? "—")} · imported {String(result.imported_count ?? "0")} · duplicate {String(result.duplicate_count ?? "0")} · rejected {String(result.rejected_count ?? "0")}.
        {errors.length > 0 && <div>Parser reported {errors.length} issue(s): {errors.slice(0, 5).map((issue, index) => <div key={index}>· {String(issue.message ?? JSON.stringify(issue))}</div>)}</div>}
        Review imported lines in the reconciliation queue.
      </div>}
    </Card>}

    <Card title="Import history" description="Previously imported statement batches for this organization.">
      {batches.loading ? <Loading /> : <><ErrorBox error={batches.error} retry={batches.retry} />{(batches.data ?? []).length ? <div className="table-wrap"><table><thead><tr><th>File</th><th>Account</th><th>Source</th><th>Imported</th><th>Duplicates</th><th>Rejected</th><th>Status</th><th></th></tr></thead><tbody>
        {(batches.data || []).map((batch) => <tr key={String(batch.id)}>
          <td>{String(batch.file_name)}</td><td>{String(batch.financial_account_code)} · {String(batch.financial_account_name)}</td><td>{String(batch.import_source)}</td>
          <td>{String(batch.imported_count)}</td><td>{String(batch.duplicate_count)}</td><td>{String(batch.rejected_count)}</td><td><StatusPill value={String(batch.status)} /></td>
          <td><Button variant="secondary" onClick={() => setExpanded(expanded === String(batch.id) ? "" : String(batch.id))}>{expanded === String(batch.id) ? "Hide lines" : "View lines"}</Button></td>
        </tr>)}
      </tbody></table></div> : <Empty>No statement batches have been imported yet.</Empty>}</>}
      {expanded && <div className="spacer" />}
      {expanded && (detail.loading ? <Loading label="Loading statement lines…" /> : <><ErrorBox error={detail.error} retry={detail.retry} />{detail.data && <BatchTransactions batch={detail.data} />}</>)}
    </Card>
  </>;
}
