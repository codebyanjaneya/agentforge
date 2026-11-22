// node shots.mjs <label> [viewport-name...]: screenshots every Studio page as the owner at 1440/1280/390/320, reports horizontal overflow and console/network problems.
import fs from "node:fs";
import path from "node:path";
import { api, chromium, BROWSER, openAs, OUT } from "./lib.mjs";
const label = process.argv[2] || "before";
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json"), "utf8"));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json"), "utf8"));
const agents = (await api("owner", "GET", "/agents", null, seed.workspace)).body.items;
const VIEWS = { d1440: { width: 1440, height: 900 }, l1280: { width: 1280, height: 800 }, m390: { width: 390, height: 844 }, n320: { width: 320, height: 640 } };
const only = process.argv.slice(3);
const PAGES = { overview: "/studio", plans: "/studio/plans", plan: `/studio/plans/${c.done.plan}`, runs: "/studio/runs", "run-done": `/studio/runs/${c.done.run}`, "run-waiting": `/studio/runs/${c.waiting.run}`, "run-rejected": `/studio/runs/${c.rejected.run}`,
  agents: "/studio/agents", agent: `/studio/agents/${agents[0].id}`, templates: "/studio/templates", approvals: "/studio/approvals", approval: `/studio/approvals/${c.waiting.approval}`, tools: "/studio/tools", tool: "/studio/tools/demo.send_note", team: "/studio/team", settings: "/studio/settings" };
const browser = await chromium.launch({ executablePath: BROWSER });
const log = []; const report = [];
for (const [vn, vp] of Object.entries(VIEWS)) {
  if (only.length && !only.includes(vn)) continue;
  const { ctx, page } = await openAs(browser, "owner", vp, log);
  await page.addInitScript((id) => { try { sessionStorage.setItem("agentforge.workspace", id); } catch {} }, seed.workspace);
  for (const [name, url] of Object.entries(PAGES)) {
    await page.goto(`http://localhost:3040${url}`, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(500);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    if (m.sw > m.cw) report.push(`OVERFLOW ${label} ${vn} ${name}: scrollWidth ${m.sw} > ${m.cw}`);
    const dir = path.join(OUT, label); fs.mkdirSync(dir, { recursive: true });
    await page.screenshot({ path: path.join(dir, `${vn}-${name}.png`), fullPage: true });
  }
  if (vp.width <= 390) { await page.goto("http://localhost:3040/studio", { waitUntil: "networkidle" }); await page.locator("summary", { hasText: "More" }).click(); await page.waitForTimeout(300); await page.screenshot({ path: path.join(OUT, label, `${vn}-more-sheet.png`) }); }
  await ctx.close();
}
await browser.close();
fs.writeFileSync(path.join(OUT, `${label}-report.json`), JSON.stringify({ report, log }, null, 1));
console.log(report.join("\n") || "no horizontal overflow"); console.log("problems:", log.length, [...new Set(log.map((l) => `${l.kind} ${l.status || ""} ${l.url || l.text}`))].slice(0, 12));
