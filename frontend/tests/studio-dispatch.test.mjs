// P0 safety sprint: the wording and the client calls for unknown side-effect outcomes. Pure logic and in-memory fakes (no network).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import { DISPATCH_STATE, dispatchFacts, reconcileError, reconciliationLine } from "../lib/studio/dispatch.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(path.join(FRONTEND, rel), "utf8");

const view = (state, over = {}) => ({
  id: "d1", state, generation: 1, idempotency_key: "af-x-g1", external_ref: null, dispatch_started_at: null, completed_at: null,
  outcome: state === "unknown" ? "unknown" : "confirmed", performed: state === "succeeded" || state === "reconciled_succeeded" ? true : state === "unknown" ? null : false, requires_reconciliation: state === "unknown", reconciliation: null, ...over,
});

test("every state has a label and an honest sentence, and unknown is never worded as a failure", () => {
  assert.equal(Object.keys(DISPATCH_STATE).length, 8);
  const unknown = dispatchFacts(view("unknown"));
  assert.equal(unknown.label, "Outcome unknown");
  assert.ok(unknown.needsReconciliation && !unknown.confirmed);
  assert.match(unknown.detail, /cannot tell whether this action took effect/);
  assert.match(unknown.detail, /never be sent again automatically/);
  assert.match(unknown.automaticRetry, /No: reconcile it first/);
  assert.ok(!/fail/i.test(unknown.label + unknown.detail), "an unknown outcome is not called a failure");
  const ok = dispatchFacts(view("succeeded"));
  assert.ok(ok.confirmed && !ok.needsReconciliation);
  assert.match(ok.detail, /replays this result and never sends it again/);
  assert.equal(dispatchFacts(view("failed")).automaticRetry, "No: this tool needs a person to decide");
  assert.match(dispatchFacts(view("failed"), "after_confirmed_failure").automaticRetry, /allows it/);
  assert.match(dispatchFacts(view("cancelled")).detail, /never sent/);
  assert.match(dispatchFacts(view("reconciled_failed")).detail, /new idempotency key/);
  assert.equal(dispatchFacts(view("reconciled_succeeded")).confirmed, true);
});

test("a reconciliation is described with who decided, how, what they checked and why: a statement is never presented as proof", () => {
  const attest = reconciliationLine({ decision: "confirmed_failed", evidence_kind: "operator_attestation", evidence: "Checked the dashboard.", reason: "Customer says nothing arrived", at: "x", attestation_only: true, by: "olivia@example.com", attempts: 1 });
  assert.match(attest, /olivia@example\.com decided it was not performed\./);
  assert.match(attest, /A person's statement, not proof from the remote service/);
  assert.match(attest, /Reason: Customer says nothing arrived/);
  const remote = reconciliationLine({ decision: "confirmed_succeeded", evidence_kind: "remote_query", evidence: "The remote has this key.", reason: "check", at: "x", attestation_only: false, by: null, attempts: 2 });
  assert.match(remote, /a workspace member decided it was performed\. Asked the remote service/);
  assert.match(reconciliationLine({ decision: "unresolved", evidence_kind: "remote_query", evidence: "No answer.", reason: "try", at: "x", attestation_only: false, by: "a@x.co", attempts: 1 }), /could not resolve it/);
  assert.match(reconcileError("already_resolved"), /already resolved/);
  assert.match(reconcileError("reconciler_role_required"), /owner or admin/);
  assert.equal(reconcileError("SECRET raw text"), reconcileError(undefined));
});

test("the client sends exactly the documented reconciliation requests and never an identity", async () => {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { items: [], can_reconcile: true, dispatch: {} }));
  const client = createApiClient({ baseUrl: "https://api.example.com", tokens, fetchImpl: backend });
  await client.listReconciliations();
  await client.reconcileInvocation("i1", { mode: "query", reason: "check the record" });
  await client.reconcileInvocation("i1", { mode: "attest", decision: "confirmed_failed", evidence: "dashboard", reason: "customer confirmed" });
  const seen = backend.calls.map((c) => `${c.init.method} ${new URL(c.url).pathname} ${c.init.body ?? ""}`.trim());
  assert.deepEqual(seen, [
    "GET /reconciliations",
    'POST /tool-invocations/i1/reconcile {"mode":"query","reason":"check the record"}',
    'POST /tool-invocations/i1/reconcile {"mode":"attest","decision":"confirmed_failed","evidence":"dashboard","reason":"customer confirmed"}',
  ]);
  for (const c of backend.calls) assert.ok(!/user_id|workspace|actor|email/i.test(c.url + (c.init.body ?? "")), "the actor and workspace come from the session");
});

test("the run page shows side effects, blocks the retry visibly, and the reconcile form is labelled and explains what each option does", () => {
  const page = read("app/studio/runs/[id]/page.tsx");
  assert.match(page, /title="Side effects"/);
  assert.match(page, /data-testid="retry-blocked"/);
  assert.match(page, /acknowledging is not enough/);
  const ui = read("app/studio/_dispatch.tsx");
  assert.match(ui, /Ask the remote service/);
  assert.match(ui, /not as proof from the remote service/);
  assert.match(ui, /htmlFor=\{`reason-\$\{invocationId\}`\}/);
  assert.match(ui, /aria-live="polite"/);
  assert.equal((ui.match(/reconcileInvocation\(/g) ?? []).length, 1, "exactly one place sends a reconciliation");
  assert.ok(!/localStorage|sessionStorage/.test(ui) && !/dangerouslySetInnerHTML/.test(ui));
  assert.match(read("app/studio/_tools.tsx"), /dispatchFacts\(item\.dispatch\)/);
});

// Found in a real browser: after a reconciliation the card kept showing "unknown" because the tool-call loader did not depend on the resolved counter.
test("the run page reloads its tool calls after a reconciliation", () => {
  const src = read("app/studio/runs/[id]/page.tsx");
  assert.match(src, /getRunToolInvocations\(id\), \[[^\]]*\bresolved\]/);
});
