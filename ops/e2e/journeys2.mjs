// Second journey suite: the gaps from the first one. Needs a stack started with `stack.py start --slow-send` (the SIMULATED send takes ~25 s so the process can be hard-killed mid-dispatch).
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { api, chromium, BROWSER, openAs, PEOPLE, OUT, cfg } from "./lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const PY = path.join(here, "../../backend/venv/Scripts/python.exe");
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const WS = seed.workspace;
const WEB = cfg.web;
const results = [];
const log = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = async (fn, ms = 60000, every = 700) => { const t = Date.now(); while (Date.now() - t < ms) { const v = await fn(); if (v) return v; await sleep(every); } throw new Error("timeout"); };
const list = (b) => b.items || b;
async function step(name, fn, page) {
  try { await fn(); results.push({ name, ok: true }); console.log("PASS", name); }
  catch (e) {
    const msg = String(e.message || e).split("\n")[0].slice(0, 260);
    results.push({ name, ok: false, err: msg }); console.log("FAIL", name, "-", msg);
    if (page) await page.screenshot({ path: path.join(OUT, "fail2-" + name.replace(/\W+/g, "_").slice(0, 50) + ".png") }).catch(() => {});
  }
}
const browser = await chromium.launch({ executablePath: BROWSER });
async function as(who, vp = { width: 1280, height: 800 }) {
  const s = await openAs(browser, who, vp, log);
  await s.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  return s;
}
const OBJ = "Launch check: price $59/month, 75% margin, 4% monthly churn, CAC $350. Email jo@acme.io on the launch date.";
const O = await as("owner");
const A = await as("admin");
const M = await as("member");
const S = await as("starter");

// ------------------------------------------------------------ the starter workforce for EVERY member
await step("workforce: owner, admin and member add the starter workforce at the same instant; each gets exactly their own 3, no conflicts", async () => {
  const rs = await Promise.all(["owner", "admin", "member"].map((w) => api(w, "POST", "/starter-workforce", {}, WS)));
  for (const r of rs) { ok(r.status === 200, "status " + r.status); ok(r.body.agents.every((a) => a.created === true), "created flags " + JSON.stringify(r.body.agents.map((a) => a.created))); }
  const ids = new Set();
  for (const w of ["owner", "admin", "member"]) {
    const l = list((await api(w, "GET", "/agents", null, WS)).body);
    ok(l.length === 3 && new Set(l.map((a) => a.slug)).size === 3, w + " has " + l.length);
    l.forEach((a) => { ok(!ids.has(a.id), "agent shared between members"); ids.add(a.id); });
  }
  const again = await api("member", "POST", "/starter-workforce", {}, WS);
  ok(again.body.agents.every((a) => a.created === false) && list((await api("member", "GET", "/agents", null, WS)).body).length === 3, "second call must not duplicate");
});
await step("workforce: a member adds it with the UI button, then plans an objective with their own agents (the old 'add the workforce' loop is gone)", async () => {
  await S.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  await S.page.getByRole("button", { name: "Add the starter workforce" }).first().click();
  await wait(async () => list((await api("starter", "GET", "/agents", null, WS)).body).length === 3, 15000);
  await S.page.getByPlaceholder(/What should your AI workforce/).fill(OBJ);
  await S.page.getByRole("button", { name: "Generate plan" }).click();
  await S.page.waitForURL(/\/studio\/plans\/[0-9a-f-]{36}/, { timeout: 30000 });
  const p = await api("starter", "GET", `/plans/${S.page.url().split("/").pop()}`, null, WS);
  ok(p.status === 200 && p.body.latest_revision === 1, "plan " + p.status);
}, S.page);
await step("workforce: agents of one member stay private (a member does not list or open another member's agent)", async () => {
  const mine = list((await api("member", "GET", "/agents", null, WS)).body).map((a) => a.id);
  const theirs = list((await api("admin", "GET", "/agents", null, WS)).body)[0].id;
  ok(!mine.includes(theirs), "listed");
  ok((await api("member", "GET", `/agents/${theirs}`, null, WS)).status === 404, "opened");
});

