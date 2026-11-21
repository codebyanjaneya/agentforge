// Words for the dispatch state machine of side-effecting calls (P0). Pure. The point of every sentence: say what is KNOWN, never turn "we do not know" into "it failed", and never
// promise exactly-once delivery.

import type { DispatchState, DispatchView } from "./types.ts";

type Tone = "good" | "bad" | "warn" | "muted" | "active";

export const DISPATCH_STATE: Record<DispatchState, { label: string; tone: Tone }> = {
  authorized: { label: "Authorized, not sent", tone: "muted" },
  dispatching: { label: "Sending", tone: "active" },
  succeeded: { label: "Confirmed performed", tone: "good" },
  failed: { label: "Confirmed not performed", tone: "bad" },
  unknown: { label: "Outcome unknown", tone: "warn" },
  reconciled_succeeded: { label: "Reconciled: performed", tone: "good" },
  reconciled_failed: { label: "Reconciled: not performed", tone: "muted" },
  cancelled: { label: "Never sent", tone: "muted" },
};

export type DispatchFacts = { label: string; tone: Tone; detail: string; automaticRetry: string; needsReconciliation: boolean; confirmed: boolean };

/** What a person needs to know about one dispatch. ``retryPolicy`` is the tool's own declared policy for a confirmed failure ("never" | "after_confirmed_failure"). */
export function dispatchFacts(d: DispatchView, retryPolicy: string = "never"): DispatchFacts {
  const base = DISPATCH_STATE[d.state];
  const common = { label: base.label, tone: base.tone, needsReconciliation: d.requires_reconciliation, confirmed: d.performed === true && (d.state === "succeeded" || d.state === "reconciled_succeeded") };
  switch (d.state) {
    case "authorized":
      return { ...common, detail: "The approval was used and the intent is saved, but nothing has been sent yet.", automaticRetry: "Not applicable yet" };
    case "dispatching":
      return { ...common, detail: "The request is being sent. If the server stops now, the outcome will be unknown, not failed.", automaticRetry: "No: it is in flight" };
    case "succeeded":
      return { ...common, detail: "The remote service confirmed it. A retry replays this result and never sends it again.", automaticRetry: "Not needed: it is done" };
    case "failed":
      return { ...common, detail: "The tool proved the action was not performed.", automaticRetry: retryPolicy === "after_confirmed_failure" ? "Yes: this tool allows it after a confirmed failure" : "No: this tool needs a person to decide" };
    case "unknown":
      return { ...common, detail: "AgentForge cannot tell whether this action took effect. It will never be sent again automatically. Reconcile it: ask the remote service, or record an informed decision.", automaticRetry: "No: reconcile it first" };
    case "reconciled_succeeded":
      return { ...common, detail: "Resolved by a reconciliation record. A retry replays this result and never sends it again.", automaticRetry: "Not needed: it is done" };
    case "reconciled_failed":
      return { ...common, detail: "Resolved by a reconciliation record as not performed. A retry may send it again, with a new idempotency key.", automaticRetry: "A person-started retry may send it again" };
    default:
      return { ...common, detail: "It was proven that this action was never sent.", automaticRetry: "Yes: it was never sent" };
  }
}

/** The honest line about HOW a reconciliation was decided: a remote answer is evidence, a person's statement is only a statement. */
export function reconciliationLine(r: NonNullable<DispatchView["reconciliation"]>): string {
  const who = r.by ?? "a workspace member";
  const what = r.decision === "unresolved" ? "could not resolve it" : r.decision === "confirmed_succeeded" ? "decided it was performed" : "decided it was not performed";
  const how = r.evidence_kind === "remote_query" ? "Asked the remote service" : "A person's statement, not proof from the remote service";
  return `${who} ${what}. ${how}: ${r.evidence} Reason: ${r.reason}`;
}

const ERRORS: Record<string, string> = {
  already_resolved: "Someone already resolved this outcome. Reload to see the decision.",
  reconciler_role_required: "Only a workspace owner or admin can reconcile an unknown outcome.",
  lookup_unavailable: "This tool cannot be asked about past operations. Record a decision instead.",
  evidence_and_reason_required: "Say what you checked and why you decided.",
  not_found: "That call does not exist, or it belongs to a different workspace.",
  not_unknown: "This call does not have an unknown outcome.",
};
export const reconcileError = (code: string | undefined): string => (code && ERRORS[code]) || "That could not be recorded. Reload and try again.";
