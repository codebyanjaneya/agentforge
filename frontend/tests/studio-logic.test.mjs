// M3-C: the Studio's logic layer (API client methods, error details, polling, view rules, forms and workflows).
// In-memory fakes only: no network, no backend process.

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, createApiClient, createTokenManager, sanitizeErrorBody } from "../lib/api-client.ts";
import * as forms from "../lib/studio/forms.ts";
import * as flows from "../lib/studio/flows.ts";
import { createPoller } from "../lib/studio/poller.ts";
import * as view from "../lib/studio/view.ts";
import { clock, fakeFetch, json, tokenEndpoint } from "./m1h-helpers.mjs";

const BASE = "https://api.example.com";

function setup(backend = () => json(200, {})) {
  const now = clock();
  const tokens = createTokenManager({ fetchImpl: fakeFetch(tokenEndpoint(now)), now });
  const backendFetch = fakeFetch(backend);
  return { client: createApiClient({ baseUrl: BASE, tokens, fetchImpl: backendFetch }), backendFetch };
}

const call = (backendFetch, i = 0) => {
  const c = backendFetch.calls[i];
  const url = new URL(c.url);
  return { method: c.init.method, path: url.pathname, query: Object.fromEntries(url.searchParams), body: c.init.body === undefined ? undefined : JSON.parse(c.init.body) };
};

// ------------------------------------------------------------------------------------------------ client

test("every Studio client method sends the documented method, path, query and body, and never an ownership identity", async () => {
  const { client, backendFetch } = setup(() => json(200, { items: [], next_cursor: null }));
  const def = forms.definitionFromForm({ ...forms.blankForm(), name: "Writer" });
  await client.listTemplates();
  await client.getTemplate("sales-outreach");
  await client.instantiateTemplate("sales-outreach", { slug: "my-sales", name: "My Sales" });
  await client.instantiateTemplate("sales-outreach");
  await client.listAgents();
  await client.listAgents("all");
  await client.getAgent("a/b");
  await client.createAgent({ slug: "writer", definition: def, change_note: "first" });
  await client.updateAgent("id1", { base_version: 2, definition: def });
  await client.archiveAgent("id1");
  await client.unarchiveAgent("id1");
  await client.listVersions("id1");
  await client.getVersion("id1", 3);
  await client.rollbackAgent("id1", { to_version: 1, base_version: 3 });
  await client.createPlan("Launch our product to early customers");
  await client.listPlans({ status: "approved", limit: 10, cursor: "v1.a.b" });
  await client.listPlans();
  await client.getPlan("p1");
  await client.listRevisions("p1");
  await client.getRevision("p1", 2);
  await client.validatePlan("p1");
  await client.revisePlan("p1", { base_revision: 1, tasks: [] });
  await client.approvePlan("p1", 4);
  await client.cancelPlan("p1");
  await client.startRun("p1");
  await client.listRuns("p1");
  await client.getRun("r1");
  await client.getTaskRun("r1", "t-1");
  await client.cancelRun("r1");
  await client.retryRun("r1");
  await client.retryRun("r1", { acknowledge_uncertain: ["t-1"] });
  await client.getRunEvents("r1");
  await client.getRunEvents("r1", { after: 7, limit: 50 });
  const seen = backendFetch.calls.map((_, i) => call(backendFetch, i));
  const sig = seen.map((c) => `${c.method} ${c.path}${Object.keys(c.query).length ? "?" + new URLSearchParams(c.query) : ""}`);
  assert.deepEqual(sig, [
    "GET /agent-templates", "GET /agent-templates/sales-outreach", "POST /agent-templates/sales-outreach/instantiate", "POST /agent-templates/sales-outreach/instantiate",
    "GET /agents?status=active", "GET /agents?status=all", "GET /agents/a%2Fb", "POST /agents", "PATCH /agents/id1", "POST /agents/id1/archive",
    "POST /agents/id1/unarchive", "GET /agents/id1/versions", "GET /agents/id1/versions/3", "POST /agents/id1/rollback", "POST /plans",
    "GET /plans?status=approved&limit=10&cursor=v1.a.b", "GET /plans", "GET /plans/p1", "GET /plans/p1/revisions", "GET /plans/p1/revisions/2",
    "POST /plans/p1/validate", "POST /plans/p1/revisions", "POST /plans/p1/approve", "POST /plans/p1/cancel", "POST /plans/p1/runs", "GET /plans/p1/runs",
    "GET /plan-runs/r1", "GET /plan-runs/r1/tasks/t-1", "POST /plan-runs/r1/cancel",
    "POST /plan-runs/r1/retry", "POST /plan-runs/r1/retry", "GET /plan-runs/r1/events", "GET /plan-runs/r1/events?after=7&limit=50",
  ]);
  assert.deepEqual(seen[29].body, { acknowledge_uncertain: [] }, "a retry sends only the acknowledgements: no revision, user or task list");
  assert.deepEqual(seen[30].body, { acknowledge_uncertain: ["t-1"] });
  assert.deepEqual(seen[2].body, { slug: "my-sales", name: "My Sales" });
  assert.deepEqual(seen[3].body, {}, "instantiate without options sends an empty body");
  assert.deepEqual(seen[13].body, { to_version: 1, base_version: 3 });
  assert.deepEqual(seen[22].body, { revision: 4 });
  assert.equal(seen[24].body, undefined, "starting a run sends nothing: the revision comes from the stored approval");
  for (const c of backendFetch.calls) {
    assert.ok(!/user_id|workspace|email|owner/i.test(c.init.body ?? ""), "no ownership identity is ever sent");
    assert.equal(c.init.credentials, "omit");
  }
});

