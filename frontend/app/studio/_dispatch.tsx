"use client";

import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api-client";
import { getApi } from "@/lib/backend";
import { dispatchFacts, reconcileError, reconciliationLine } from "@/lib/studio/dispatch";
import type { DispatchView, ReconciliationList, ToolInvocationItem } from "@/lib/studio/types";
import { Badge, Notice } from "./_ui";

/** One side-effecting call: whether it was SENT, whether the outcome is KNOWN, whether an automatic retry is allowed, and the remote reference when there is one. */
export function DispatchCard({ item, canReconcile, onResolved }: { item: ToolInvocationItem; canReconcile: boolean; onResolved: () => void }) {
  const d = item.dispatch as DispatchView;
  const facts = dispatchFacts(d);
  const [open, setOpen] = useState(false);
  return (
    <li className="st-dispatch" data-state={d.state}>
      <div className="flex flex-wrap items-center gap-2">
        <strong className="st-mono">{item.tool.id}{item.tool.version ? `@${item.tool.version}` : ""}</strong>
        {item.task ? <span className="st-muted text-xs">task {item.task}</span> : null}
        <Badge tone={facts.tone} pulse={d.state === "dispatching"}>
          {facts.label}
        </Badge>
        {d.generation > 1 ? <span className="st-muted text-xs">attempt {d.generation} of this operation</span> : null}
      </div>
      <p className="st-muted m-0 mt-1 text-sm">{facts.detail}</p>
      <dl className="st-dispatch-facts">
        <div>
          <dt>Automatic retry</dt>
          <dd>{facts.automaticRetry}</dd>
        </div>
        <div>
          <dt>Reconciliation required</dt>
          <dd>{d.requires_reconciliation ? "Yes" : "No"}</dd>
        </div>
        {d.external_ref ? (
          <div>
            <dt>Remote reference</dt>
            <dd className="st-mono">{d.external_ref}</dd>
          </div>
        ) : null}
      </dl>
      {d.reconciliation ? <p className="m-0 mt-2 text-sm" data-testid="reconciliation-line">{reconciliationLine(d.reconciliation)}</p> : null}
      {d.requires_reconciliation ? (
        canReconcile ? (
          <div className="mt-3">
            <button className="st-btn st-btn-sm" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
              {open ? "Close" : "Reconcile this outcome"}
            </button>
            {open ? <ReconcileForm invocationId={item.id} onDone={() => { setOpen(false); onResolved(); }} /> : null}
          </div>
        ) : (
          <p className="st-muted m-0 mt-2 text-xs">Only a workspace owner or admin can reconcile an unknown outcome.</p>
        )
      ) : null}
    </li>
  );
}

function ReconcileForm({ invocationId, onDone }: { invocationId: string; onDone: () => void }) {
  const [mode, setMode] = useState<"query" | "attest">("query");
  const [decision, setDecision] = useState<"confirmed_succeeded" | "confirmed_failed">("confirmed_failed");
  const [evidence, setEvidence] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setProblem(null);
    try {
      await getApi().reconcileInvocation(invocationId, mode === "query" ? { mode, reason: reason.trim() } : { mode, decision, evidence: evidence.trim(), reason: reason.trim() });
      onDone();
    } catch (err) {
      setProblem(reconcileError(err instanceof ApiError ? err.detail?.code : undefined));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="st-reconcile" onSubmit={submit}>
      <fieldset className="st-field">
        <legend className="st-label">How do you want to resolve it?</legend>
        <label className="flex items-start gap-2 text-sm">
          <input type="radio" name={`mode-${invocationId}`} checked={mode === "query"} onChange={() => setMode("query")} />
          <span>
            <strong>Ask the remote service</strong>
            <span className="st-muted block">Looks the operation up by its idempotency key. Nothing is sent. If the service cannot answer, the outcome stays unknown and the attempt is recorded.</span>
          </span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="radio" name={`mode-${invocationId}`} checked={mode === "attest"} onChange={() => setMode("attest")} />
          <span>
            <strong>Record my own decision</strong>
            <span className="st-muted block">Use this when you checked somewhere else. It is stored as your statement, not as proof from the remote service.</span>
          </span>
        </label>
      </fieldset>
      {mode === "attest" ? (
        <>
          <div className="st-field">
            <label className="st-label" htmlFor={`decision-${invocationId}`}>
              What happened?
            </label>
            <select id={`decision-${invocationId}`} className="st-input" value={decision} onChange={(e) => setDecision(e.target.value as typeof decision)}>
              <option value="confirmed_failed">It was NOT performed</option>
              <option value="confirmed_succeeded">It WAS performed</option>
            </select>
          </div>
          <div className="st-field">
            <label className="st-label" htmlFor={`evidence-${invocationId}`}>
              What did you check?
            </label>
            <textarea id={`evidence-${invocationId}`} className="st-input" rows={2} required minLength={3} maxLength={500} value={evidence} onChange={(e) => setEvidence(e.target.value)} />
          </div>
        </>
      ) : null}
      <div className="st-field">
        <label className="st-label" htmlFor={`reason-${invocationId}`}>
          Why are you resolving it this way?
        </label>
        <textarea id={`reason-${invocationId}`} className="st-input" rows={2} required minLength={3} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>
      <div aria-live="polite">{problem ? <Notice tone="bad" title="Couldn't record that">{problem}</Notice> : null}</div>
      <button className="st-btn st-btn-primary" disabled={busy || reason.trim().length < 3 || (mode === "attest" && evidence.trim().length < 3)}>
        {busy ? "Recording…" : mode === "query" ? "Ask the remote service" : "Record my decision"}
      </button>
    </form>
  );
}

/** The side-effecting calls of a run, with the honest state of each. Real records only. */
export function SideEffects({ calls, reconciliations, onResolved }: { calls: ToolInvocationItem[]; reconciliations: ReconciliationList | null; onResolved: () => void }) {
  const rows = calls.filter((c) => c.dispatch);
  if (!rows.length) return null;
  return (
    <ul className="st-dispatch-list" aria-label="Side-effecting actions of this run">
      {rows.map((c) => (
        <DispatchCard key={c.id} item={c} canReconcile={reconciliations?.can_reconcile ?? false} onResolved={onResolved} />
      ))}
    </ul>
  );
}
