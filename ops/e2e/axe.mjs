// Fresh axe-core scan (wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa) across the redesigned pages, both themes, three widths, plus interactive states.
// Needs a stack with seed.mjs + content.mjs. Automated checks only: they do NOT replace a human screen-reader pass.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { api, chromium, BROWSER, openAs, PEOPLE, OUT, cfg } from "./lib.mjs";
PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };

const axeSource = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const WEB = cfg.web;
const agents = (await api("owner", "GET", "/agents", null, seed.workspace)).body.items;
const PAGES = { overview: "/studio", plans: "/studio/plans", plan: `/studio/plans/${c.done.plan}`, runs: "/studio/runs", "run-done": `/studio/runs/${c.done.run}`, "run-waiting": `/studio/runs/${c.waiting.run}`,
  "run-rejected": `/studio/runs/${c.rejected.run}`, agents: "/studio/agents", "agent-new": "/studio/agents/new", agent: `/studio/agents/${agents[0].id}`, templates: "/studio/templates", approvals: "/studio/approvals",
  approval: `/studio/approvals/${c.waiting.approval}`, tools: "/studio/tools", tool: "/studio/tools/demo.send_note", activity: "/studio/tools/activity", team: "/studio/team", settings: "/studio/settings" };
const VIEWS = { d1440: { width: 1440, height: 900 }, d1280: { width: 1280, height: 800 }, m390: { width: 390, height: 844 }, n320: { width: 320, height: 640 } };
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];
const findings = []; let scans = 0;
const browser = await chromium.launch({ executablePath: BROWSER });

async function scan(page, label) {
  await page.evaluate(axeSource);
  const r = await page.evaluate(async (tags) => { const x = await window.axe.run(document, { runOnly: { type: "tag", values: tags } }); return { v: x.violations.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map((n) => n.target.join(" ")) })), incomplete: x.incomplete.length, passes: x.passes.length }; }, TAGS);
  scans++;
  for (const v of r.v) findings.push({ label, ...v });
}

for (const theme of ["dark", "light"]) {
  for (const [vn, vp] of Object.entries(VIEWS)) {
    const { ctx, page } = await openAs(browser, "owner", vp, []);
    await page.addInitScript(([id, th]) => { sessionStorage.setItem("agentforge.workspace", id); localStorage.setItem("agentforge-theme", th); }, [seed.workspace, theme]);
    for (const [name, url] of Object.entries(PAGES)) {
      await page.goto(WEB + url, { waitUntil: "networkidle" });
      await page.waitForTimeout(300);
      await scan(page, `${theme} ${vn} ${name}`);
    }
    // interactive states
    await page.goto(`${WEB}/studio/agents/new`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Create agent" }).click();
    await scan(page, `${theme} ${vn} agent-form-with-errors`);
    const plan = (await api("owner", "POST", "/plans", { objective: "Launch check: price $59/month, 75% margin, 4% monthly churn, CAC $350. Email jo@acme.io on the launch date." }, seed.workspace)).body.id;
    await page.goto(`${WEB}/studio/plans/${plan}`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Edit tasks" }).click();
    await scan(page, `${theme} ${vn} plan-editor`);
    await page.reload({ waitUntil: "networkidle" });
    await page.getByRole("button", { name: /^Approve/ }).first().click();
    await scan(page, `${theme} ${vn} confirm-dialog-open`);
    await page.keyboard.press("Escape");
    await page.goto(`${WEB}/studio/team`, { waitUntil: "networkidle" });
    await scan(page, `${theme} ${vn} team-with-pending-invitation`);
    if (vp.width <= 390) {
      await page.goto(`${WEB}/studio`, { waitUntil: "networkidle" });
      await page.locator("summary", { hasText: "More" }).click();
      await scan(page, `${theme} ${vn} more-sheet-open`);
    }
    await ctx.close();
    // P0 surfaces: the public landing page and sign-in (signed out), then a brand-new person's first-run overview, the checklist and the command palette
    const anon = await browser.newContext({ viewport: vp });
    const ap = await anon.newPage();
    await ap.addInitScript((th) => { try { localStorage.setItem("agentforge-theme", th); } catch {} }, theme);
    for (const [name, url] of [["landing", "/"], ["signin", "/signin"], ["signin-error", "/signin?error=AccessDenied"]]) {
      await ap.goto(WEB + url, { waitUntil: "networkidle" });
      await ap.waitForTimeout(300);
      await scan(ap, `${theme} ${vn} ${name}`);
    }
    await anon.close();
    const fresh = await openAs(browser, "fresh", vp, []);
    await fresh.page.addInitScript((th) => { try { localStorage.setItem("agentforge-theme", th); } catch {} }, theme);
    await fresh.page.goto(`${WEB}/studio`, { waitUntil: "networkidle" });
    await fresh.page.waitForTimeout(400);
    await scan(fresh.page, `${theme} ${vn} firstrun-overview`);
    await fresh.page.getByRole("button", { name: /Use$|We are launching/ }).first().click().catch(() => {});
    await scan(fresh.page, `${theme} ${vn} firstrun-example-picked`);
    await fresh.page.keyboard.press("Control+k");
    await fresh.page.waitForTimeout(300);
    await scan(fresh.page, `${theme} ${vn} command-palette-open`);
    await fresh.page.keyboard.type("zzzz");
    await scan(fresh.page, `${theme} ${vn} command-palette-no-results`);
    await fresh.ctx.close();
  }
}
await browser.close();
const by = {};
for (const f of findings) (by[f.id] ||= { impact: f.impact, help: f.help, where: [] }).where.push(`${f.label}: ${f.nodes.join(" | ")}`);
fs.writeFileSync(path.join(OUT, "axe.json"), JSON.stringify({ scans, violations: findings.length, by }, null, 1));
console.log(`${scans} scans, ${findings.length} violations`);
for (const [id, v] of Object.entries(by)) console.log(`- ${id} (${v.impact}): ${v.help} x${v.where.length}\n    e.g. ${v.where[0]}`);
