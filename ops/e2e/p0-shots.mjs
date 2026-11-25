// P0 before/after screenshots at 1440, 1280, 390, 320 for the surfaces P0 changes: the public root, sign-in, a brand-new person's overview (first run), the command palette (after only),
// and the Studio overview of an established workspace. Usage: node p0-shots.mjs <before|after> [theme=dark|light]. Writes out/p0/<label>-<theme>/. Needs seed.mjs + content.mjs.
import fs from "node:fs";
import path from "node:path";
import { chromium, BROWSER, openAs, PEOPLE, OUT, cfg } from "./lib.mjs";

const label = process.argv[2] || "before";
const theme = process.argv[3] || "dark";
const seed = JSON.parse(fs.readFileSync(path.join(OUT, "seed.json")));
const DIR = path.join(OUT, "p0", `${label}-${theme}`);
fs.mkdirSync(DIR, { recursive: true });
const WEB = cfg.web;
PEOPLE.fresh = { sub: "e2e-fresh", email: "fresh@e2e.test", name: "Fiona Fresh" };
const VIEWS = { d1440: { width: 1440, height: 900 }, l1280: { width: 1280, height: 800 }, m390: { width: 390, height: 844 }, n320: { width: 320, height: 640 } };
const browser = await chromium.launch({ executablePath: BROWSER });
const report = [];
const setTheme = (p) => p.addInitScript((t) => { try { localStorage.setItem("agentforge-theme", t); } catch {} }, theme);

for (const [vn, vp] of Object.entries(VIEWS)) {
  const anon = await browser.newContext({ viewport: vp, reducedMotion: "reduce" });
  const ap = await anon.newPage();
  await setTheme(ap);
  for (const [name, url] of [["root-signedout", "/"], ["signin", "/studio"]]) {
    await ap.goto(WEB + url, { waitUntil: "networkidle" }).catch(() => {});
    await ap.waitForTimeout(500);
    const m = await ap.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    if (m.sw > m.cw) report.push(`OVERFLOW ${label}/${theme} ${vn} ${name}: ${m.sw} > ${m.cw}`);
    await ap.screenshot({ path: path.join(DIR, `${vn}-${name}.png`), fullPage: name === "root-signedout" });
  }
  await anon.close();
  for (const [who, name, url, ws] of [["fresh", "firstrun-overview", "/studio", null], ["owner", "established-overview", "/studio", seed.workspace]]) {
    const { ctx, page } = await openAs(browser, who, vp, []);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await setTheme(page);
    if (ws) await page.addInitScript((id) => sessionStorage.setItem("agentforge.workspace", id), ws);
    await page.goto(WEB + url, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);
    const m = await page.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
    if (m.sw > m.cw) report.push(`OVERFLOW ${label}/${theme} ${vn} ${name}: ${m.sw} > ${m.cw}`);
    await page.screenshot({ path: path.join(DIR, `${vn}-${name}.png`), fullPage: true });
    if (label === "after" && who === "fresh") {
      await page.keyboard.press("Control+k");
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(DIR, `${vn}-palette.png`) });
    }
    await ctx.close();
  }
}
await browser.close();
console.log(report.join("\n") || `no horizontal overflow (${label}/${theme})`);