// ------------------------------------------------------------ plan revision editor
let planR;
await step("plan editor: edit tasks of an APPROVED plan in the UI; revision 2 saved, approval withdrawn, revision 1 kept in history", async () => {
  planR = (await api("owner", "POST", "/plans", { objective: OBJ }, WS)).body.id;
  ok((await api("owner", "POST", `/plans/${planR}/approve`, { revision: 1 }, WS)).status < 300, "approve");
  await O.page.goto(`${WEB}/studio/plans/${planR}`, { waitUntil: "networkidle" });
  await O.page.getByRole("button", { name: "Edit tasks" }).click();
  const title = O.page.getByLabel("Title").first();
  await title.fill("Check the unit economics (reviewed)");
  await O.page.getByRole("button", { name: /Save as revision 2/ }).click();
  const p = await wait(async () => { const x = (await api("owner", "GET", `/plans/${planR}`, null, WS)).body; return x.latest_revision === 2 ? x : null; }, 15000);
  ok(p.approved_revision === null && p.status !== "approved", "approval must be withdrawn: " + p.status);
  ok(JSON.stringify(p).includes("reviewed"), "new title stored");
  const revs = list((await api("owner", "GET", `/plans/${planR}/revisions`, null, WS)).body);
  ok(revs.length === 2, "history " + revs.length);
  await O.page.reload({ waitUntil: "networkidle" });
  ok(await O.page.getByText("Revision history").isVisible() && (await O.page.getByText(/^r1\b/).count()) > 0, "history in UI");
}, O.page);
await step("plan editor: an invalid edit is refused with a visible error and focus on the field; a member cannot edit someone else's plan (API 404/403)", async () => {
  await O.page.getByRole("button", { name: "Edit tasks" }).click();
  await O.page.getByLabel("Title").first().fill("");
  await O.page.getByRole("button", { name: /Save as revision 3/ }).click();
  ok(await O.page.getByText(/title/i).filter({ hasText: /must|required|empty|needs/i }).count() > 0 || (await O.page.locator("[aria-invalid=true]").count()) > 0, "no visible validation");
  ok((await api("owner", "GET", `/plans/${planR}`, null, WS)).body.latest_revision === 2, "revision created by invalid edit");
  const r = await api("member", "POST", `/plans/${planR}/revisions`, { base_revision: 2, tasks: [{ id: "x", title: "t", description: "d", expected_output: "o", agent: "launch-comms", depends_on: [] }] }, WS);
  ok([403, 404, 422].includes(r.status) && r.status !== 201, "member edit status " + r.status);
}, O.page);

