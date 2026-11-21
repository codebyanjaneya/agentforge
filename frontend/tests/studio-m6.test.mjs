// M6: usage and cost wording, the workspace selection (a request the server verifies), the new client calls, and the accessibility-relevant source invariants.
// In-memory fakes only (no network, no backend process).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import { costLabel, formatCost, tokens } from "../lib/studio/usage.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(path.join(FRONTEND, rel), "utf8");
const BASE = "https://api.example.com";
const WS = "3f2b8c1e-9d4a-4b6e-8a57-1c2d3e4f5a6b";

const sum = (over = {}) => ({ requests: 2, input_tokens: 1000, output_tokens: 200, cached_tokens: 0, requests_without_usage: 0, cost_micros: 300, cost_complete: true, requests_without_price: 0, ...over });

test("costs are shown as dollars with honest precision and never invented", () => {
  assert.equal(formatCost(0), "$0");
  assert.equal(formatCost(900_000), "$0.90");
  assert.equal(formatCost(150), "$0.00015");
  assert.equal(formatCost(2_500_000), "$2.50");
  assert.equal(tokens(1234567), "1,234,567");
  assert.deepEqual(costLabel(sum()), { text: "$0.0003", note: null });
  assert.deepEqual(costLabel(sum({ requests: 0, cost_micros: null, cost_complete: false })), { text: "No requests", note: null });
  const none = costLabel(sum({ cost_micros: null, cost_complete: false, requests_without_usage: 2 }));
  assert.equal(none.text, "Not available");
  assert.match(none.note, /no token counts/);
  assert.match(costLabel(sum({ cost_micros: null, cost_complete: false, requests_without_price: 2 })).note, /No price is configured/);
  const partial = costLabel(sum({ cost_complete: false, requests_without_usage: 1, requests_without_price: 0 }));
  assert.match(partial.text, /^at least \$/);
  assert.match(partial.note, /Incomplete: 1 request reported no token counts/);
});

test("the workspace header is sent only for a selected UUID, never for the workspace routes, and never when nothing is selected", async () => {
  const now = clock();
  const tokensManager = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  let selected = null;
  const backend = fakeFetch(() => json(200, { items: [] }));
  const client = createApiClient({ baseUrl: BASE, tokens: tokensManager, fetchImpl: backend, workspace: () => selected });
  await client.listApprovals();
  selected = "not-a-uuid";
  await client.listApprovals();
  selected = WS;
  await client.listApprovals();
  await client.listWorkspaces();
  await client.listWorkspaceMembers(WS);
  assert.deepEqual(backend.calls.map((c) => c.headers.get("x-workspace-id")), [null, null, WS, null, null]);
});

test("the M6 client calls send the documented requests", async () => {
  const now = clock();
  const tokensManager = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { items: [] }));
  const client = createApiClient({ baseUrl: BASE, tokens: tokensManager, fetchImpl: backend });
  await client.createWorkspace("Launch team");
  await client.addWorkspaceMember(WS, "a@example.com", "admin");
  await client.listNotifications({ unread: true, limit: 20 });
  await client.getNotificationCounts();
  await client.markNotificationRead("n1");
  await client.markAllNotificationsRead();
  await client.getRunUsage("r1");
  const seen = backend.calls.map((c) => `${c.init.method} ${new URL(c.url).pathname}${new URL(c.url).search} ${c.init.body ?? ""}`.trim());
  assert.deepEqual(seen, [
    'POST /workspaces {"name":"Launch team"}',
    `POST /workspaces/${WS}/members {"email":"a@example.com","role":"admin"}`,
    "GET /notifications?unread=true&limit=20",
    "GET /notifications/count",
    "POST /notifications/n1/read",
    "POST /notifications/read",
    "GET /plan-runs/r1/usage",
  ]);
});

test("the selection store accepts only UUIDs, survives missing storage and announces a change", async () => {
  const events = [];
  const store = new Map();
  globalThis.window = {
    sessionStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) },
    dispatchEvent: (e) => events.push(e.detail),
  };
  globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
  const { activeWorkspace, selectWorkspace } = await import("../lib/workspace.ts");
  assert.equal(activeWorkspace(), null);
  selectWorkspace(WS);
  assert.equal(activeWorkspace(), WS);
  selectWorkspace("../etc/passwd");
  assert.equal(activeWorkspace(), null);
  selectWorkspace(WS);
  store.set("agentforge.workspace", "tampered; value");
  assert.equal(activeWorkspace(), null, "a tampered stored value is never sent");
  window.sessionStorage.getItem = () => { throw new Error("blocked"); };
  window.sessionStorage.setItem = () => { throw new Error("blocked"); };
  selectWorkspace(WS);
  assert.equal(activeWorkspace(), WS, "falls back to memory when storage is blocked");
  assert.deepEqual(events.slice(0, 2), [WS, null]);
  delete globalThis.window;
});

test("accessibility: the badge is announced politely, decisions and errors are live regions, the primary button uses an accessible fill, and motion respects the user", () => {
  const layout = read("app/studio/layout.tsx");
  assert.match(layout, /role="status" aria-live="polite"[\s\S]{0,80}inbox-announcer|aria-live="polite" aria-atomic="true" data-testid="inbox-announcer"/);
  assert.match(layout, /sr-only/);
  const team = read("app/studio/_team.tsx");
  assert.match(team, /intervalMs: 15000/, "the badge polls on a bounded interval");
  assert.match(team, /htmlFor="workspace-switcher"/, "the switcher has a label");
  const css = read("app/studio/studio.css");
  assert.match(css, /\.st-btn-primary \{\s*background: var\(--st-ember-btn\);/);
  assert.match(css, /--st-ember-btn: #b43e00;/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  const approvals = read("app/studio/_approvals.tsx");
  assert.match(approvals, /role="status" aria-live="polite"/);
  const usage = read("app/studio/_usage.tsx");
  assert.match(usage, /<caption className="sr-only">/);
  assert.match(usage, /scope="col"/);
});