test("listPlans validates the page shape and keeps the cursor", async () => {
  const ok = setup(() => json(200, { items: [{ id: "1" }], next_cursor: "v1.x.y" }));
  assert.deepEqual(await ok.client.listPlans(), { items: [{ id: "1" }], next_cursor: "v1.x.y" });
  for (const body of [{}, { items: [], next_cursor: 4 }, { items: "x", next_cursor: null }]) {
    await assert.rejects(setup(() => json(200, body)).client.listPlans(), (e) => e.kind === "server");
  }
});

test("error responses keep only whitelisted, bounded facts: codes, field messages and fixed backend texts", async () => {
  const cases = [
    [409, { detail: { code: "version_conflict", latest_version: 5, secret: "gsk_abcdefghijklmnopqrstuvwxyz" } }, (e) => e.kind === "conflict" && e.detail.code === "version_conflict" && e.detail.latestVersion === 5 && !JSON.stringify(e.detail).includes("gsk_")],
    [409, { detail: { code: "revision_conflict", latest_revision: 3 } }, (e) => e.detail.latestRevision === 3],
    [422, { detail: [{ loc: ["body", "definition", "name"], msg: "must be 2-80 characters", type: "value_error", input: "SECRET" }] }, (e) => e.kind === "validation" && e.detail.fields[0].path === "definition.name" && !JSON.stringify(e).includes("SECRET")],
    [422, { detail: { code: "plan_rejected", errors: [{ code: "cycle", task: "a", message: "Tasks depend on each other." }, { nope: 1 }, "x"] } }, (e) => e.detail.errors.length === 1 && e.detail.errors[0].code === "cycle"],
    [422, { detail: { code: "objective_not_plannable", interpretation: "Charge cards.", unsupported: [{ requirement: "Charge cards", reason: "No agent can take payments." }] } }, (e) => e.detail.unsupported[0].requirement === "Charge cards" && e.detail.interpretation === "Charge cards."],
    [500, { detail: "SECRET sql text" }, (e) => e.kind === "server" && e.detail === undefined],
    [500, "not even an object", (e) => e.detail === undefined],
    [429, { detail: { code: "BAD CODE!", message: "x".repeat(900) } }, (e) => e.kind === "rate_limited" && e.detail.code === undefined && e.detail.message.length === 400],
  ];
  for (const [status, body, check] of cases) {
    const { client } = setup(() => json(status, body));
    await assert.rejects(client.getPlan("p"), (e) => e instanceof ApiError && e.status === status && check(e) && !e.message.includes("SECRET"), JSON.stringify(body).slice(0, 60));
  }
  assert.equal(sanitizeErrorBody(null), undefined);
  assert.equal(setup(() => new Response("<html>", { status: 502 })).client === undefined, false);
  await assert.rejects(setup(() => new Response("<html>", { status: 502 })).client.getPlan("p"), (e) => e.kind === "server" && e.detail === undefined);
});

