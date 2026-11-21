// M4-B: the tool presentation logic and the tool client methods. In-memory fakes only (no network, no backend process).

import assert from "node:assert/strict";
import { test } from "node:test";

import { createApiClient, createTokenManager } from "../lib/api-client.ts";
import * as forms from "../lib/studio/forms.ts";
import * as tools from "../lib/studio/tools.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const BASE = "https://api.example.com";

// What pydantic really emits for the unit-economics input and the text-analysis output (nullable fields, nested models, bounds, enums, dates).
const UNIT = {
  type: "object",
  required: ["monthly_price", "gross_margin_pct"],
  properties: {
    monthly_price: { type: "number", exclusiveMinimum: 0, maximum: 1e9, description: "Revenue per customer per month" },
    gross_margin_pct: { type: "number", exclusiveMinimum: 0, maximum: 100 },
    note: { anyOf: [{ type: "string", maxLength: 40 }, { type: "null" }], default: null },
    plan: { enum: ["a", "b"], type: "string" },
    start: { type: "string", format: "date" },
    top: { type: "integer", minimum: 1, maximum: 15, default: 8 },
  },
};
const TEXT_OUT = {
  $defs: { Keyword: { title: "Keyword", type: "object", required: ["word", "count"], properties: { word: { type: "string" }, count: { type: "integer" } } } },
  type: "object",
  required: ["words", "top_keywords"],
  properties: { words: { type: "integer" }, top_keywords: { type: "array", items: { $ref: "#/$defs/Keyword" } }, flesch: { anyOf: [{ type: "number" }, { type: "null" }] } },
};

test("schemas become ports in plain language: types, required marks and limits", () => {
  const ports = Object.fromEntries(tools.portsOf(UNIT).map((p) => [p.name, p]));
  assert.deepEqual([ports.monthly_price.type, ports.monthly_price.required, ports.monthly_price.constraints], ["number", true, ["greater than 0", "at most 1,000,000,000"]]);
  assert.equal(ports.monthly_price.description, "Revenue per customer per month");
  assert.deepEqual([ports.note.type, ports.note.required, ports.note.constraints], ["text", false, ["up to 40 characters"]]);
  assert.equal(ports.plan.type, "one of: a | b");
  assert.equal(ports.start.type, "date (YYYY-MM-DD)");
  assert.deepEqual([ports.top.type, ports.top.constraints], ["whole number", ["at least 1", "at most 15", "default 8"]]);
  const out = Object.fromEntries(tools.portsOf(TEXT_OUT).map((p) => [p.name, p]));
  assert.equal(out.top_keywords.type, "list of keyword");
  assert.deepEqual(out.top_keywords.fields.map((f) => [f.name, f.type, f.required]), [["word", "text", true], ["count", "whole number", true]]); // a list of objects shows one element's fields
  assert.equal(out.flesch.required, false);
  assert.deepEqual(tools.portsOf(null), []);
  assert.deepEqual(tools.portsOf({}), []);
  assert.doesNotThrow(() => tools.portsOf({ type: "object", properties: { loop: { $ref: "#/$defs/Missing" } } }), "an unknown reference never crashes the screen");
});

test("every invocation status has a label, a tone and a sentence, and failures are explained from fixed codes only", () => {
  for (const status of ["running", "succeeded", "failed", "timed_out", "rejected", "cancelled", "interrupted", "uncertain"]) {
    const meta = tools.INVOCATION_STATUS[status];
    assert.ok(meta.label && meta.tone && meta.glyph && meta.explain, status);
  }
  assert.equal(tools.explainInvocation("succeeded", null), null);
  assert.equal(tools.explainInvocation("running", null), null);
  assert.match(tools.explainInvocation("rejected", { code: "not_permitted", message: "x" }), /has not been given/);
  assert.match(tools.explainInvocation("rejected", { code: "approval_required", message: "x" }), /human approval/);
  assert.match(tools.explainInvocation("failed", { code: "invalid_input", message: "x" }), /did not fit/);
  assert.match(tools.explainInvocation("timed_out", { code: "timeout", message: "x" }), /time limit/);
  assert.equal(tools.explainInvocation("uncertain", { code: "uncertain", message: "x" }), tools.INVOCATION_STATUS.uncertain.explain, "an unknown code falls back to the status sentence, never to raw text");
  assert.ok(!/SECRET/.test(tools.explainInvocation("failed", { code: "tool_error", message: "SECRET internal text" })));
});

