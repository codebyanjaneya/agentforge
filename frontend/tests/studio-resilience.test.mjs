// M6b: provider retry wording, required-action status, and that a revision never drops a task's required action. Pure logic and the typed client (no network).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import { editorFromRevision, payloadFromEditor } from "../lib/studio/forms.ts";
import { PROVIDER_STATE, providerSentence, requiredActionStatus } from "../lib/studio/resilience.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => readFileSync(path.join(FRONTEND, rel), "utf8");

test("every retry state has a label and an honest sentence built only from recorded facts", () => {
  assert.deepEqual(Object.keys(PROVIDER_STATE).sort(), ["exhausted", "failed", "recovered", "retrying", "waiting"]);
  assert.equal(PROVIDER_STATE.waiting.label, "Waiting before retry");
  assert.equal(PROVIDER_STATE.exhausted.label, "Rate limit exhausted");
  assert.equal(PROVIDER_STATE.failed.label, "Provider unavailable");
  assert.match(providerSentence({ state: "waiting", attempt: 1, error_code: "rate_limited", wait_ms: 1500 }), /Waiting 1\.5 s before trying again\. Nothing was lost/);
  assert.match(providerSentence({ state: "retrying", attempt: 2, error_code: "rate_limited", wait_ms: null }), /attempt 2/);
  assert.match(providerSentence({ state: "recovered", attempt: 3, error_code: null, wait_ms: null }), /nothing was run twice/);
  const spent = providerSentence({ state: "exhausted", attempt: 4, error_code: "rate_limited", wait_ms: null }, "failed");
  assert.match(spent, /budget is spent \(4 attempts\)/);
  assert.match(spent, /finished tasks keep their results/);
  const quota = providerSentence({ state: "exhausted", attempt: 1, error_code: "rate_limited", wait_ms: 623000 }, "failed");
  assert.match(quota, /asked us to wait about 11 minutes \(a usage limit\)/);
  assert.ok(!/budget is spent/.test(quota), "a usage limit is not described as a spent retry budget");
  assert.ok(!/finished tasks/.test(providerSentence({ state: "exhausted", attempt: 4, error_code: "rate_limited", wait_ms: null }, "running")), "a live state does not claim the task failed");
  assert.match(providerSentence({ state: "failed", attempt: 4, error_code: "unavailable", wait_ms: null }, "failed"), /was unavailable after 4 attempts/);
  assert.match(providerSentence({ state: "failed", attempt: 1, error_code: "timeout", wait_ms: null }), /did not answer in time after 1 attempt\./);
  assert.ok(PROVIDER_STATE.waiting.live && PROVIDER_STATE.retrying.live && !PROVIDER_STATE.recovered.live && !PROVIDER_STATE.exhausted.live);
});

const call = (status, task = "t3", id = "demo.send_note", version = 1) => ({ task, status, tool: { id, version } });

test("a required action counts as performed only when the tool call is recorded as succeeded", () => {
  const ref = "demo.send_note@1";
  assert.equal(requiredActionStatus(ref, [call("succeeded")], "t3", false, "succeeded").label, "Performed");
  assert.equal(requiredActionStatus(ref, [], "t3", true, "running").label, "Waiting for approval");
  assert.equal(requiredActionStatus(ref, [call("rejected")], "t3", false, "failed").label, "Not performed");
  assert.equal(requiredActionStatus(ref, [], "t3", false, "failed").label, "Not performed");
  assert.equal(requiredActionStatus(ref, [], "t3", false, "running").label, "Not yet invoked");
  // another task's call, another tool, or another version never counts
  assert.equal(requiredActionStatus(ref, [call("succeeded", "t2")], "t3", false, "running").label, "Not yet invoked");
  assert.equal(requiredActionStatus(ref, [call("succeeded", "t3", "text.analyze")], "t3", false, "running").label, "Not yet invoked");
  assert.equal(requiredActionStatus(ref, [call("succeeded", "t3", "demo.send_note", 2)], "t3", false, "running").label, "Not yet invoked");
});

test("a revision carries each task's required action unchanged and a new task has none", () => {
  const rev = { tasks: [{ id: "t1", title: "x", description: "d", expected_output: "e", agent: { key: "a" }, depends_on: [], dependency_reason: null, complexity: "low", priority: "normal", rationale: "r", required_tool: "demo.send_note@1" },
                        { id: "t2", title: "y", description: "d", expected_output: "e", agent: { key: "a" }, depends_on: [], dependency_reason: null, complexity: "low", priority: "normal", rationale: "r", required_tool: null }] };
  const editor = editorFromRevision(rev);
  assert.deepEqual(editor.map((t) => t.required_tool), ["demo.send_note@1", null]);
  const payload = payloadFromEditor(editor, 1, null);
  assert.equal(payload.tasks[0].required_tool, "demo.send_note@1");
  assert.ok(!("required_tool" in payload.tasks[1]));
});

test("the provider-events call is a plain GET that sends no identity", async () => {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { items: [], by_task: {} }));
  const client = createApiClient({ baseUrl: "https://api.example.com", tokens, fetchImpl: backend });
  await client.getRunProviderEvents("r1");
  assert.equal(`${backend.calls[0].init.method} ${new URL(backend.calls[0].url).pathname}`, "GET /plan-runs/r1/provider-events");
});

test("the run page shows provider state and required actions, and a live state is announced politely", () => {
  const ui = read("app/studio/_provider.tsx");
  assert.match(ui, /aria-live="polite"/);
  assert.match(ui, /aria-label="Model provider status per task"/);
  const page = read("app/studio/runs/[id]/page.tsx");
  assert.match(page, /getRunProviderEvents\(id\)/);
  assert.match(page, /title="Model provider"/);
  assert.match(page, /title="Required actions"/);
  assert.ok(!/setInterval[^]{0,200}getRunProviderEvents/.test(page), "no second timer: it is re-read with the run's own poll");
});