// ------------------------------------------------------------------------------------------------ poller

function timers() {
  const queue = [];
  return {
    setTimer: (fn, ms) => {
      const handle = { fn, ms, live: true };
      queue.push(handle);
      return handle;
    },
    clearTimer: (h) => {
      if (h) h.live = false;
    },
    pending: () => queue.filter((h) => h.live),
    async fire() {
      const h = queue.find((x) => x.live);
      assert.ok(h, "a timer is scheduled");
      h.live = false;
      h.fn();
      await new Promise((r) => setImmediate(r));
    },
  };
}
const flush = () => new Promise((r) => setImmediate(r));

test("poller: one loop only, no overlapping requests, stops at a terminal state", async () => {
  const t = timers();
  const seen = [];
  let calls = 0;
  const release = [];
  const poller = createPoller({
    fetch: () => new Promise((resolve) => { calls += 1; release.push(resolve); }),
    onData: (v) => seen.push(v),
    isTerminal: (v) => v === "done",
    setTimer: t.setTimer,
    clearTimer: t.clearTimer,
    isVisible: () => true,
  });
  poller.start();
  poller.start();
  poller.start();
  poller.refresh(); // a refresh while a request is pending must not start another
  await flush();
  assert.equal(calls, 1);
  assert.equal(poller.running, true);
  release[0]("running");
  await flush();
  assert.deepEqual(seen, ["running"]);
  assert.equal(t.pending().length, 1, "exactly one timer");
  await t.fire();
  assert.equal(calls, 2);
  release[1]("done");
  await flush();
  assert.equal(poller.running, false);
  assert.equal(t.pending().length, 0, "nothing is scheduled after the terminal state");
  assert.equal(calls, 2);
});

test("poller: pauses while hidden, backs off on transient errors and stops on errors retrying cannot fix", async () => {
  const t = timers();
  let visible = false;
  let calls = 0;
  const errors = [];
  const results = [new ApiError("server", 500), new ApiError("network"), "ok", new ApiError("auth", 401)];
  const poller = createPoller({
    fetch: async () => {
      calls += 1;
      const r = results.shift();
      if (r instanceof Error) throw r;
      return r;
    },
    onData: () => {},
    onError: (e, info) => errors.push([e.kind, info.consecutive, info.willRetry]),
    isTerminal: () => false,
    intervalMs: 1000,
    maxIntervalMs: 8000,
    setTimer: t.setTimer,
    clearTimer: t.clearTimer,
    isVisible: () => visible,
  });
  poller.start();
  await flush();
  assert.equal(calls, 0, "no request while the tab is hidden");
  visible = true;
  await t.fire();
  assert.equal(calls, 1);
  assert.deepEqual(t.pending().map((h) => h.ms), [2000], "first failure backs off");
  await t.fire();
  assert.deepEqual(t.pending().map((h) => h.ms), [4000]);
  await t.fire(); // success resets the backoff
  assert.deepEqual(t.pending().map((h) => h.ms), [1000]);
  await t.fire(); // auth error: fatal
  assert.deepEqual(errors, [["server", 1, true], ["network", 2, true], ["auth", 1, false]]);
  assert.equal(poller.running, false);
  assert.equal(t.pending().length, 0);
});

test("poller: stop() aborts the request and a late answer is never delivered; restart works", async () => {
  const t = timers();
  const delivered = [];
  let signal;
  let resolveLate;
  const poller = createPoller({
    fetch: (s) => new Promise((resolve) => { signal = s; resolveLate = resolve; }),
    onData: (v) => delivered.push(v),
    isTerminal: () => false,
    setTimer: t.setTimer,
    clearTimer: t.clearTimer,
    isVisible: () => true,
  });
  poller.start();
  await flush();
  poller.stop();
  assert.equal(signal.aborted, true);
  resolveLate("late");
  await flush();
  assert.deepEqual(delivered, []);
  assert.equal(t.pending().length, 0);
  poller.start();
  await flush();
  resolveLate("fresh");
  await flush();
  assert.deepEqual(delivered, ["fresh"]);
  poller.stop();
});