test("risk is said plainly, values are summarised safely and calls are grouped by task in the order they were made", () => {
  assert.deepEqual([tools.riskLabel("read", "none").label, tools.riskLabel("read", "none").tone], ["Read-only", "good"]);
  assert.equal(tools.riskLabel("write", "non_idempotent").label, "Writes data");
  assert.match(tools.riskLabel("external", "non_idempotent").detail, /approval/);
  assert.equal(tools.brief({ a: 1, b: "x", c: { d: 1 } }), "a: 1 · b: x · c: …");
  assert.equal(tools.brief("y".repeat(200), 20).length, 20);
  assert.equal(tools.brief(null), "—");
  assert.deepEqual([tools.durationLabel(null), tools.durationLabel(35), tools.durationLabel(1500), tools.durationLabel(12000)], ["", "35 ms", "1.5 s", "12 s"]);
  const item = (id, task, seq, at) => ({ id, task, seq, created_at: at });
  const grouped = tools.byTask([item("3", "b", 1, "2026-01-01T00:00:03"), item("1", "a", 2, "2026-01-01T00:00:02"), item("0", "a", 1, "2026-01-01T00:00:01"), item("9", null, 1, "2026-01-01T00:00:00")]);
  assert.deepEqual(Object.fromEntries(Object.entries(grouped).map(([k, v]) => [k, v.map((i) => i.id)])), { a: ["0", "1"], b: ["3"] }); // standalone calls have no task
});

test("selecting tools in the editor works by id, keeps pinned references and respects the limit", () => {
  assert.equal(tools.isSelected(["math.calculate@1"], "math.calculate"), true);
  assert.equal(tools.isSelected(["math.calculate@1"], "math"), false);
  assert.equal(tools.isSelected([], "math.calculate"), false);
  assert.deepEqual(tools.toggleTool([], "math.calculate"), ["math.calculate"]);
  assert.deepEqual(tools.toggleTool(["math.calculate@1", "time.date_math"], "math.calculate"), ["time.date_math"]);
  assert.equal(tools.MAX_TOOLS_PER_AGENT, 8);
});

test("the agent form carries the chosen tools into the request and back, and nothing else decides them", () => {
  const blank = forms.blankForm();
  assert.deepEqual(blank.tools, []);
  assert.deepEqual(forms.definitionFromForm({ ...blank, name: "Writer", tools: ["math.calculate", "time.date_math@1"] }).allowed_tools, ["math.calculate", "time.date_math@1"]);
  assert.deepEqual(forms.definitionFromForm(blank).allowed_tools, []);
  const stored = { name: "A", description: "d", role: "r", purpose: "p", instructions: "i", capabilities: [], allowed_tools: ["math.calculate@1"], input_expectations: "", output_expectations: "", model: { provider: "groq", name: "m", temperature: 0.5, max_tokens: null }, metadata: {} };
  assert.deepEqual(forms.formFromDefinition(stored).tools, ["math.calculate@1"]);
  const copy = forms.formFromDefinition(stored);
  copy.tools.push("x");
  assert.deepEqual(stored.allowed_tools, ["math.calculate@1"], "editing the form never mutates the stored definition");
});

test("the tool client methods send the documented requests and no ownership identity", async () => {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backend = fakeFetch(() => json(200, { items: [], next_cursor: null }));
  const client = createApiClient({ baseUrl: BASE, tokens, fetchImpl: backend });
  await client.listTools();
  await client.getTool("math.calculate");
  await client.listToolInvocations();
  await client.listToolInvocations({ runId: "r1", toolId: "math.calculate", agent: "writer", status: "failed", limit: 10, cursor: "v1.a.b" });
  await client.getToolInvocation("i1");
  await client.getRunToolInvocations("r1");
  const seen = backend.calls.map((c) => {
    const url = new URL(c.url);
    return `${c.init.method} ${url.pathname}${url.search}`;
  });
  assert.deepEqual(seen, [
    "GET /tools", "GET /tools/math.calculate", "GET /tool-invocations", "GET /tool-invocations?run_id=r1&tool_id=math.calculate&agent=writer&status=failed&limit=10&cursor=v1.a.b",
    "GET /tool-invocations/i1", "GET /plan-runs/r1/tool-invocations",
  ]);
  for (const c of backend.calls) assert.ok(c.init.body === undefined && !/user_id|workspace|email|owner/i.test(c.url), "read-only requests with no identity");
});
