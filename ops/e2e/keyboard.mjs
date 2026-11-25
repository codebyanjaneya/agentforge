// Keyboard-only regression: no mouse, only Tab / Shift+Tab / Enter / Space / Escape / arrows. Needs a stack with seed.mjs + content.mjs.
import fs from "node:fs";
import path from "node:path";
import { api, chromium, BROWSER, openAs, OUT, cfg } from "./lib.mjs";

const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const WS = seed.workspace;
const WEB = cfg.web;
const results = [];
const ok = (cond, m) => { if (!cond) throw new Error(m); };
const wait = async (fn, ms = 15000) => { const t = Date.now(); while (Date.now() - t < ms) { const v = await fn(); if (v) return v; await new Promise((r) => setTimeout(r, 300)); } throw new Error("timeout"); };
async function step(name, fn, page) {
  try { await fn(); results.push({ name, ok: true }); console.log("PASS", name); }
  catch (e) { const msg = String(e.message || e).split("\n")[0].slice(0, 260); results.push({ name, ok: false, err: msg }); console.log("FAIL", name, "-", msg); if (page) await page.screenshot({ path: path.join(OUT, "kbd-fail-" + name.replace(/\W+/g, "_").slice(0, 40) + ".png") }).catch(() => {}); }
}
const browser = await chromium.launch({ executablePath: BROWSER });
async function as(who, vp = { width: 1280, height: 800 }) {
  const s = await openAs(browser, who, vp, []);
  await s.page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), WS);
  return s;
}
const focused = (page) => page.evaluate(() => {
  const e = document.activeElement; const cs = getComputedStyle(e); const r = e.getBoundingClientRect();
  return { tag: e.tagName, role: e.getAttribute("role"), name: (e.getAttribute("aria-label") || e.innerText || e.id || e.getAttribute("placeholder") || "").trim().replace(/\s+/g, " ").slice(0, 50),
    ring: (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0) || cs.boxShadow !== "none", visible: r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight, body: e === document.body,
    inDialog: !!e.closest("dialog"), tabindex: e.getAttribute("tabindex") };
});
async function walk(page, n, key = "Tab") { const seen = []; for (let i = 0; i < n; i++) { await page.keyboard.press(key); seen.push(await focused(page)); } return seen; }

const O = await as("owner");
await step("overview: Tab reaches the nav, New objective, the objective box and Generate plan in a sensible order; every stop shows a focus indicator and is on screen", async () => {
  await O.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  const seen = await walk(O.page, 60);
  const names = seen.map((s) => s.name);
  for (const want of ["Overview", "Plans", "Runs", "Approvals", "Agents", "Tools", "Settings", "New objective"]) ok(names.some((n) => n.startsWith(want)), "never reached " + want);
  ok(seen.some((s) => s.tag === "TEXTAREA"), "objective box unreachable");
  const bad = seen.filter((s) => !s.body && (!s.ring || !s.visible));
  ok(bad.length === 0, "no focus ring / off screen: " + JSON.stringify(bad.slice(0, 3)));
  ok(seen.every((s) => !(Number(s.tabindex) > 0)), "positive tabindex");
}, O.page);
await step("overview: the nav is reachable BEFORE the content, so a skip-to-content link (WCAG 2.4.1) is checked", async () => {
  await O.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  await O.page.keyboard.press("Tab");
  const first = await focused(O.page);
  const skip = /skip/i.test(first.name);
  console.log(`   first Tab stop: "${first.name}" (skip link: ${skip})`);
  ok(skip || (await O.page.locator("a[href='#main'], a.sr-only[href^='#']").count()) > 0 || true, "");
  results.push({ name: "skip-to-content link present", ok: skip, note: skip ? "" : "absent: the sidebar is ~12 tab stops before the content" });
}, O.page);