test("poller: gives up after too many consecutive transient errors", async () => {
  const t = timers();
  let calls = 0;
  const poller = createPoller({
    fetch: async () => { calls += 1; throw new ApiError("server", 500); },
    onData: () => {},
    isTerminal: () => false,
    maxConsecutiveErrors: 3,
    setTimer: t.setTimer,
    clearTimer: t.clearTimer,
    isVisible: () => true,
  });
  poller.start();
  await flush();
  await t.fire();
  await t.fire();
  assert.equal(calls, 3);
  assert.equal(poller.running, false);
});

// ------------------------------------------------------------------------------------------------ view rules

const task = (id, level, status, extra = {}) => ({
  id, position: level * 10 + id.charCodeAt(0) % 10, level, title: `Task ${id}`, status, depends_on: [], blocked_by: [], execution_id: null,
  agent: { source: "builtin", key: "research", name: "Research", version: null, version_id: null }, error: null, created_at: "2026-01-01T00:00:00", started_at: null, completed_at: null, ...extra,
});

test("stages group independent tasks by level in order", () => {
  const stages = view.groupStages([task("c", 2, "pending"), task("a", 0, "succeeded"), task("b", 0, "succeeded"), task("d", 1, "running")]);
  assert.deepEqual(stages.map((s) => [s.index, s.level, s.tasks.map((t) => t.id), s.parallel]), [[1, 0, ["a", "b"], true], [2, 1, ["d"], false], [3, 2, ["c"], false]]);
});

test("run summaries and explanations use only real states (partial success is explained)", () => {
  const tasks = [
    task("a", 0, "succeeded"),
    task("b", 1, "failed", { depends_on: ["a"], error: { code: "timeout", message: "The agent took too long to answer." } }),
    task("c", 2, "skipped", { depends_on: ["b"], error: { code: "dependency_failed", message: "A task this one depends on did not succeed, so it was not run." } }),
    task("d", 1, "pending", { depends_on: ["b"], blocked_by: ["b"] }),
  ];
  const counts = { pending: 1, running: 0, succeeded: 1, failed: 1, skipped: 1, cancelled: 0, total: 4 };
  const s = view.summarizeRun({ status: "partial", counts });
  assert.equal(s.headline, "1 of 4 tasks succeeded");
  assert.deepEqual(s.breakdown.map((b) => [b.label, b.count]), [["Succeeded", 1], ["Failed", 1], ["Skipped", 1], ["Waiting", 1]]);
  assert.match(view.explainTask(tasks[2], tasks), /Blocked by: Task b/);
  assert.equal(view.explainTask(tasks[1], tasks), "The agent took too long to answer.");
  assert.match(view.explainTask(tasks[3], tasks), /Waiting for: Task b/);
  assert.equal(view.explainTask(tasks[0], tasks), null);
  assert.deepEqual(view.attention({ tasks }).map((t) => t.id), ["b", "c"], "failed first, then skipped");
  assert.equal(view.summarizeRun({ status: "running", counts }).headline, "1 of 4 tasks finished");
  assert.equal(view.summarizeRun({ status: "succeeded", counts: { ...counts, total: 3 } }).headline, "All 3 tasks succeeded");
  for (const status of ["running", "succeeded", "partial", "failed", "cancelled"]) assert.ok(view.RUN_STATUS[status].label);
  for (const status of ["pending", "running", "succeeded", "failed", "skipped", "cancelled"]) assert.ok(view.TASK_STATUS[status].label);
  assert.equal(view.isTerminalRun({ status: "running" }), false);
  for (const status of ["succeeded", "partial", "failed", "cancelled"]) assert.equal(view.isTerminalRun({ status }), true);
});

