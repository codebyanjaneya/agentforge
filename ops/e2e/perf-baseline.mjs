// Page weight and paint timings. Decoded JS is the unit of the 2026-11 baseline (731 / 791 / 747 KB); "gz" is the compressed transfer the browser really received (resource timing,
// encodedBodySize). Usage: node perf-baseline.mjs. Needs seed.mjs + content.mjs.
import fs from "node:fs";
import path from "node:path";
import { chromium, BROWSER, openAs, PEOPLE, OUT } from "./lib.mjs";

PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const c = JSON.parse(fs.readFileSync(path.join(OUT, "content.json")));
const b = await chromium.launch({ executablePath: BROWSER });
const PAGES = [
  ["studio overview (established)", "owner", "/studio", true],
  ["run page", "owner", `/studio/runs/${c.done.run}`, true],
  ["classic dashboard (was `/`)", "owner", "/classic", false],
  ["landing `/` (signed out)", null, "/", false],
  ["sign-in (signed out)", null, "/signin", false],
  ["first-run overview (new person)", "fresh", "/studio", false],
];
for (const [name, who, url, ws] of PAGES) {
  const ctx = who ? (await openAs(b, who, { width: 1440, height: 900 }, [])).ctx : await b.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await ctx.newPage();
  if (who === "owner" && ws) await page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), seed.workspace);
  if (!who) await page.addInitScript(() => {});
  let js = 0;
  page.on("response", async (r) => { try { if (/javascript/.test(r.headers()["content-type"] || "")) js += (await r.body()).length; } catch {} });
  await page.addInitScript(() => {
    window.__m = { lcp: 0, cls: 0 };
    new PerformanceObserver((l) => { for (const e of l.getEntries()) window.__m.lcp = e.startTime; }).observe({ type: "largest-contentful-paint", buffered: true });
    new PerformanceObserver((l) => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__m.cls += e.value; }).observe({ type: "layout-shift", buffered: true });
  });
  await page.goto("http://localhost:3040" + url, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const m = await page.evaluate(() => ({
    ...window.__m,
    fcp: performance.getEntriesByName("first-contentful-paint")[0]?.startTime,
    gz: performance.getEntriesByType("resource").filter((e) => /\.js(\?|$)/.test(e.name)).reduce((t, e) => t + (e.encodedBodySize || 0), 0),
    css: performance.getEntriesByType("resource").filter((e) => /\.css(\?|$)/.test(e.name)).reduce((t, e) => t + (e.encodedBodySize || 0), 0),
    dom: document.querySelectorAll("*").length,
  }));
  console.log(`${name}: JS ${(js / 1024).toFixed(0)} KB decoded / ${(m.gz / 1024).toFixed(0)} KB gz, CSS ${(m.css / 1024).toFixed(0)} KB gz | FCP ${Math.round(m.fcp)} ms, LCP ${Math.round(m.lcp)} ms, CLS ${m.cls.toFixed(3)}, DOM ${m.dom}`);
  await ctx.close();
}
await b.close();
