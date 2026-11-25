import fs from "node:fs"; import path from "node:path";
import { chromium, BROWSER, openAs, PEOPLE, OUT } from "./lib.mjs";
PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const b = await chromium.launch({ executablePath: BROWSER });
for (const [who, label] of [["owner", "established"], ["fresh", "first-run"]]) {
  const { ctx, page } = await openAs(b, who, { width: 1440, height: 900 }, []);
  if (who === "owner") await page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), seed.workspace);
  await page.addInitScript(() => { window.__shifts = []; new PerformanceObserver((l) => { for (const e of l.getEntries()) if (!e.hadRecentInput && e.value > 0.0005) window.__shifts.push({ v: +e.value.toFixed(4), t: Math.round(e.startTime), src: e.sources.map((s) => { const n = s.node; return (n && (n.nodeName + "." + (n.className || "").toString().slice(0, 40))) + ` ${Math.round(s.previousRect.y)}->${Math.round(s.currentRect.y)} h${Math.round(s.previousRect.height)}->${Math.round(s.currentRect.height)}`; }) }); }).observe({ type: "layout-shift", buffered: true }); });
  await page.goto("http://localhost:3040/studio", { waitUntil: "networkidle" }); await page.waitForTimeout(2000);
  console.log(label, JSON.stringify(await page.evaluate(() => window.__shifts), null, 1));
  await ctx.close();
}
await b.close();