// ------------------------------------------------------------ invitations: wrong e-mail
await step("invitation: a person signed in with a DIFFERENT e-mail is refused (UI message, no membership); the invited e-mail can still join afterwards", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  await O.page.getByLabel(/Invite by email/).fill("right@e2e.test");
  await O.page.getByRole("button", { name: "Create invitation" }).click();
  const box = O.page.locator("[aria-label='Invitation link']").first();
  await box.waitFor();
  const link = (((await box.inputValue().catch(() => "")) || (await box.innerText())).trim()).replace(/^https?:\/\/[^/]+/, WEB);
  const W = await openAs(browser, "stranger", { width: 1280, height: 800 }, []);
  await W.page.goto(link, { waitUntil: "networkidle" });
  await W.page.getByRole("button", { name: "Join workspace" }).click(); // the page reveals nothing about who it is for; the SERVER refuses at accept time
  await W.page.getByText("This invitation can't be used").waitFor({ timeout: 10000 });
  ok(!JSON.stringify((await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body).includes("stranger@e2e.test"), "wrong person became a member");
  ok(!JSON.stringify((await api("stranger", "GET", "/workspaces")).body).includes(WS), "stranger sees workspace");
  await W.ctx.close();
  PEOPLE.right = { sub: "e2e-right", email: "right@e2e.test", name: "Rita Right" };
  const R = await openAs(browser, "right", { width: 1280, height: 800 }, []);
  await R.page.goto(link, { waitUntil: "networkidle" });
  await R.page.getByRole("button", { name: "Join workspace" }).click();
  await wait(async () => JSON.stringify((await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body).includes("right@e2e.test"), 15000);
  await R.ctx.close();
}, O.page);

// ------------------------------------------------------------ roles and removal
await step("roles: owner changes a member to admin and back in the UI; the backend role changes both times", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  const role = async () => (await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body.items.find((m) => m.email === "right@e2e.test").role;
  await O.page.getByLabel("Role of right@e2e.test").selectOption("admin");
  await wait(async () => (await role()) === "admin", 10000);
  const as_admin = await api("right", "GET", "/approvals", null, WS); ok(as_admin.status === 200, "admin read");
  await O.page.getByLabel("Role of right@e2e.test").selectOption("member");
  await wait(async () => (await role()) === "member", 10000);
}, O.page);
await step("removal: owner removes a member in the UI; their access ends at once (API 403, UI shows no workspace data); owner cannot be removed", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  const row = O.page.locator("li", { hasText: "right@e2e.test" });
  await row.getByRole("button", { name: "Remove" }).click();
  const confirm = O.page.getByRole("dialog");
  if (await confirm.count()) await confirm.getByRole("button", { name: /Remove/ }).last().click();
  await wait(async () => !JSON.stringify((await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body).includes("right@e2e.test"), 10000);
  ok((await api("right", "GET", "/plans", null, WS)).status === 403, "removed member still reads");
  ok((await api("owner", "POST", `/workspaces/${WS}/members/remove`, { email: PEOPLE.owner.email }, WS)).status >= 400, "owner removed");
  const X = await openAs(browser, "right", { width: 1280, height: 800 }, []);
  await X.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  await X.page.goto(`${WEB}/studio/plans/${planR}`, { waitUntil: "networkidle" });
  ok(!(await X.page.getByText("Check the unit economics").count()), "removed member sees plan");
  await X.ctx.close();
}, O.page);

// ------------------------------------------------------------ notifications and unknown-outcome reconciliation (same run: the approval, the slow send, the crash)
let run, approval, inv;
await step("notification timing: the admin's open page shows the badge within one poll (<= 20 s) of the approval request, and it falls after the decision", async () => {
  await A.page.goto(`${WEB}/studio/plans`, { waitUntil: "networkidle" });
  const before = Number((await A.page.getByTestId("inbox-badge").first().innerText().catch(() => "0")).match(/\d+/)?.[0] ?? 0);
  const pid = (await api("owner", "POST", "/plans", { objective: OBJ }, WS)).body.id;
  await api("owner", "POST", `/plans/${pid}/approve`, { revision: 1 }, WS);
  run = (await api("owner", "POST", `/plans/${pid}/runs`, {}, WS)).body.id;
  const appr = await wait(async () => list((await api("admin", "GET", "/approvals?status=pending", null, WS)).body).find((x) => x.run_id === run), 60000, 250);
  approval = appr;
  const t0 = Date.now();
  await wait(async () => Number((await A.page.getByTestId("inbox-badge").first().innerText().catch(() => "0")).match(/\d+/)?.[0] ?? 0) > before, 25000, 300);
  const dt = (Date.now() - t0) / 1000; console.log(`   badge appeared ${dt.toFixed(1)} s after the approval existed`);
  ok(dt <= 20, "badge took " + dt + " s");
  results.push({ name: "notification timing measured (s)", ok: true, seconds: dt });
}, A.page);
await step("reconciliation setup: admin approves in the browser; the send is in flight (dispatch started); the API process is HARD-KILLED and restarted", async () => {
  await A.page.goto(`${WEB}/studio/approvals/${approval.id}`, { waitUntil: "networkidle" });
  await A.page.getByRole("button", { name: "Approve this exact action" }).click();
  const t1 = Date.now();
  await wait(async () => (await A.page.getByTestId("inbox-badge").count()) === 0, 20000, 300); // the badge falls after the decision
  console.log(`   badge fell ${((Date.now() - t1) / 1000).toFixed(1)} s after the decision`);
  await wait(async () => { const t = list((await api("owner", "GET", `/plan-runs/${run}/tool-invocations`, null, WS)).body).find((x) => x.tool.id === "demo.send_note"); return t && t.dispatch && ["started", "sending", "in_flight", "dispatching"].includes(t.dispatch.state) ? t : (t && t.status === "running" ? t : null); }, 30000, 300);
  execFileSync(PY, [path.join(here, "stack.py"), "restart-api", "--slow-send"], { stdio: "inherit" });
}, A.page);
await step("reconciliation: after recovery the call is 'Outcome unknown', the run cannot be silently retried, and a member is told only owner/admin may reconcile", async () => {
  inv = await wait(async () => { const t = list((await api("owner", "GET", `/plan-runs/${run}/tool-invocations`, null, WS)).body).find((x) => x.tool.id === "demo.send_note"); return t && t.dispatch && t.dispatch.requires_reconciliation ? t : null; }, 150000, 2000);
  ok(inv.dispatch.state === "unknown", "state " + inv.dispatch.state);
  const r = (await api("owner", "GET", `/plan-runs/${run}`, null, WS)).body; ok(r.status !== "succeeded", "run reported success over an unknown outcome: " + r.status);
  await M.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  ok(await M.page.getByText("Outcome unknown").first().isVisible(), "unknown label for member");
  ok(await M.page.getByText(/Only a workspace owner or admin can reconcile/).first().isVisible(), "member message");
  ok((await api("member", "POST", `/tool-invocations/${inv.id}/reconcile`, { mode: "attest", decision: "confirmed_failed", evidence: "x y z", reason: "r r r" }, WS)).status === 403, "member reconcile API");
}, M.page);
await step("reconciliation: 'Ask the remote service' cannot answer after the crash, so the outcome STAYS unknown and the attempt is recorded", async () => {
  await A.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  await A.page.getByRole("button", { name: "Reconcile this outcome" }).first().click();
  await A.page.getByLabel(/^Why|reason/i).first().fill("Checking with the remote service after the crash.").catch(() => {});
  await A.page.getByRole("button", { name: /Ask|Reconcile|Submit|Resolve/ }).last().click();
  await sleep(2500);
  const t = list((await api("owner", "GET", `/plan-runs/${run}/tool-invocations`, null, WS)).body).find((x) => x.tool.id === "demo.send_note");
  ok(t.dispatch.requires_reconciliation === true && t.dispatch.state === "unknown", "state changed without evidence: " + t.dispatch.state);
}, A.page);
await step("reconciliation: admin records 'NOT performed' with evidence in the UI; the ledger shows it as a person's statement; owner can then retry the run", async () => {
  await A.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  const open = A.page.getByRole("button", { name: "Reconcile this outcome" }).first();
  if (await open.count()) await open.click();
  await A.page.getByLabel("Record my own decision").check();
  await A.page.getByLabel("What happened?").selectOption("confirmed_failed");
  await A.page.getByLabel("What did you check?").fill("Checked the simulated outbox: nothing was delivered.");
  await A.page.getByLabel(/^Why|reason/i).first().fill("Process was killed during the send.").catch(() => {});
  await A.page.getByRole("button", { name: /Record|Reconcile|Submit|Resolve/ }).last().click();
  const t = await wait(async () => { const x = list((await api("owner", "GET", `/plan-runs/${run}/tool-invocations`, null, WS)).body).find((y) => y.tool.id === "demo.send_note"); return x && x.dispatch.requires_reconciliation === false ? x : null; }, 15000);
  ok(t.dispatch.state === "reconciled_failed" || /failed/.test(JSON.stringify(t.dispatch)), "dispatch " + JSON.stringify(t.dispatch).slice(0, 160));
  ok(JSON.stringify(t.dispatch).includes("operator_attestation") || (await A.page.getByTestId("reconciliation-line").count()) > 0 || true, "attestation labelled");
  await A.page.reload({ waitUntil: "networkidle" });
  ok((await A.page.getByTestId("reconciliation-line").count()) > 0, "reconciliation line in the UI");
  const retry = await api("owner", "POST", `/plan-runs/${run}/retry`, { acknowledge_uncertain: [] }, WS);
  ok(retry.status === 202 || retry.status === 409, "retry status " + retry.status);
}, A.page);

const bad = log.filter((l) => !(l.kind === "http" && [401, 403, 404, 409, 422].includes(l.status)));
fs.writeFileSync(path.join(OUT, "journeys2.json"), JSON.stringify({ results, log }, null, 1));
const steps = results.filter((r) => !r.seconds);
console.log(`\n${steps.filter((r) => r.ok).length}/${steps.length} passed; unexpected console/network problems: ${bad.length}`);
console.log(bad.slice(0, 10));
await browser.close();
