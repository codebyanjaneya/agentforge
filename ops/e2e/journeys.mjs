// Real-browser journeys against the e2e stack (see stack.py). Each step is PASS/FAIL; critical UI actions are verified against the backend API afterwards.
import fs from "node:fs";
import path from "node:path";
import { api, chromium, BROWSER, openAs, PEOPLE, OUT, cfg } from "./lib.mjs";

const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const WS = seed.workspace;
const WEB = cfg.web;
const results = [];
const log = [];
const ok = (c, m) => { if (!c) throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const wait = async (fn, ms = 60000) => { const t = Date.now(); while (Date.now() - t < ms) { const v = await fn(); if (v) return v; await sleep(700); } throw new Error("timeout"); };

async function step(name, fn, page) {
  try { await fn(); results.push({ name, ok: true }); console.log("PASS", name); }
  catch (e) {
    const msg = String(e.message || e).split("\n")[0].slice(0, 240);
    results.push({ name, ok: false, err: msg }); console.log("FAIL", name, "-", msg);
    if (page) await page.screenshot({ path: path.join(OUT, "fail-" + name.replace(/\W+/g, "_").slice(0, 50) + ".png") }).catch(() => {});
  }
}

const browser = await chromium.launch({ executablePath: BROWSER });
async function as(who, vp = { width: 1280, height: 800 }) {
  const s = await openAs(browser, who, vp, log);
  await s.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  return s;
}
const list = (b) => b.items || b;
const OBJ = "Launch check: price $59/month, 75% margin, 4% monthly churn, CAC $350. Email jo@acme.io on the launch date.";

const O = await as("owner");
const A = await as("admin");
const M = await as("member");
let plan, run, approvalId, approvalHash;

await step("owner: delegates an objective and gets a plan (row verified in API)", async () => {
  await O.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  await O.page.getByPlaceholder(/What should your AI workforce/).fill(OBJ);
  await O.page.getByRole("button", { name: "Generate plan" }).click();
  await O.page.waitForURL(/\/studio\/plans\/[0-9a-f-]{36}/, { timeout: 30000 });
  plan = O.page.url().split("/").pop();
  const p = await api("owner", "GET", `/plans/${plan}`, null, WS);
  ok(p.status === 200 && p.body.objective === OBJ && p.body.status === "draft", "plan row " + JSON.stringify(p.body).slice(0, 120));
}, O.page);

await step("owner: plan page shows the dependency graph with 3 tasks", async () => {
  await O.page.waitForSelector(".st-graph-node");
  ok((await O.page.locator(".st-graph-node").count()) === 3, "graph nodes");
}, O.page);

await step("owner: approves the plan (explicit confirm); backend records approved revision 1", async () => {
  await O.page.getByRole("button", { name: /^Approve/ }).first().click();
  await O.page.getByRole("button", { name: "Approve plan" }).click();
  await wait(async () => (await api("owner", "GET", `/plans/${plan}`, null, WS)).body.status === "approved", 15000);
  ok((await api("owner", "GET", `/plans/${plan}`, null, WS)).body.approved_revision === 1, "approved_revision");
}, O.page);

await step("owner: starts the run (explicit confirm); run row exists", async () => {
  await O.page.getByRole("button", { name: /^Start run/ }).first().click();
  await O.page.getByRole("dialog").getByRole("button", { name: "Start run" }).click();
  run = await wait(async () => list((await api("owner", "GET", `/plans/${plan}/runs`, null, WS)).body)[0]?.id, 20000);
}, O.page);

await step("a pending approval for the exact action appears; run page says it is waiting", async () => {
  const a = await wait(async () => list((await api("admin", "GET", "/approvals?status=pending", null, WS)).body).find((x) => x.run_id === run), 60000);
  approvalId = a.id; approvalHash = a.payload_hash;
  ok(a.tool.ref === "demo.send_note@1", "tool " + a.tool.ref);
  await O.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  ok(await O.page.getByText(/waiting for your approval/i).first().isVisible(), "suspension text");
}, O.page);

await step("member: reads the owner's plan and run (created-by shown) and has no enabled write actions", async () => {
  await M.page.goto(`${WEB}/studio/plans/${plan}`, { waitUntil: "networkidle" });
  ok(await M.page.getByText(/owner@e2e.test/i).first().isVisible(), "created-by shown");
  const start = M.page.getByRole("button", { name: /^Start run/ });
  ok((await start.count()) === 0 || (await start.first().isDisabled()), "member can start run");
  await M.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  const cancel = M.page.getByRole("button", { name: "Cancel run" });
  ok((await cancel.count()) === 0 || (await cancel.first().isDisabled()), "member can cancel");
}, M.page);

await step("member: can read the approval but cannot decide it (UI and API 403)", async () => {
  await M.page.goto(`${WEB}/studio/approvals/${approvalId}`, { waitUntil: "networkidle" });
  const b = M.page.getByRole("button", { name: "Approve this exact action" });
  ok((await b.count()) === 0 || (await b.isDisabled()), "approve enabled for member");
  const d = await api("member", "POST", `/approvals/${approvalId}/approve`, { payload_hash: approvalHash }, WS);
  ok(d.status === 403, "API status " + d.status);
}, M.page);

await step("admin: approves the exact action in the browser; consumed once; run succeeds; second decision refused", async () => {
  await A.page.goto(`${WEB}/studio/approvals/${approvalId}`, { waitUntil: "networkidle" });
  await A.page.getByRole("button", { name: "Approve this exact action" }).click();
  const s = await wait(async () => { const r = (await api("owner", "GET", `/plan-runs/${run}`, null, WS)).body; return ["succeeded", "failed", "partial"].includes(r.status) ? r : null; }, 60000);
  ok(s.status === "succeeded", "run " + s.status);
  const ev = list((await api("owner", "GET", `/plan-runs/${run}/events`, null, WS)).body).map((e) => e.type);
  ok(ev.filter((t) => t === "approval_consumed").length === 1, "approval consumed once");
  ok((await api("admin", "POST", `/approvals/${approvalId}/approve`, { payload_hash: approvalHash }, WS)).status === 409, "second decision must be refused");
}, A.page);

await step("tool calls of the run are readable by owner, admin and member (shared read)", async () => {
  for (const who of ["owner", "admin", "member"]) {
    const t = (await api(who, "GET", `/plan-runs/${run}/tool-invocations`, null, WS)).body.items;
    ok(t.some((x) => x.tool.id === "demo.send_note") && t.length >= 3, who + " saw " + t.length);
  }
});

await step("usage section is shown on the run page and the usage API answers for a member", async () => {
  await O.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  ok(await O.page.getByText("Tokens and cost").isVisible(), "usage section");
  ok((await api("member", "GET", `/plan-runs/${run}/usage`, null, WS)).status === 200, "usage api");
}, O.page);

let plan2, run2;
await step("owner: cancels a run from the UI; backend status is cancelled", async () => {
  const p = await api("owner", "POST", "/plans", { objective: OBJ }, WS); plan2 = p.body.id;
  await api("owner", "POST", `/plans/${plan2}/approve`, { revision: 1 }, WS);
  run2 = (await api("owner", "POST", `/plans/${plan2}/runs`, {}, WS)).body.id;
  await O.page.goto(`${WEB}/studio/runs/${run2}`, { waitUntil: "networkidle" });
  await O.page.getByRole("button", { name: "Cancel run" }).click();
  const d = O.page.getByRole("dialog");
  if (await d.count()) await d.getByRole("button", { name: /Cancel run|Yes|Confirm/ }).first().click();
  const s = await wait(async () => { const x = (await api("owner", "GET", `/plan-runs/${run2}`, null, WS)).body; return ["cancelled", "failed", "partial", "succeeded"].includes(x.status) ? x : null; }, 30000);
  ok(s.status === "cancelled", "status " + s.status);
}, O.page);

await step("owner: retries the cancelled run from the UI; attempt 2 exists", async () => {
  await O.page.reload({ waitUntil: "networkidle" });
  await O.page.getByRole("button", { name: /Retry/ }).first().click();
  const d = O.page.getByRole("dialog");
  await d.getByRole("button", { name: /Retry|Start/ }).last().click(); // WITHOUT acknowledging: tasks of a cancelled run may have taken effect, so it must be refused
  await O.page.getByText("Couldn't start the retry").waitFor({ timeout: 10000 });
  ok(list((await api("owner", "GET", `/plans/${plan2}/runs`, null, WS)).body).length === 1, "unacknowledged retry created an attempt");
  await O.page.getByRole("button", { name: /Retry/ }).first().click();
  for (const box of await d.getByRole("checkbox").all()) await box.check();
  await d.getByRole("button", { name: /Retry|Start/ }).last().click();
  await wait(async () => list((await api("owner", "GET", `/plans/${plan2}/runs`, null, WS)).body).length >= 2, 20000);
}, O.page);

await step("member cannot cancel the owner's run through the API (403/404)", async () => {
  const c = await api("member", "POST", `/plan-runs/${run2}/cancel`, {}, WS);
  ok([403, 404].includes(c.status), "status " + c.status);
});

await step("stranger: foreign workspace refused (403), foreign ids invisible (404), no tool-call leakage", async () => {
  ok((await api("stranger", "GET", "/plans", null, WS)).status === 403, "plans with foreign workspace header");
  ok((await api("stranger", "GET", `/plan-runs/${run}`, null, seed.other)).status === 404, "run by id");
  ok((await api("stranger", "GET", "/tool-invocations", null, seed.other)).body.items.length === 0, "tool invocations leak");
  ok((await api("stranger", "GET", `/approvals/${approvalId}`, null, seed.other)).status === 404, "approval by id");
});

await step("stranger: UI pointed at a foreign workspace shows no data", async () => {
  const S = await openAs(browser, "stranger", { width: 1280, height: 800 }, []);
  await S.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  await S.page.goto(`${WEB}/studio/runs/${run}`, { waitUntil: "networkidle" });
  ok(!(await S.page.getByText("Tell the first customer").count()), "foreign data rendered");
  await S.ctx.close();
});

await step("owner: creates a custom agent in the UI, saves a new version; backend shows version 2", async () => {
  await O.page.goto(`${WEB}/studio/agents/new`, { waitUntil: "networkidle" });
  await O.page.getByLabel(/^Name/).fill("Pricing Reviewer");
  await O.page.getByLabel(/^Role/).fill("Reviews pricing");
  await O.page.getByRole("button", { name: "Create agent" }).click(); // invalid on purpose: focus must land on the first invalid field
  ok(await O.page.evaluate(() => document.activeElement?.getAttribute("aria-invalid") === "true"), "focus not moved to the first invalid field");
  await O.page.getByLabel(/^Description/).fill("Reviews whether a price point is sustainable.");
  await O.page.getByLabel(/^Purpose/).fill("Checks that a price makes sense.");
  await O.page.getByRole("button", { name: "Create agent" }).click(); // still invalid (Instructions): focus moves again
  ok(await O.page.evaluate(() => document.activeElement?.id === "f-instructions" || document.activeElement?.getAttribute("aria-invalid") === "true"), "focus not on Instructions");
  await O.page.getByLabel(/^Instructions/).fill("Review the price, margin, churn and CAC and state clearly whether the price is sustainable.");
  await O.page.getByRole("button", { name: "Create agent" }).click();
  await O.page.waitForURL(/\/studio\/agents\/[0-9a-f-]{36}/, { timeout: 20000 });
  const id = O.page.url().split("/").pop();
  await O.page.getByLabel(/^Purpose/).fill("Checks that a price makes sense and says why.");
  await O.page.getByRole("button", { name: /Save as version 2/ }).click();
  await wait(async () => (await api("owner", "GET", `/agents/${id}`, null, WS)).body.latest_version === 2, 10000);
}, O.page);

await step("member cannot see the owner's private agents (agents are per-user)", async () => {
  const l = list((await api("member", "GET", "/agents", null, WS)).body);
  ok(!l.some((a) => a.slug.includes("pricing")), "leak");
});

await step("owner invites a never-signed-in person in the UI; they join on mobile; membership verified; reuse refused", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  await O.page.getByLabel(/Invite by email/).fill("newbie@e2e.test");
  await O.page.getByRole("button", { name: "Create invitation" }).click();
  const box = O.page.locator("[aria-label='Invitation link']").first();
  await box.waitFor();
  const link = ((await box.inputValue().catch(() => "")) || (await box.innerText())).trim();
  ok(/\/studio\/join/.test(link), "link shape");
  PEOPLE.newbie = { sub: "e2e-newbie", email: "newbie@e2e.test", name: "Nina Newbie" };
  const W = await openAs(browser, "newbie", { width: 390, height: 844 }, log);
  const url = link.replace(/^https?:\/\/[^/]+/, WEB);
  await W.page.goto(url, { waitUntil: "networkidle" });
  await W.page.getByRole("button", { name: "Join workspace" }).click();
  await wait(async () => JSON.stringify((await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body).includes("newbie@e2e.test"), 15000);
  const again = await api("newbie", "POST", "/invitations/accept", { token: new URL(url).hash.slice(1) || new URL(url).searchParams.get("token") });
  ok(again.status >= 400, "reuse status " + again.status);
  await W.ctx.close();
}, O.page);

await step("tool registry, tool page (delivery guarantee) and dispatch activity render", async () => {
  await O.page.goto(`${WEB}/studio/tools`, { waitUntil: "networkidle" });
  ok(await O.page.getByText("demo.send_note").first().isVisible(), "registry");
  await O.page.goto(`${WEB}/studio/tools/demo.send_note`, { waitUntil: "networkidle" });
  ok(await O.page.getByText(/Delivery guarantee/i).first().isVisible(), "delivery section");
  await O.page.goto(`${WEB}/studio/tools/activity`, { waitUntil: "networkidle" });
  ok(await O.page.getByText(/send_note/).first().isVisible(), "activity");
}, O.page);

await step("settings and templates pages render", async () => {
  await O.page.goto(`${WEB}/studio/settings`, { waitUntil: "networkidle" });
  ok(await O.page.getByText("Capability").first().isVisible(), "settings table");
  await O.page.goto(`${WEB}/studio/templates`, { waitUntil: "networkidle" });
  ok(await O.page.getByText(/template/i).first().isVisible(), "templates");
}, O.page);

await step("mobile 390: More sheet opens, navigates, and closes", async () => {
  const P = await as("owner", { width: 390, height: 844 });
  await P.page.goto(`${WEB}/studio`, { waitUntil: "networkidle" });
  await P.page.locator("summary", { hasText: "More" }).click();
  await P.page.locator(".st-more-sheet").getByRole("link", { name: "Settings" }).click();
  await P.page.waitForURL(/settings/);
  ok((await P.page.locator("details.st-more[open]").count()) === 0, "sheet still open");
  await P.ctx.close();
});

await step("session: a garbage session cookie shows the sign-in gate, never data", async () => {
  const ctx = await browser.newContext();
  await ctx.addCookies([{ name: "next-auth.session-token", value: "garbage", url: WEB }]);
  const pg = await ctx.newPage();
  await pg.goto(`${WEB}/studio`, { waitUntil: "networkidle" });
  ok(await pg.getByText(/Sign in/i).first().isVisible(), "sign-in gate");
  await ctx.close();
});

await step("API: invalid bearer token refused (401)", async () => {
  const r = await fetch(cfg.api + "/plans", { headers: { Authorization: "Bearer abc.def.ghi" } });
  ok(r.status === 401, "status " + r.status);
});

const bad = log.filter((l) => !(l.kind === "http" && [401, 403, 404, 409].includes(l.status)));
fs.writeFileSync(path.join(OUT, "journeys.json"), JSON.stringify({ results, log }, null, 1));
console.log(`\n${results.filter((r) => r.ok).length}/${results.length} passed; unexpected console/network problems: ${bad.length}`);
console.log(bad.slice(0, 12));
await browser.close();
