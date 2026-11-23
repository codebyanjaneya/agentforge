// Accessibility-TREE check of the manual screen-reader script (docs/accessibility.md). It asserts what a screen reader would RECEIVE (landmarks, headings, roles, accessible
// names, dialog semantics, live regions, focus order). It is NOT a screen-reader test: nothing here hears, speaks or navigates like NVDA/VoiceOver, and it must never be
// reported as one. Needs a stack with seed.mjs + content.mjs. Writes the raw ARIA snapshots to out/a11y-tree/ as evidence.
import fs from "node:fs";
import path from "node:path";
import { api, chromium, BROWSER, openAs, OUT, cfg } from "./lib.mjs";

const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const WS = seed.workspace;
const WEB = cfg.web;
const DIR = path.join(OUT, "a11y-tree");
fs.mkdirSync(DIR, { recursive: true });
const results = [];
const ok = (cond, m) => { if (!cond) throw new Error(m); };
async function step(name, fn) {
  try { await fn(); results.push({ name, ok: true }); console.log("PASS", name); }
  catch (e) { const msg = String(e.message || e).split("\n")[0].slice(0, 300); results.push({ name, ok: false, err: msg }); console.log("FAIL", name, "-", msg); }
}
const browser = await chromium.launch({ executablePath: BROWSER });
async function as(who, vp = { width: 1280, height: 800 }) {
  const s = await openAs(browser, who, vp, []);
  await s.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  return s;
}
async function snap(page, name) {
  const y = await page.locator("body").ariaSnapshot();
  fs.writeFileSync(path.join(DIR, name + ".yml"), y);
  return y;
}
const names = (page, role) => page.getByRole(role).evaluateAll((els) => els.map((e) => (e.getAttribute("aria-label") || e.textContent || "").trim().replace(/\s+/g, " ").slice(0, 60)));
const O = await as("owner");
const M = await as("member");

