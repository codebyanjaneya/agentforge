// Compressed JS transfer (resource timing encodedBodySize) of the SAME routes on the pre-P0 build (port 3041) and the current build (port 3040), plus layout shift. Needs both servers up.
import fs from "node:fs";
import path from "node:path";
import { chromium, BROWSER, openAs, PEOPLE, OUT } from "./lib.mjs";
PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json"))); const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const b = await chromium.launch({ executablePath: BROWSER });
const ROUTES = [["/studio (established)", "owner", "/studio"], ["run page", "owner", `/studio/runs/${c.done.run}`], ["/ signed out (before: legacy dashboard, after: landing)", null, "/"], ["/studio new person (first run)", "fresh", "/studio"]];
async function measure(port, who, url) {
  const vp = { width: 1440, height: 900 };
  const { ctx } = who ? await openAs(b, who, vp, []) : { ctx: await b.newContext({ viewport: vp }) };
  const page = await ctx.newPage();
  if (who === "owner") await page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), seed.workspace);
  await page.addInitScript(() => { window.__cls = 0; new PerformanceObserver((l) => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cls += e.value; }).observe({ type: "layout-shift", buffered: true }); });
  await page.goto(`http://localhost:${port}${url}`, { waitUntil: "load" }); await page.waitForTimeout(3500);
  const r = await page.evaluate(() => ({ gz: performance.getEntriesByType("resource").filter((e) => /\.js(\?|$)/.test(e.name)).reduce((t, e) => t + (e.encodedBodySize || 0), 0), cls: window.__cls }));
  await ctx.close(); return r;
}
for (const [name, who, url] of ROUTES) {
  const before = await measure(3041, who, url), after = await measure(3040, who, url);
  console.log(`${name}: JS gz ${(before.gz / 1024).toFixed(1)} -> ${(after.gz / 1024).toFixed(1)} KB (${after.gz >= before.gz ? "+" : ""}${((after.gz - before.gz) / 1024).toFixed(1)} KB) | CLS ${before.cls.toFixed(3)} -> ${after.cls.toFixed(3)}`);
}
await b.close();