test("plan lifecycle: approval never enables or implies execution; start is its own, guarded action", () => {
  const a = (status, latest) => view.planActions({ status, latest_run: latest ? { status: latest } : null });
  assert.deepEqual([a("draft").canApprove, a("draft").canStartRun], [true, false]);
  assert.match(a("draft").startBlockedReason, /does not start any work/);
  assert.deepEqual([a("approved").canApprove, a("approved").canStartRun, a("approved").startBlockedReason], [false, true, null]);
  assert.equal(a("approved", "running").canStartRun, false);
  assert.match(a("approved", "succeeded").startBlockedReason, /Revise the plan/);
  assert.equal(a("approved", "failed").canStartRun, true, "a failed attempt can be started again, explicitly");
  assert.equal(a("approved", "cancelled").canStartRun, true);
  const cancelled = a("cancelled");
  assert.deepEqual([cancelled.canRevise, cancelled.canValidate, cancelled.canApprove, cancelled.canStartRun, cancelled.canCancel], [false, false, false, false, false]);
});

test("errors are explained without leaking raw text, and field errors map to form fields", () => {
  const field = view.explainError(new ApiError("validation", 422, { fields: [{ path: "definition.model.temperature", message: "temperature must be between 0 and 2" }] }));
  assert.equal(field.fields["model.temperature"], "temperature must be between 0 and 2");
  const plan = view.explainError(new ApiError("validation", 422, { code: "plan_rejected", errors: [{ code: "agent_unavailable", task: "b", message: "Agent is unavailable." }] }));
  assert.equal(plan.title, "The plan didn't pass validation");
  assert.deepEqual(plan.items, ["b: Agent is unavailable."]);
  const unplannable = view.explainError(new ApiError("validation", 422, { code: "objective_not_plannable", interpretation: "Charge cards.", unsupported: [{ requirement: "Charge cards", reason: "No payments." }] }));
  assert.deepEqual(unplannable.items, ["How AgentForge read it: Charge cards.", "Charge cards — No payments."]);
  assert.equal(view.explainError(new ApiError("conflict", 409, { code: "revision_conflict" })).stale, true);
  assert.equal(view.explainError(new ApiError("auth", 401)).needsSignIn, true);
  assert.equal(view.explainError(new ApiError("forbidden", 403)).title, "No access");
  assert.equal(view.explainError(new ApiError("not_found", 404, { code: "not_found" })).title, "Not found");
  assert.equal(view.explainError(new ApiError("rate_limited", 429, { code: "busy" })).title, "AgentForge is busy");
  assert.equal(view.explainError(new ApiError("server", 504, { code: "timeout" })).title, "The model took too long");
  assert.equal(view.explainError(new ApiError("network")).title, "Can't reach AgentForge");
  assert.equal(view.explainError(new Error("raw SECRET")).message, "Something went wrong. Please try again.");
  assert.equal(view.explainError(new Error("x"), "Custom").title, "Custom");
});

test("times: backend naive UTC is read as UTC", () => {
  assert.equal(view.asUtc("2026-01-01T10:00:00").toISOString(), "2026-01-01T10:00:00.000Z");
  assert.equal(view.asUtc("2026-01-01T10:00:00+00:00").toISOString(), "2026-01-01T10:00:00.000Z");
  const now = Date.parse("2026-01-01T10:10:00Z");
  assert.equal(view.relativeTime("2026-01-01T10:09:50", now), "just now");
  assert.equal(view.relativeTime("2026-01-01T09:50:00", now), "20 min ago");
  assert.equal(view.relativeTime(null, now), "—");
  assert.equal(view.duration("2026-01-01T10:00:00", "2026-01-01T10:01:05"), "1m 05s");
  assert.equal(view.duration("2026-01-01T10:00:00", "2026-01-01T10:00:09"), "9s");
  assert.equal(view.duration(null, null), null);
});

// ------------------------------------------------------------------------------------------------ forms