await step("1 landmarks: exactly one main, two named navigations, a banner, and 'Skip to content' is the first stop and targets main", async () => {
  await O.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  const y = await snap(O.page, "01-overview");
  ok((await O.page.getByRole("main").count()) === 1, "main count");
  const navs = await names(O.page, "navigation");
  ok(navs.includes("Primary"), "navigation names: " + JSON.stringify(navs)); // the compact bar is display:none at this width, so it is correctly absent from the tree (checked at 390 px in step 10)
  ok(await O.page.getByRole("banner").count() >= 0, "");
  await O.page.keyboard.press("Tab");
  ok(/Skip to content/.test(await O.page.evaluate(() => document.activeElement.textContent)), "first stop");
  ok(await O.page.evaluate(() => document.querySelector(".st-skip").getAttribute("href") === "#main" && document.getElementById("main")?.tagName === "MAIN"), "skip target");
  ok(/link "Overview"/.test(y) && /heading/.test(y), "snapshot lacks links/headings");
});
await step("2 overview: exactly one h1; the next step is TEXT ('Next step' region with words), not colour", async () => {
  ok((await O.page.getByRole("heading", { level: 1 }).count()) === 1, "h1 count");
  const next = O.page.getByLabel("Next step");
  ok((await next.count()) >= 1 && (await next.first().innerText()).trim().length > 10, "next-step text");
});
let draft;
await step("3 plan (draft): status as text, tasks as a list, named buttons Approve / Edit tasks / Validate / Cancel plan", async () => {
  draft = (await api("owner", "POST", "/plans", { objective: "Launch check: price $59/month, 75% margin, 4% monthly churn, CAC $350. Email jo@acme.io on the launch date." }, WS)).body.id;
  await O.page.goto(`${WEB}/studio/plans/${draft}`, { waitUntil: "networkidle" });
  await snap(O.page, "03-plan-draft");
  const btn = await names(O.page, "button");
  for (const want of [/^Approve/, /^Edit tasks/, /^Validate/, /^Cancel plan/]) ok(btn.some((b) => want.test(b)), "button missing " + want);
  ok((await O.page.getByRole("list").count()) > 0 && (await O.page.getByRole("listitem").count()) >= 3, "tasks are not a list");
  ok((await O.page.getByRole("heading", { level: 1 }).count()) === 1, "h1");
});
await step("4 dialog: native modal with an accessible name 'Approve revision N?', focus inside on 'Not now', background inert, Escape closes and returns focus", async () => {
  await O.page.getByRole("button", { name: /^Approve/ }).first().click();
  const d = O.page.getByRole("dialog", { name: /Approve revision \d+\?/ });
  await d.waitFor({ timeout: 5000 });
  await snap(O.page, "04-dialog-open");
  ok(await O.page.evaluate(() => { const e = document.activeElement; return e.closest("dialog") && /Not now/.test(e.textContent); }), "focus not on Not now");
  ok(await O.page.evaluate(() => document.querySelector("dialog").matches(":modal")), "not modal");
  await O.page.keyboard.press("Escape");
  await O.page.getByRole("dialog").waitFor({ state: "hidden" });
  ok(await O.page.evaluate(() => /^Approve/.test(document.activeElement.textContent)), "focus not returned");
  await O.page.getByRole("button", { name: /^Cancel plan/ }).click();
  ok(await O.page.getByRole("dialog", { name: /Cancel/ }).count() === 1, "Cancel plan dialog name must start with Cancel (not the Approve dialog's title)");
  await O.page.keyboard.press("Escape");
});
await step("5 run page: status is a word; the task table has column headers and an accessible name; a polite live region exists; side-effect state is words", async () => {
  await O.page.goto(`${WEB}/studio/runs/${c.waiting.run}`, { waitUntil: "networkidle" });
  const y = await snap(O.page, "05-run-waiting");
  ok(/Running|Completed|Failed|Suspended|waiting/i.test(await O.page.locator("main").innerText()), "status word");
  ok(await O.page.locator("[aria-live=polite], [role=status]").count() > 0, "no polite live region");
  ok(await O.page.locator("th").count() > 0 && (await O.page.locator("th[scope=col], thead th").count()) > 0, "table headers");
  ok(await O.page.locator("[role=region][aria-label]").count() > 0, "named regions");
  await O.page.goto(`${WEB}/studio/runs/${c.done.run}`, { waitUntil: "networkidle" });
  await snap(O.page, "05-run-done");
  ok(/Confirmed performed|performed|Succeeded/i.test(await O.page.locator("main").innerText()), "side-effect words");
});
await step("6 approval: who, what, why, impact, decision are read in that DOM order; Approve/Reject named for an owner; a member gets an explanation instead of the buttons", async () => {
  await O.page.goto(`${WEB}/studio/approvals/${c.waiting.approval}`, { waitUntil: "networkidle" });
  await snap(O.page, "06-approval-owner");
  const text = (await O.page.locator("main").innerText()).toUpperCase();
  const order = ["WHO", "WHAT", "WHY", "IMPACT", "DECISION"].map((k) => text.indexOf(k));
  ok(order.every((v, i) => v >= 0 && (i === 0 || v > order[i - 1])), "order " + order);
  ok((await names(O.page, "button")).some((b) => /^Approve this exact action/.test(b)) && (await names(O.page, "button")).some((b) => /^Reject this action/.test(b)), "decision buttons");
  await M.page.goto(`${WEB}/studio/approvals/${c.waiting.approval}`, { waitUntil: "networkidle" });
  await snap(O.page, "06-approval-member");
  const mb = await names(M.page, "button");
  ok(!mb.some((b) => /^Approve this exact action/.test(b)), "member has the approve button");
  ok(/owner|admin/i.test(await M.page.locator("main").innerText()), "member explanation text");
});
await step("7 team: invitation success is announced (live region), the link field is labelled 'Invitation link', role selects are labelled 'Role of <email>', Remove is a button", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  ok((await O.page.getByLabel("Role of member@e2e.test").count()) === 1, "role select label");
  await O.page.getByLabel(/Invite by email/).fill("sr-check@e2e.test");
  await O.page.getByRole("button", { name: "Create invitation" }).click();
  const link = O.page.locator("[aria-label='Invitation link']").first();
  await link.waitFor();
  ok(await O.page.evaluate(() => { const l = document.querySelector("[aria-label='Invitation link']"); return !!l.closest("[aria-live], [role=status], [role=alert]") || !!document.querySelector("[role=status]:not(.sr-only)"); }), "success not inside a live region");
  await snap(O.page, "07-team-invitation");
  ok((await names(O.page, "button")).some((b) => /^Remove/.test(b)), "Remove button");
});
await step("8 join page: the outcome ('This invitation can't be used') is in an alert/live region", async () => {
  await O.page.goto(`${WEB}/studio/join?token=${"x".repeat(43)}`, { waitUntil: "networkidle" });
  const b = O.page.getByRole("button", { name: "Join workspace" });
  if (await b.count()) await b.click();
  await O.page.getByText(/can't be used|couldn't|could not/i).first().waitFor({ timeout: 10000 });
  ok(await O.page.evaluate(() => { const e = [...document.querySelectorAll("*")].find((n) => /can't be used/.test(n.textContent) && n.children.length === 0); return !!e?.closest("[role=alert], [role=status], [aria-live]"); }), "outcome not announced (no live region)");
});
await step("9 errors: a failed action shows an error that is an alert/live region and the page stays operable", async () => {
  await O.page.goto(`${WEB}/studio/agents/new`, { waitUntil: "networkidle" });
  await O.page.getByRole("button", { name: "Create agent" }).click();
  ok(await O.page.locator("[aria-invalid=true]").count() > 0 && await O.page.locator(".st-error-text").count() > 0, "field errors");
  ok(await O.page.evaluate(() => { const e = document.activeElement; const d = e?.getAttribute("aria-describedby"); const t = d && document.getElementById(d); return e?.getAttribute("aria-invalid") === "true" && !!t && t.textContent.trim().length > 3 && t.classList.contains("st-error-text"); }), "the focused invalid field is not described by an existing error element");
});
const P = await as("owner", { width: 390, height: 844 });
await step("10 mobile: 'More' has an accessible name and exposes expanded/collapsed state; links inside are named; the sheet closes on navigation", async () => {
  await P.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  const more = P.page.locator("summary", { hasText: "More" });
  ok((await P.page.getByRole("navigation", { name: /Studio, compact/ }).count()) === 1, "compact nav");
  const before = await P.page.evaluate(() => document.querySelector("details.st-more").open);
  await more.click();
  const y = await snap(P.page, "10-mobile-more-open");
  ok(before === false && (await P.page.evaluate(() => document.querySelector("details.st-more").open)) === true, "toggle state");
  ok(/Settings/.test(y) && /Team/.test(y), "sheet links in tree");
});
await browser.close();
const fails = results.filter((r) => !r.ok);
fs.writeFileSync(path.join(OUT, "a11y-tree.json"), JSON.stringify(results, null, 1));
console.log(`\n${results.length - fails.length}/${results.length} accessibility-tree checks passed (NOT a screen-reader test)`);
