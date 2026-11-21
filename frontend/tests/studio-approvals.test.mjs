// M4-C: the human-approval screens. Pure logic, the client's decision calls and source-level invariants. In-memory fakes only (no network, no backend process).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import * as approvals from "../lib/studio/approvals.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const strip = (source) => source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
const read = (rel) => strip(readFileSync(path.join(FRONTEND, rel), "utf8"));
const BASE = "https://api.example.com";

const item = (over = {}) => ({
  id: "a1", status: "pending", execution_id: "e1", run_id: "r1", task: "notify", agent: "notifier", agent_version_id: null, seq: 1,
  tool: { id: "demo.send_note", version: 1, ref: "demo.send_note@1", title: "Send a note (simulated)", summary: "s", risk: "external", side_effect: "non_idempotent", limitations: [] },
  impact: "x", reversible: null, arguments: { recipient: "customer@example.com", body: "y".repeat(120), count: 3 }, args_hash: "a".repeat(64), payload_hash: "b".repeat(64),
  reason: null, created_at: "2026-11-18T10:00:00", expires_at: "2026-11-18T10:15:00", decided_at: null, decided: false, consumed_at: null, invocation_id: null, ...over,
});

test("every state has a label, a seal and a plain explanation, and only a pending request is open", () => {
  for (const status of ["pending", "approved", "rejected", "expired", "invalidated", "consumed"]) {
    const meta = approvals.APPROVAL_STATUS[status];
    assert.ok(meta.label && meta.tone && meta.seal && meta.explain.length > 20, status);
    assert.equal(approvals.isOpen(status), status === "pending");
  }
  assert.match(approvals.APPROVAL_STATUS.consumed.explain, /cannot be used again/);
  assert.match(approvals.APPROVAL_STATUS.pending.explain, /Nothing has happened yet/);
});

test("server refusals are explained with fixed text only (no raw server text), including a changed request", () => {
  assert.match(approvals.approvalError("approval_stale"), /changed while you were looking/);
  assert.match(approvals.approvalError("approval_expired"), /expired/);
  assert.match(approvals.approvalError("approval_consumed"), /already used/);
  assert.match(approvals.approvalError("not_found"), /different workspace/);
  assert.equal(approvals.approvalError("SECRET raw server text"), approvals.approvalError(undefined));
  assert.ok(!/SECRET/.test(approvals.approvalError("SECRET raw server text")));
});

test("the countdown is computed from the server's expiry (UTC) and never goes negative", () => {
  const now = Date.parse("2026-11-18T10:00:00Z");
  assert.equal(approvals.secondsLeft("2026-11-18T10:15:00", now), 900);
  assert.equal(approvals.secondsLeft("2026-11-18T09:59:00", now), 0);
  assert.deepEqual([approvals.countdown(0), approvals.countdown(5), approvals.countdown(125), approvals.countdown(3725)], ["expired", "0:05 left", "2:05 left", "1 h 2 min left"]);
});

test("arguments are shown as rows exactly as validated, and the one-line summary is bounded", () => {
  const rows = approvals.argumentRows(item().arguments);
  assert.deepEqual(rows.map((r) => [r.name, r.long]), [["recipient", false], ["body", true], ["count", false]]);
  assert.equal(rows[2].value, "3");
  assert.deepEqual(approvals.argumentRows(null), []);
  const line = approvals.actionSummary(item());
  assert.ok(line.startsWith("Send a note (simulated) · recipient: customer@example.com"));
  assert.ok(approvals.actionSummary(item({ arguments: { body: "z".repeat(200) } })).length < 90);
});

test("the inbox shows what is waiting first (oldest first), then the decided history (newest first); tasks group their approvals", () => {
  const list = [item({ id: "h1", status: "rejected", created_at: "2026-11-18T08:00:00" }), item({ id: "p2", created_at: "2026-11-18T10:05:00" }), item({ id: "p1", created_at: "2026-11-18T10:00:00" }),
                item({ id: "h2", status: "consumed", created_at: "2026-11-18T09:00:00" })];
  const { waiting, history } = approvals.inboxOrder(list);
  assert.deepEqual([waiting.map((i) => i.id), history.map((i) => i.id)], [["p1", "p2"], ["h2", "h1"]]);
  const grouped = approvals.approvalsByTask([item({ id: "a", task: "x" }), item({ id: "b", task: "x", created_at: "2026-11-18T11:00:00" }), item({ id: "c", task: null })]);
  assert.deepEqual(Object.fromEntries(Object.entries(grouped).map(([k, v]) => [k, v.map((i) => i.id)])), { x: ["b", "a"] });
});