test("agent form: build, validate and preserve; tools are never selectable", () => {
  const blank = forms.blankForm();
  const errors = forms.validateForm(blank);
  assert.deepEqual(Object.keys(errors).sort(), ["description", "instructions", "name", "purpose", "role"]);
  const good = { ...blank, name: "Release Writer", description: "Writes release notes for customers.", role: "Product marketer", purpose: "Explain what changed and why.", instructions: "You write clear release notes from a changelog.", capabilities: "Release Notes, writing", max_tokens: "512" };
  assert.deepEqual(forms.validateForm(good), {});
  const def = forms.definitionFromForm(good, { allowed_tools: [], metadata: { team: "growth" } });
  assert.deepEqual(def.capabilities, ["release notes", "writing"]);
  assert.deepEqual(def.model, { provider: "groq", name: forms.DEFAULT_MODEL, temperature: 0.7, max_tokens: 512 });
  assert.deepEqual([def.allowed_tools, def.metadata], [[], { team: "growth" }]);
  assert.equal(forms.definitionFromForm(blank).model.max_tokens, null);
  assert.deepEqual(forms.formFromDefinition(def), { ...good, capabilities: "release notes, writing", name: "Release Writer" });
  for (const [patch, key] of [[{ temperature: "3" }, "model.temperature"], [{ temperature: "" }, "model.temperature"], [{ max_tokens: "0" }, "model.max_tokens"], [{ max_tokens: "1.5" }, "model.max_tokens"], [{ model_name: "bad name!" }, "model.name"],
    [{ capabilities: "ok, ok" }, "capabilities"], [{ capabilities: "A!, b" }, "capabilities"], [{ name: "x" }, "name"], [{ instructions: "short" }, "instructions"]]) {
    assert.ok(key in forms.validateForm({ ...good, ...patch }), key);
  }
  assert.ok("capabilities" in forms.validateForm({ ...good, capabilities: Array.from({ length: 13 }, (_, i) => `cap ${i}`).join(",") }));
  assert.equal(forms.validateSlug("release-writer"), null);
  for (const bad of ["ab", "Release", "ceo", "a--b", "1abc", "-abc", "a".repeat(41), "new"]) assert.ok(forms.validateSlug(bad), bad);
  assert.equal(forms.suggestSlug("Release Notes Writer!"), "release-notes-writer");
  assert.equal(forms.validateSlug(forms.suggestSlug("2nd Sales Agent")), null);
  assert.equal(forms.suggestSlug("???"), "");
});

test("plan editor: payload carries the base revision, dependencies stay consistent and invalid plans are caught early", () => {
  const rev = { tasks: [
    { id: "a", title: "Research", description: "Research the market properly.", expected_output: "A short summary", rationale: "Fits research.", dependency_reason: null, depends_on: [], complexity: "low", priority: "normal", agent: { key: "research" } },
    { id: "b", title: "Write", description: "Write the announcement post.", expected_output: "A post draft", rationale: "Fits marketing.", dependency_reason: "needs research", depends_on: ["a"], complexity: "medium", priority: "high", agent: { key: "marketing" } },
  ] };
  let tasks = forms.editorFromRevision(rev);
  assert.deepEqual(forms.validateEditor(tasks), {});
  const payload = forms.payloadFromEditor(tasks, 3, "A narrower reading of the objective.");
  assert.equal(payload.base_revision, 3);
  assert.equal(payload.interpretation, "A narrower reading of the objective.");
  assert.deepEqual(payload.tasks.map((t) => [t.id, t.agent, t.depends_on]), [["a", "research", []], ["b", "marketing", ["a"]]]);
  assert.ok(!JSON.stringify(payload).match(/user|workspace|owner|version_id/i), "no identity or version pin is sent: the backend resolves and pins agents");
  assert.equal("interpretation" in forms.payloadFromEditor(tasks, 3, " "), false);
  const fresh = forms.blankTask(tasks, "sales");
  assert.equal(fresh.id, "task-3");
  tasks = [...tasks, { ...fresh, depends_on: ["b"] }];
  assert.ok(forms.validateEditor(tasks)["task-3.title"]);
  assert.equal(forms.nextTaskId(tasks), "task-4");
  const removed = forms.removeTask(tasks, "a");
  assert.deepEqual(removed.map((t) => [t.id, t.depends_on]), [["b", []], ["task-3", ["b"]]], "a removed task disappears from every dependency list");
  assert.ok(forms.validateEditor([])._plan);
  assert.ok(forms.validateEditor([{ ...tasks[0], depends_on: ["a"] }])["a.depends_on"]);
  assert.ok(forms.validateEditor([{ ...tasks[0], depends_on: ["ghost"] }])["a.depends_on"]);
  assert.ok(forms.validateEditor([tasks[0], { ...tasks[0] }])["a.id"]);
  assert.ok(forms.validateEditor([{ ...tasks[0], id: "Bad Id" }])["Bad Id.id"]);
  assert.ok(forms.validateEditor(Array.from({ length: 13 }, (_, i) => ({ ...tasks[0], id: `t${i}` })))._plan);
});

