// Read-only visual audit: the first-time experience (a brand-new person with nothing), the signed-out path, and the classic routes. Writes to out/audit/. No application change.
import fs from "node:fs";
import path from "node:path";
import { chromium, BROWSER, openAs, PEOPLE, OUT, cfg } from "./lib.mjs";

const DIR = path.join(OUT, "audit");
fs.mkdirSync(DIR, { recursive: true });
const WEB = cfg.web;
PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };
const VIEWS = { d1440: { width: 1440, height: 900 }, m390: { width: 390, height: 844 } };
const browser = await chromium.launch({ executablePath: BROWSER });
const problems = [];

for (const [vn, vp] of Object.entries(VIEWS)) {
  // 1. signed out: what an external visitor meets
  const anon = await browser.newContext({ viewport: vp });
  const ap = await anon.newPage();
  for (const [name, url] of [["signedout-root", "/"], ["signedout-studio", "/studio"]]) {
    await ap.goto(WEB + url, { waitUntil: "networkidle" }).catch(() => {});
    await ap.screenshot({ path: path.join(DIR, `${vn}-${name}.png`), fullPage: false });
    console.log(vn, name, "->", ap.url().replace(WEB, ""));
  }
  await anon.close();
  // 2. a brand-new signed-in person: personal workspace, no agents, no plans
  const { ctx, page } = await openAs(browser, "fresh", vp, problems);
  for (const [name, url] of [["fresh-overview", "/studio"], ["fresh-agents", "/studio/agents"], ["fresh-plans", "/studio/plans"], ["fresh-runs", "/studio/runs"], ["fresh-approvals", "/studio/approvals"], ["fresh-templates", "/studio/templates"],
    ["fresh-tools", "/studio/tools"], ["fresh-team", "/studio/team"], ["classic-root", "/"], ["classic-autonomous", "/autonomous"], ["classic-history", "/history"], ["classic-improve", "/improve"], ["classic-twin", "/twin"]]) {
    await page.goto(WEB + url, { waitUntil: "networkidle" }).catch(() => {});
    await page.waitForTimeout(600);
    await page.screenshot({ path: path.join(DIR, `${vn}-${name}.png`), fullPage: vn === "d1440" ? false : false });
  }
  await ctx.close();
}
await browser.close();
console.log("console/network problems on these pages:", problems.length, [...new Set(problems.map((p) => `${p.kind} ${p.status || ""} ${p.url || p.text}`))].slice(0, 10));