let plan;
await step("plan: keyboard-only approve: the Approve button opens a dialog, focus moves INTO it and is trapped, Escape closes it and returns focus, Enter confirms", async () => {
  plan = (await api("owner", "POST", "/plans", { objective: "Launch check: price $59/month, 75% margin, 4% monthly churn, CAC $350. Email jo@acme.io on the launch date." }, WS)).body.id;
  await O.page.goto(`${WEB}/studio/plans/${plan}`, { waitUntil: "networkidle" });
  let reached = false;
  for (let i = 0; i < 60 && !reached; i++) { await O.page.keyboard.press("Tab"); reached = (await focused(O.page)).name.startsWith("Approve"); }
  ok(reached, "Approve not reachable by Tab");
  const trigger = await O.page.evaluate(() => { document.activeElement.setAttribute("data-kbd-trigger", "1"); return document.activeElement.innerText; });
  await O.page.keyboard.press("Enter");
  await O.page.getByRole("dialog").waitFor({ timeout: 5000 });
  ok((await focused(O.page)).inDialog, "focus did not move into the dialog");
  const inside = await walk(O.page, 8);
  ok(inside.every((s) => s.inDialog || s.body), "a page element outside the dialog took focus: " + JSON.stringify(inside.filter((s) => !s.inDialog && !s.body).slice(0, 2))); // native modal: past the last button focus goes to the browser UI (body); the page behind is inert
  ok(inside.filter((s) => s.inDialog).length >= 4, "focus did not cycle through the dialog buttons");
  await O.page.keyboard.press("Escape");
  await wait(async () => (await O.page.getByRole("dialog").count()) === 0, 3000);
  ok(await O.page.evaluate(() => document.activeElement?.getAttribute("data-kbd-trigger") === "1"), "focus not returned to the trigger after Escape");
  await O.page.keyboard.press("Enter");
  await O.page.getByRole("dialog").waitFor({ timeout: 5000 });
  let confirm = false;
  for (let i = 0; i < 8 && !confirm; i++) { const f = await focused(O.page); if (f.name === "Approve plan") confirm = true; else await O.page.keyboard.press("Tab"); }
  ok(confirm, "confirm button unreachable inside the dialog");
  await O.page.keyboard.press("Enter");
  await wait(async () => (await api("owner", "GET", `/plans/${plan}`, null, WS)).body.status === "approved", 15000);
}, O.page);
await step("agent form: Enter in a field submits; focus lands on the first invalid field (not on the button)", async () => {
  await O.page.goto(`${WEB}/studio/agents/new`, { waitUntil: "networkidle" });
  await O.page.getByLabel(/^Name/).focus();
  await O.page.keyboard.type("Keyboard Agent");
  await O.page.keyboard.press("Enter");
  await wait(async () => (await focused(O.page)).tag !== "INPUT" || (await O.page.evaluate(() => document.activeElement?.getAttribute("aria-invalid") === "true")), 3000);
  ok(await O.page.evaluate(() => document.activeElement?.getAttribute("aria-invalid") === "true"), "focus not on an invalid field");
}, O.page);
await step("team: the role select of a member is operable by keyboard alone", async () => {
  await O.page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
  await O.page.getByLabel("Role of starter@e2e.test").focus();
  await O.page.keyboard.press("ArrowDown"); // Member -> Admin on a native select (option order: Member, Admin)
  await wait(async () => (await api("owner", "GET", `/workspaces/${WS}/members`, null, WS)).body.items.find((m) => m.email === "starter@e2e.test").role === "admin", 8000);
  await O.page.getByLabel("Role of starter@e2e.test").selectOption("member");
}, O.page);
await step("approval: Approve/Reject are reachable and decidable by keyboard only (reject the waiting request)", async () => {
  await O.page.goto(`${WEB}/studio/approvals/${c.waiting.approval}`, { waitUntil: "networkidle" });
  let reached = false;
  for (let i = 0; i < 60 && !reached; i++) { await O.page.keyboard.press("Tab"); reached = (await focused(O.page)).name.startsWith("Reject this action"); }
  ok(reached, "Reject unreachable");
  await O.page.keyboard.press("Space");
  await wait(async () => (await api("owner", "GET", `/approvals/${c.waiting.approval}`, null, WS)).body.status === "rejected", 15000);
}, O.page);
await step("command palette: Ctrl+K opens it with focus in the search field, arrows move the active option (aria-activedescendant), Enter goes, Escape closes and returns focus", async () => {
  await O.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  await O.page.getByRole("button", { name: "Search pages and actions" }).focus();
  await O.page.keyboard.press("Control+k");
  await O.page.getByRole("dialog", { name: "Command palette" }).waitFor({ timeout: 5000 });
  ok(await O.page.evaluate(() => document.activeElement?.getAttribute("role") === "combobox"), "focus not in the combobox");
  const first = await O.page.evaluate(() => document.activeElement.getAttribute("aria-activedescendant"));
  await O.page.keyboard.press("ArrowDown");
  const second = await O.page.evaluate(() => document.activeElement.getAttribute("aria-activedescendant"));
  ok(first && second && first !== second, "ArrowDown did not move the active option");
  ok((await O.page.locator("[role=option][aria-selected=true]").count()) === 1, "exactly one selected option");
  await O.page.keyboard.type("plans");
  ok((await O.page.locator("[role=option]").first().innerText()).trim().startsWith("Plans"), "filter");
  await O.page.keyboard.press("Enter");
  await O.page.waitForURL(/\/studio\/plans$/, { timeout: 8000 });
  await O.page.keyboard.press("Control+k");
  await O.page.getByRole("dialog", { name: "Command palette" }).waitFor();
  await O.page.keyboard.press("Escape");
  await O.page.getByRole("dialog").waitFor({ state: "hidden" });
  ok(await O.page.evaluate(() => document.activeElement && document.activeElement !== document.body), "focus lost after Escape");
}, O.page);
await step("landing and sign-in: skip link is the first stop, the call to action is reachable and is a link, the sign-in button is operable", async () => {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  const pg = await ctx.newPage();
  await pg.goto(WEB + "/", { waitUntil: "networkidle" });
  await pg.keyboard.press("Tab");
  ok(/Skip to content/.test(await pg.evaluate(() => document.activeElement.textContent)), "landing: first stop");
  let cta = false;
  for (let i = 0; i < 20 && !cta; i++) { await pg.keyboard.press("Tab"); cta = await pg.evaluate(() => document.activeElement?.tagName === "A" && /Sign in with Google/.test(document.activeElement.textContent)); }
  ok(cta, "landing: sign-in call to action unreachable");
  await pg.keyboard.press("Enter");
  await pg.waitForURL(/\/signin/, { timeout: 8000 });
  let btn = false;
  for (let i = 0; i < 12 && !btn; i++) { await pg.keyboard.press("Tab"); btn = await pg.evaluate(() => /Continue with Google/.test(document.activeElement?.textContent || "")); }
  ok(btn, "sign-in: Continue with Google unreachable");
  await ctx.close();
});
const P = await as("owner", { width: 390, height: 844 });
await step("mobile: the More sheet opens with Enter, its links are reachable and Escape/activation close it", async () => {
  await P.page.goto(WEB + "/studio", { waitUntil: "networkidle" });
  let reached = false;
  for (let i = 0; i < 40 && !reached; i++) { await P.page.keyboard.press("Tab"); reached = (await focused(P.page)).name === "More"; }
  ok(reached, "More unreachable");
  await P.page.keyboard.press("Enter");
  ok((await P.page.locator("details.st-more[open]").count()) === 1, "did not open");
  await P.page.keyboard.press("Tab");
  ok((await focused(P.page)).tag === "A", "first sheet link not next in order");
  await P.page.keyboard.press("Escape");
  const closed = (await P.page.locator("details.st-more[open]").count()) === 0;
  results.push({ name: "More sheet closes on Escape", ok: closed, note: closed ? "" : "does not close on Escape (native <details>)" });
  console.log(`   More sheet closes on Escape: ${closed}`);
}, P.page);
await browser.close();
const fails = results.filter((r) => !r.ok);
fs.writeFileSync(path.join(OUT, "keyboard.json"), JSON.stringify(results, null, 1));
console.log(`\n${results.length - fails.length}/${results.length} passed`);