// ------------------------------------------------------------------------------------------------ workflows

function recorder() {
  const calls = [];
  const make = (name, result = {}) => async (...args) => { calls.push([name, ...args]); return result; };
  return {
    calls,
    api: {
      createPlan: make("createPlan", { id: "p1", status: "draft", latest_revision: 1 }), revisePlan: make("revisePlan", { id: "p1", status: "draft", latest_revision: 2 }),
      approvePlan: make("approvePlan", { id: "p1", status: "approved", latest_revision: 2 }), validatePlan: make("validatePlan"), cancelPlan: make("cancelPlan"),
      startRun: make("startRun", { id: "r1" }), updateAgent: make("updateAgent"), createAgent: make("createAgent"), rollbackAgent: make("rollbackAgent"),
      instantiateTemplate: make("instantiateTemplate"),
    },
  };
}

test("creating, revising and approving a plan NEVER starts a run; starting is separate and guarded", async () => {
  const r = recorder();
  const plan = await flows.createPlanFlow(r.api, "  Launch our product to early customers  ");
  assert.deepEqual(r.calls[0], ["createPlan", "Launch our product to early customers"]);
  await flows.revisePlanFlow(r.api, { id: "p1", latest_revision: 1 }, forms.editorFromRevision({ tasks: [] }), null);
  assert.deepEqual(r.calls[1].slice(0, 2), ["revisePlan", "p1"]);
  assert.equal(r.calls[1][2].base_revision, 1);
  await flows.approvePlanFlow(r.api, { id: "p1", status: "draft", latest_revision: 2 });
  assert.deepEqual(r.calls[2], ["approvePlan", "p1", 2], "the approved revision is the one the user saw (the latest)");
  assert.deepEqual(r.calls.map((c) => c[0]), ["createPlan", "revisePlan", "approvePlan"], "no startRun call happened");
  await assert.rejects(flows.startRunFlow(r.api, { id: "p1", status: "draft" }), flows.FlowRefused);
  await assert.rejects(flows.startRunFlow(r.api, { id: "p1", status: "cancelled" }), flows.FlowRefused);
  assert.equal(r.calls.length, 3);
  await flows.startRunFlow(r.api, { id: "p1", status: "approved" });
  assert.deepEqual(r.calls.at(-1), ["startRun", "p1"]);
  await assert.rejects(flows.approvePlanFlow(r.api, { id: "p1", status: "approved", latest_revision: 2 }), flows.FlowRefused);
  await assert.rejects(flows.createPlanFlow(r.api, "short"), flows.FlowRefused);
  assert.equal(plan.id, "p1");
});

test("agent save and rollback use the version the user was looking at and keep stored tools and metadata", async () => {
  const r = recorder();
  const agent = { id: "a1", latest_version: 4, definition: { allowed_tools: [], metadata: { team: "x" } } };
  const form = { ...forms.blankForm(), name: "Writer", description: "Writes things well.", role: "Writer", purpose: "Write clearly.", instructions: "You write clearly and briefly." };
  await flows.saveAgentFlow(r.api, agent, form, " tweaked ");
  const [, id, body] = r.calls[0];
  assert.equal(id, "a1");
  assert.equal(body.base_version, 4);
  assert.equal(body.change_note, "tweaked");
  assert.deepEqual(body.definition.metadata, { team: "x" });
  await flows.saveAgentFlow(r.api, agent, form, "");
  assert.ok(!("change_note" in r.calls[1][2]));
  await flows.createAgentFlow(r.api, "writer", form, "");
  assert.equal(r.calls[2][1].slug, "writer");
  await flows.rollbackFlow(r.api, agent, 2);
  assert.deepEqual(r.calls[3], ["rollbackAgent", "a1", { to_version: 2, base_version: 4 }]);
});
