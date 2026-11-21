// M5: the Studio home. Pure logic and source invariants; in-memory fakes only (no network, no backend process).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import * as home from "../lib/studio/home.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/\{\/\*[\s\S]*?\*\/\}/g, "");
const read = (rel) => strip(readFileSync(path.join(FRONTEND, rel), "utf8"));
const plan = (id, status, objective = "Check the price and tell the first customer about the launch.") => ({ id, status, objective });

test("the next step is the most useful one: a decision only a person can make comes first, then the way to a first result", () => {
  const base = { agentCount: 3, plans: [], pendingApprovals: 0 };
  assert.equal(home.nextAction({ ...base, pendingApprovals: 2, agentCount: 0 }).kind, "approve");
  assert.match(home.nextAction({ ...base, pendingApprovals: 2 }).title, /2 agents are waiting for your decision/);
  assert.match(home.nextAction({ ...base, pendingApprovals: 1 }).title, /An agent is waiting/);
  assert.equal(home.nextAction({ ...base, pendingApprovals: 1 }).href, "/studio/approvals");
  const none = home.nextAction({ ...base, agentCount: 0 });
  assert.deepEqual([none.kind, none.href, none.cta], ["assemble", null, "Add the starter workforce"]);
  const review = home.nextAction({ ...base, plans: [plan("p1", "approved"), plan("p2", "draft")] });
  assert.deepEqual([review.kind, review.href], ["review", "/studio/plans/p2"]);
  assert.deepEqual([home.nextAction({ ...base, plans: [plan("p1", "approved")], lastRun: null }).kind, home.nextAction({ ...base, plans: [plan("p1", "approved")], lastRun: null }).href], ["start", "/studio/plans/p1"]);
  const running = home.nextAction({ ...base, plans: [plan("p1", "approved")], lastRun: { id: "r1", status: "running" } });
  assert.deepEqual([running.kind, running.href], ["follow", "/studio/runs/r1"]);
  const done = home.nextAction({ ...base, plans: [plan("p1", "approved")], lastRun: { id: "r2", status: "partial" } });
  assert.deepEqual([done.kind, done.href, /partly completed/.test(done.detail)], ["result", "/studio/runs/r2", true]);
  assert.doesNotMatch(done.title + done.detail, /ready to run/, "a plan that already ran is never offered as ready to run");
  assert.equal(home.nextAction({ ...base, plans: [plan("p1", "cancelled")] }).kind, "delegate");
  assert.ok(home.nextAction({ ...base, plans: [plan("p2", "draft", "x".repeat(300))] }).detail.length < 200, "a long objective is shortened, never overflows");
});

test("control is explained in three plain lines that match what the product really does", () => {
  assert.deepEqual(home.CONTROL_LINES.map((l) => l.name), ["Runs by itself", "Asks you first", "Always recorded"]);
  assert.match(home.CONTROL_LINES[0].text, /Read-only tools/);
  assert.match(home.CONTROL_LINES[1].text, /exact tool, version and values/);
  assert.match(home.CONTROL_LINES[2].text, /secret/);
});

test("demo status and the starter workforce are read and created through the typed client only", async () => {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { demo: true, agents: [] }));
  const client = createApiClient({ baseUrl: "https://api.example.com", tokens, fetchImpl: backend });
  await client.getDemo();
  await client.createStarterWorkforce();
  assert.deepEqual(backend.calls.map((c) => `${c.init.method} ${new URL(c.url).pathname} ${c.init.body ?? ""}`.trim()), ["GET /demo", "POST /starter-workforce"]);
});

test("the home page shows only what the backend reports, labels demo mode honestly and never auto-creates anything", () => {
  const page = read("app/studio/page.tsx");
  const parts = read("app/studio/_home.tsx");
  assert.match(page, /getApi\(\)\.listApprovals/);
  assert.ok(!/Math\.random|faker|lorem/i.test(page + parts), "no invented data");
  assert.match(parts, /DEMO MODE/);
  assert.match(parts, /script<\/strong>, not a live model/);
  assert.match(parts, /simulation<\/strong> that sends nothing/);
  assert.equal((parts.match(/createStarterWorkforce\(/g) ?? []).length, 1, "one place creates the workforce (a hook used by every button)");
  assert.match(parts, /onClick=\{\(\) => void add\(\)\}/, "creating the workforce is an explicit click");
  assert.match(page, /void starter\.add\(\)/, "the primary next-step button creates it directly, from the click");
  assert.ok(!/useEffect[^]{0,300}(createStarterWorkforce|starter\.add)/.test(parts + page), "never from an effect");
});