test("the client's decision calls send only the hash that was shown (a staleness check), never an identity, and use no other verb", async () => {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { items: [], pending: 0 }));
  const client = createApiClient({ baseUrl: BASE, tokens, fetchImpl: backend });
  await client.listApprovals();
  await client.listApprovals({ status: "pending", runId: "r1", limit: 5 });
  await client.getApproval("a1");
  await client.decideApproval("a1", "approve", "c".repeat(64));
  await client.decideApproval("a1", "reject");
  const seen = backend.calls.map((c) => {
    const url = new URL(c.url);
    return `${c.init.method} ${url.pathname}${url.search} ${c.init.body ?? ""}`.trim();
  });
  assert.deepEqual(seen, ["GET /approvals", "GET /approvals?status=pending&run_id=r1&limit=5", "GET /approvals/a1", `POST /approvals/a1/approve {"payload_hash":"${"c".repeat(64)}"}`, "POST /approvals/a1/reject {}"]);
  for (const c of backend.calls) assert.ok(!/user_id|workspace|email|owner|decided_by/i.test(c.url + (c.init.body ?? "")), "no identity is ever sent");
});

test("approval is always an explicit click on one exact action: no auto-approve, no blanket trust, no vague labels", () => {
  const ui = read("app/studio/_approvals.tsx");
  const pages = read("app/studio/approvals/[id]/page.tsx") + read("app/studio/approvals/page.tsx");
  const all = ui + pages + read("app/studio/_run.tsx");
  assert.equal((ui.match(/decideApproval\(/g) ?? []).length, 1, "exactly one place can send a decision");
  assert.match(ui, /onClick=\{\(\) => void decide\("approve"\)\}/);
  assert.match(ui, /onClick=\{\(\) => void decide\("reject"\)\}/);
  assert.ok(!/decideApproval/.test(pages) && !/decideApproval/.test(read("app/studio/_run.tsx")), "no page or list can decide on its own");
  assert.ok(!/useEffect[^]{0,400}decide\(/.test(ui), "a decision is never made from an effect");
  assert.match(ui, /Approve this exact action/);
  for (const bad of [/Approve agent/i, /Trust this agent/i, /Always allow/i, /Approve all/i, /remember (this|my) (choice|decision)/i]) assert.ok(!bad.test(all), String(bad));
  assert.ok(!/localStorage|sessionStorage|document\.cookie/.test(all), "nothing about a decision is remembered in the browser");
  assert.ok(!/dangerouslySetInnerHTML|innerHTML/.test(all), "the agent's words and the values are rendered as text only");
});

test("a suspended task is told apart from a request that only waits, an approval that is resuming from one that ran, and a member from an approver", () => {
  const phase = (over) => approvals.phaseOf({ status: "pending", suspended: false, task: "notify", ...over });
  assert.equal(phase({ suspended: true }).label, "Suspended · waiting for you");
  assert.match(phase({ suspended: true }).explain, /No process is holding it, so it survives a restart/);
  assert.equal(phase({ suspended: false }).label, "Waiting for you");
  assert.equal(phase({ status: "approved", suspended: true }).label, "Approved · resuming");
  assert.equal(phase({ status: "consumed", suspended: false }).label, "Approved · resumed and run");
  assert.equal(phase({ status: "consumed", suspended: false, task: null }).label, "Approved and run", "a standalone action was never suspended, so it is not described as resumed");
  for (const status of ["rejected", "expired", "invalidated"]) assert.equal(phase({ status }).label, approvals.APPROVAL_STATUS[status].label);
});

test("approval and suspension events read in plain words, name the actor, show the tool and step and never a value", () => {
  for (const type of ["approval_requested", "task_suspended", "approval_approved", "approval_rejected", "approval_expired", "approval_invalidated", "approval_consumed", "task_resumed"]) {
    assert.ok(approvals.EVENT_TEXT[type].label.length > 8 && approvals.isLifecycleEvent(type), type);
  }
  assert.ok(approvals.isLifecycleEvent("run_resumed") && approvals.isLifecycleEvent("recovery_performed") && !approvals.isLifecycleEvent("task_started"));
  assert.deepEqual([approvals.actorLabel("user"), approvals.actorLabel("worker"), approvals.actorLabel("recovery"), approvals.actorLabel("???")], ["a person", "a worker", "recovery", "the system"]);
  assert.equal(approvals.eventDetail({ tool: "demo.send_note@1", seq: 1, reason: "lease_expired" }), "demo.send_note@1 · step 1 · lease expired");
  assert.equal(approvals.eventDetail({}), null);
});

test("the decision panel tells a member they cannot decide and offers a decision only to an owner or admin", () => {
  const ui = read("app/studio/_approvals.tsx");
  assert.match(ui, /!item\.can_decide/);
  assert.match(ui, /Only a workspace owner or admin can approve or reject/);
  assert.ok(ui.indexOf("!item.can_decide") < ui.indexOf("Approve this exact action"), "the member's notice comes before any decision control");
  assert.match(read("app/studio/runs/[id]/page.tsx"), /getApi\(\)\.getRunEvents/);
});
