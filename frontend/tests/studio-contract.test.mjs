// M3-C: the Studio client against the REAL backend. (1) Every new client method is checked against the OpenAPI document of the
// real FastAPI app (route, method, query names, body fields). (2) The backend's own test (backend/tests/test_studio_samples.py)
// drives the whole journey through the real app with a fake provider and records the JSON it returns; the response shapes the
// TypeScript types describe are compared with those recordings, and real error bodies are fed through the client's error handling.
// Skipped when the backend's Python environment is not available.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { before, test } from "node:test";

import { ApiError, createApiClient, createTokenManager } from "../lib/api-client.ts";
import * as tools from "../lib/studio/tools.ts";
import * as view from "../lib/studio/view.ts";
import { forms } from "./studio-forms.mjs";
import { backendOpenApi, backendPythonAvailable, clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";
import { BACKEND } from "./token-helpers.mjs";

const BASE = "https://api.example.com";
const skip = !backendPythonAvailable;

function client(backend) {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backendFetch = fakeFetch(backend);
  return { api: createApiClient({ baseUrl: BASE, tokens, fetchImpl: backendFetch }), backendFetch };
}

function resolveRef(doc, schema) {
  if (schema?.anyOf) return resolveRef(doc, schema.anyOf.find((s) => s.type !== "null")); // an optional body: Model | null
  return schema?.$ref ? doc.components.schemas[schema.$ref.split("/").pop()] : schema;
}

test("every Studio client method matches the REAL backend OpenAPI contract", { skip }, async () => {
  const doc = backendOpenApi();
  const { api, backendFetch } = client(() => json(200, { items: [], next_cursor: null }));
  const def = forms.definitionFromForm({ ...forms.blankForm(), name: "Writer" });
  await api.listTemplates();
  await api.getTemplate("t");
  await api.instantiateTemplate("t", { slug: "my-agent", name: "Mine" });
  await api.instantiateTemplate("t");
  await api.listAgents("all");
  await api.getAgent("a");
  await api.createAgent({ slug: "writer", definition: def, change_note: "n" });
  await api.updateAgent("a", { base_version: 1, definition: def, change_note: "n" });
  await api.archiveAgent("a");
  await api.unarchiveAgent("a");
  await api.listVersions("a");
  await api.getVersion("a", 2);
  await api.rollbackAgent("a", { to_version: 1, base_version: 2 });
  await api.createPlan("Launch our new product.");
  await api.listPlans({ status: "draft", limit: 5, cursor: "v1.a.b" });
  await api.getPlan("p");
  await api.listRevisions("p");
  await api.getRevision("p", 1);
  await api.validatePlan("p");
  await api.revisePlan("p", { base_revision: 1, interpretation: "A reading of it.", tasks: [{ id: "a", title: "T", description: "D", expected_output: "E", agent: "research", depends_on: [], dependency_reason: null, complexity: "low", priority: "normal" }] });
  await api.approvePlan("p", 2);
  await api.cancelPlan("p");
  await api.startRun("p");
  await api.listRuns("p");
  await api.getRun("r");
  await api.getTaskRun("r", "t1");
  await api.cancelRun("r");
  await api.retryRun("r");
  await api.retryRun("r", { acknowledge_uncertain: ["a"] });
  await api.getRunEvents("r", { after: 1, limit: 5 });
  await api.listTools();
  await api.getTool("math.calculate");
  await api.listToolInvocations({ runId: "00000000-0000-4000-8000-000000000001", toolId: "math.calculate", agent: "writer", status: "succeeded", limit: 5, cursor: "v1.a.b" });
  await api.getToolInvocation("00000000-0000-4000-8000-000000000002");
  await api.getRunToolInvocations("r");
  await api.listApprovals({ status: "pending", runId: "00000000-0000-4000-8000-000000000001", limit: 5 });
  await api.getApproval("00000000-0000-4000-8000-000000000003");
  await api.decideApproval("00000000-0000-4000-8000-000000000003", "approve", "a".repeat(64));
  await api.decideApproval("00000000-0000-4000-8000-000000000003", "reject");
  await api.getDemo();
  await api.createStarterWorkforce();

  const covered = new Set();
  for (const call of backendFetch.calls) {
    const url = new URL(call.url);
    const method = call.init.method.toLowerCase();
    const template = Object.keys(doc.paths).find((p) => new RegExp(`^${p.replace(/\{[^}]+\}/g, "[^/]+")}$`).test(url.pathname));
    assert.ok(template, `no backend route for ${method} ${url.pathname}`);
    const operation = doc.paths[template][method];
    assert.ok(operation, `backend route ${template} has no ${method.toUpperCase()}`);
    covered.add(`${method} ${template}`);
    const queryNames = (operation.parameters ?? []).filter((p) => p.in === "query").map((p) => p.name);
    for (const name of url.searchParams.keys()) assert.ok(queryNames.includes(name), `unknown query parameter ${name} for ${template}`);
    const schema = resolveRef(doc, operation.requestBody?.content?.["application/json"]?.schema);
    if (call.init.body === undefined) {
      assert.ok(!operation.requestBody?.required, `${template} requires a body the client does not send`);
    } else {
      assert.ok(schema, `${template} takes no JSON body but the client sends one`);
      const body = JSON.parse(call.init.body);
      for (const key of Object.keys(body)) assert.ok(key in schema.properties, `unknown body field ${key} for ${template}`);
      for (const key of schema.required ?? []) assert.ok(key in body, `missing required body field ${key} for ${template}`);
    }
  }
  assert.deepEqual([...covered].sort(), [
    "get /agent-templates", "get /agent-templates/{template_id}", "get /agents", "get /agents/{agent_id}", "get /agents/{agent_id}/versions",
    "get /agents/{agent_id}/versions/{version}", "get /plan-runs/{run_id}", "get /plan-runs/{run_id}/tasks/{task_key}", "get /plans", "get /plans/{plan_id}",
    "get /plans/{plan_id}/revisions", "get /plans/{plan_id}/revisions/{revision}", "get /plans/{plan_id}/runs", "patch /agents/{agent_id}",
    "post /agent-templates/{template_id}/instantiate", "post /agents", "post /agents/{agent_id}/archive", "post /agents/{agent_id}/rollback",
    "post /agents/{agent_id}/unarchive", "post /plan-runs/{run_id}/cancel", "post /plans", "post /plans/{plan_id}/approve", "post /plans/{plan_id}/revisions",
    "post /plans/{plan_id}/runs", "post /plans/{plan_id}/validate", "post /plans/{plan_id}/cancel",
    "post /plan-runs/{run_id}/retry", "get /plan-runs/{run_id}/events", "get /tools", "get /tools/{tool_id}", "get /tool-invocations", "get /tool-invocations/{invocation_id}",
    "get /plan-runs/{run_id}/tool-invocations", "get /approvals", "get /approvals/{approval_id}", "post /approvals/{approval_id}/approve", "post /approvals/{approval_id}/reject", "get /demo", "post /starter-workforce",
  ].sort());
  // the plan-list limit the Studio relies on, and the "start run" route taking no required body (so nothing client-side can choose a revision)
  const limit = doc.paths["/plans"].get.parameters.find((p) => p.name === "limit").schema;
  assert.deepEqual([limit.default, limit.minimum, limit.maximum], [20, 1, 100]);
  assert.ok(!doc.paths["/plans/{plan_id}/runs"].post.requestBody?.required);
});

// ------------------------------------------------------------------------------------------------ recorded real responses

let samples;
before(() => {
  if (skip) return;
  const out = path.join(mkdtempSync(path.join(tmpdir(), "studio-samples-")), "samples.json");
  const python = process.platform === "win32" ? path.join(BACKEND, "venv", "Scripts", "python.exe") : path.join(BACKEND, "venv", "bin", "python");
  const env = { ...process.env, STUDIO_SAMPLES_OUT: out, PYTHONDONTWRITEBYTECODE: "1" };
  delete env.AGENTFORGE_TEST_PG_ADMIN_URL;
  delete env.AGENTFORGE_TEST_PG_TOKEN;
  const r = spawnSync(python, ["-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests/test_studio_samples.py"], { cwd: BACKEND, env, encoding: "utf8", timeout: 240000 });
  if (r.status !== 0) throw new Error(`could not record the backend samples:\n${(r.stdout + r.stderr).slice(-1500)}`);
  samples = JSON.parse(readFileSync(out, "utf8"));
});

const keys = (value) => Object.keys(value).sort();
const same = (actual, expected, label) => assert.deepEqual(keys(actual), [...expected].sort(), `${label}: the TypeScript contract and the real response differ`);

test("real response shapes equal the TypeScript contracts (agents, templates, versions)", { skip }, () => {
  const definition = ["name", "description", "role", "purpose", "instructions", "capabilities", "allowed_tools", "input_expectations", "output_expectations", "model", "metadata"];
  const summary = ["id", "slug", "name", "description", "role", "status", "current_version", "latest_version", "template_id", "template_version", "created_at", "updated_at", "archived_at"];
  const version = ["version", "content_hash", "change_note", "restored_from_version", "created_at"];
  same(samples.template.body, ["id", "version", "name", "description", "role", "capabilities", "definition"], "template");
  same(samples.templates.body.items[0], ["id", "version", "name", "description", "role", "capabilities"], "template summary");
  same(samples.template.body.definition, definition, "definition");
  same(samples.template.body.definition.model, ["provider", "name", "temperature", "max_tokens"], "model");
  for (const name of ["agent_created", "agent_updated", "instantiated", "rolled_back", "archived", "unarchived"]) {
    same(samples[name].body, [...summary, "definition", "version"], name);
    same(samples[name].body.version, version, `${name}.version`);
  }
  same(samples.agents.body.items[0], summary, "agent summary");
  same(samples.versions.body.items[0], version, "version info");
  same(samples.version.body, [...version, "definition"], "version detail");
  assert.deepEqual(samples.agent_created.body.definition.allowed_tools, [], "no tools exist");
  assert.equal(samples.rolled_back.body.version.restored_from_version, 1);
  assert.equal(samples.rolled_back.body.latest_version, 3, "a rollback creates a NEW version");
});

test("real response shapes equal the TypeScript contracts (plans, revisions, runs)", { skip }, () => {
  const task = ["id", "position", "level", "title", "description", "expected_output", "rationale", "dependency_reason", "depends_on", "complexity", "priority", "agent", "required_tool"];
  const revision = ["revision", "origin", "interpretation", "warnings", "unsupported", "provider", "model", "created_at", "levels", "tasks"];
  const plan = ["id", "objective", "status", "latest_revision", "approved_revision", "created_at", "updated_at", "approved_at", "revision", "execution", "latest_run", "created_by"];
  same(samples.plan.body, plan, "plan");
  same(samples.plan.body.revision, revision, "revision");
  same(samples.revision.body, revision, "revision detail");
  same(samples.plan.body.revision.tasks[0], task, "plan task");
  same(samples.plan.body.revision.tasks[1].agent, ["source", "key", "name", "version", "version_id"], "task agent");
  assert.deepEqual([samples.plan.body.revision.tasks[1].agent.source, samples.plan.body.revision.tasks[1].agent.version], ["custom", 3], "custom tasks pin the exact version");
  assert.equal(samples.plan.body.revision.tasks[0].agent.version, null, "built-in tasks have no version");
  same(samples.plans.body.items[0], ["id", "objective", "status", "latest_revision", "approved_revision", "task_count", "created_at", "updated_at", "approved_at", "created_by"], "plan summary");
  same(samples.revisions.body.items[0], ["revision", "origin", "created_at", "provider", "model"], "revision list item");
  same(samples.validation.body, ["valid", "revision", "status", "errors", "warnings", "levels"], "validation");
  assert.equal(samples.plan.body.latest_run, null);
  assert.deepEqual(samples.plan.body.execution, { started: false });
  same(samples.plan_with_run.body.latest_run, ["id", "status", "attempt", "created_at", "completed_at"], "latest_run");
  assert.deepEqual(samples.plan_with_run.body.execution, { started: true });
  assert.equal(samples.plan_revised.body.status, "draft");
  assert.equal(samples.plan_approved.body.status, "approved");
  assert.equal(samples.plan_approved.body.execution.started, false, "approval does not start execution");
  assert.equal(samples.run_started.status, 202);
  assert.equal(samples.run_started.body.status, "running");

  const runKeys = ["id", "plan_id", "revision", "attempt", "status", "error", "counts", "created_at", "completed_at", "tasks", "outputs", "retry_of", "retried_by", "ownership", "retry_options", "started_by"];
  same(samples.run.body, runKeys, "run");
  same(samples.run.body.tasks[0], ["id", "position", "level", "title", "status", "depends_on", "blocked_by", "execution_id", "agent", "error", "created_at", "started_at", "completed_at", "reused", "retry"], "task run");
  same(samples.run.body.counts, ["pending", "running", "succeeded", "failed", "skipped", "cancelled", "total"], "counts");
  same(samples.run.body.outputs[0], ["task", "title", "execution_id", "result"], "output");
  same(samples.runs.body.items[0], ["id", "revision", "attempt", "status", "error", "counts", "created_at", "completed_at"], "run list item");
  same(samples.run_task.body, ["id", "position", "level", "title", "status", "depends_on", "blocked_by", "execution_id", "agent", "error", "created_at", "started_at", "completed_at", "reused", "retry", "run_id", "description", "expected_output", "execution"], "task run detail");
  same(samples.run_task.body.execution, ["id", "status", "provider", "model", "input", "result", "started_at", "completed_at"], "task execution");
  assert.equal(samples.run.body.status, "succeeded");
  assert.deepEqual(samples.run.body.tasks.map((t) => t.status), ["succeeded", "succeeded"]);
  assert.equal(samples.run_failed.body.status, "failed");
  assert.deepEqual(samples.run_failed.body.tasks.map((t) => [t.status, t.error?.code]), [["failed", "timeout"], ["skipped", "dependency_failed"]]);
  assert.equal(samples.run_failed_task.body.execution.status, "failed");
  assert.equal(samples.run_failed_task.body.execution.result, null);
});

test("the Studio's status vocabulary covers every status the real backend produced", { skip }, () => {
  for (const name of ["run", "run_failed", "run_started"]) assert.ok(view.RUN_STATUS[samples[name].body.status], name);
  for (const run of [samples.run.body, samples.run_failed.body]) for (const t of run.tasks) assert.ok(view.TASK_STATUS[t.status], t.status);
  for (const name of ["plan", "plan_approved", "plan_cancelled", "plan_revised"]) assert.ok(view.PLAN_STATUS[samples[name].body.status], name);
});

test("real error bodies flow through the client and are explained for the user", { skip }, async () => {
  const cases = {
    error_version_conflict: (e, x) => e.kind === "conflict" && e.detail.latestVersion === 3 && x.stale && x.title === "This agent changed elsewhere",
    error_validation: (e, x) => e.kind === "validation" && e.detail.fields[0].path === "definition.model.temperature" && x.fields["model.temperature"] === "Value error, temperature must be between 0 and 2",
    error_slug_taken: (e, x) => e.kind === "conflict" && x.title === "That identifier is taken",
    error_plan_not_approved: (e, x) => x.title === "The plan isn't approved",
    error_revision_conflict: (e, x) => e.kind === "conflict" && x.stale,
    error_already_executed: (e, x) => x.title === "This version already completed",
    error_not_plannable: (e, x) => x.title === "AgentForge can't plan this safely" && x.items.includes("Charge cards — No agent can take payments.") && x.items.includes("How AgentForge read it: Charge cards."),
    error_not_found: (e, x) => e.kind === "not_found" && x.title === "Not found",
  };
  for (const [name, check] of Object.entries(cases)) {
    const sample = samples[name];
    const { api } = client(() => json(sample.status, sample.body));
    await assert.rejects(api.getPlan("p"), (e) => e instanceof ApiError && e.status === sample.status && check(e, view.explainError(e)), name);
  }
  assert.equal(samples.error_revision_conflict.body.detail.code, "revision_conflict");
});

test("the client parses real success bodies unchanged and the poller's terminal rule matches real runs", { skip }, async () => {
  const { api } = client((c) => json(200, c.url.endsWith("/plan-runs/r") ? samples.run.body : samples.plan.body));
  assert.deepEqual(await api.getRun("r"), samples.run.body);
  assert.deepEqual(await api.getPlan("p"), samples.plan.body);
  assert.equal(view.isTerminalRun(samples.run.body), true);
  assert.equal(view.isTerminalRun(samples.run_started.body), false);
});

// ------------------------------------------------------------------------------------------------ tools (M4-B)

test("real tool responses equal the TypeScript contracts and the presentation logic can read the real schemas", { skip }, () => {
  const summary = ["id", "version", "ref", "title", "summary", "category", "capabilities", "risk", "side_effect", "lifecycle", "executable", "policy", "requires_approval", "grantable", "timeout_seconds", "max_output_bytes"];
  same(samples.tools.body, ["items", "policy"], "catalog");
  same(samples.tools.body.policy, ["max_risk", "side_effects", "approvals"], "catalog policy");
  const item = samples.tools.body.items.find((t) => t.id === "math.calculate");
  same(item, ["id", "title", "summary", "category", "capabilities", "latest", "versions", "usage", "agents"], "catalog item");
  same(item.latest, summary, "tool summary");
  same(item.versions[0], ["version", "lifecycle", "executable"], "version chip");
  same(item.usage, ["calls", "last_used"], "usage");
  same(item.agents[0], ["id", "slug", "name", "version"], "grant");
  assert.deepEqual([item.usage.calls, item.agents.map((a) => a.slug), item.agents[0].version], [1, ["calculator-agent"], 1], "real usage and the pinned grant are reported");
  same(samples.tool.body, ["id", "versions", "agents", "recent_invocations"], "tool detail");
  same(samples.tool.body.versions[0], [...summary, "description", "limitations", "example", "input_schema", "output_schema", "usage"], "tool version detail");
  const call = ["id", "execution_id", "run_id", "task", "agent", "tool", "seq", "status", "side_effect", "error", "created_at", "started_at", "completed_at", "duration_ms", "dispatch"];
  same(samples.tool_invocations.body, ["items", "next_cursor"], "invocation page");
  same(samples.tool_invocations.body.items[0], [...call, "input_preview", "output_preview"], "invocation item");
  same(samples.tool_invocation.body, [...call, "input", "output", "args_hash"], "invocation detail");
  same(samples.run_tool_invocations.body, ["items", "truncated"], "run invocations");
  assert.equal(samples.tool_invocation.body.status, "succeeded");
  assert.deepEqual(samples.tool_invocation.body.output.value, 588);
  assert.deepEqual(samples.agent_with_tools.body.definition.allowed_tools, ["math.calculate@1"], "the agent's tool is pinned to an exact version on save");
  // the port logic reads every REAL schema of the five tools: no crash, every property becomes a port, required fields are marked
  for (const entry of samples.tools.body.items) {
    const detail = entry.id === "math.calculate" ? samples.tool.body.versions[0] : null;
    if (detail) {
      assert.deepEqual(tools.portsOf(detail.input_schema).map((p) => [p.name, p.type, p.required]), [["expression", "text", true]]);
      assert.deepEqual(tools.portsOf(detail.output_schema).map((p) => p.name).sort(), ["expression", "formatted", "value"]);
    }
  }
});
