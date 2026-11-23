// Fresh axe-core scan (wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa) across the redesigned pages, both themes, three widths, plus interactive states.
// Needs a stack with seed.mjs + content.mjs. Automated checks only: they do NOT replace a human screen-reader pass.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { api, chromium, BROWSER, openAs, OUT, cfg } from "./lib.mjs";

const axeSource = fs.readFileSync(createRequire(import.meta.url).resolve("axe-core/axe.min.js"), "utf8");
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const WEB = cfg.web;
const agents = (await api("owner", "GET", "/agents", null, seed.workspace)).body.items;
const PAGES = { overview: "/studio", plans: "/studio/plans", plan: `/studio/plans/${c.done.plan}`, runs: "/studio/runs", "run-done": `/studio/runs/${c.done.run}`, "run-waiting": `/studio/runs/${c.waiting.run}`,
  "run-rejected": `/studio/runs/${c.rejected.run}`, agents: "/studio/agents", "agent-new": "/studio/agents/new", agent: `/studio/agents/${agents[0].id}`, templates: "/studio/templates", approvals: "/studio/approvals",
  approval: `/studio/approvals/${c.waiting.approval}`, tools: "/studio/tools", tool: "/studio/tools/demo.send_note", activity: "/studio/tools/activity", team: "/studio/team", settings: "/studio/settings" };
const VIEWS = { d1280: { width: 1280, height: 800 }, m390: { width: 390, height: 844 }, n320: { width: 320, height: 640 } };
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
  }
}
await browser.close();
const by = {};
for (const f of findings) (by[f.id] ||= { impact: f.impact, help: f.help, where: [] }).where.push(`${f.label}: ${f.nodes.join(" | ")}`);
fs.writeFileSync(path.join(OUT, "axe.json"), JSON.stringify({ scans, violations: findings.length, by }, null, 1));
console.log(`${scans} scans, ${findings.length} violations`);
for (const [id, v] of Object.entries(by)) console.log(`- ${id} (${v.impact}): ${v.help} x${v.where.length}\n    e.g. ${v.where[0]}`);
