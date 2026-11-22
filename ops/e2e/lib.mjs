// Test-only helpers (see stack.py). Reads the throw-away keys from .run; fails if the stack is not an e2e stack.
import { createRequire } from "node:module";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const req = createRequire(path.join(here, "../../frontend/package.json"));
const { encode } = req("next-auth/jwt");
export const { chromium } = createRequire(import.meta.url)("playwright");
export const cfg = JSON.parse(fs.readFileSync(path.join(here, ".run/config.json"), "utf8"));
export const OUT = path.join(here, "out");
fs.mkdirSync(OUT, { recursive: true });

export const PEOPLE = {
  owner: { sub: "e2e-owner", email: "owner@e2e.test", name: "Olivia Owner" },
  admin: { sub: "e2e-admin", email: "admin@e2e.test", name: "Adam Admin" },
  member: { sub: "e2e-member", email: "member@e2e.test", name: "Maya Member" },
  starter: { sub: "e2e-starter", email: "starter@e2e.test", name: "Sam Starter" },
  stranger: { sub: "e2e-stranger", email: "stranger@e2e.test", name: "Sasha Stranger" },
};

const b64u = (b) => Buffer.from(b).toString("base64url");
export function accessToken(p) {
  const iat = Math.floor(Date.now() / 1000);
  const head = b64u(JSON.stringify({ alg: "ES256", typ: "JWT", kid: cfg.kid }));
  const body = b64u(JSON.stringify({ iss: "agentforge-web", aud: "agentforge-api", sub: p.sub, idp: "google", email: p.email, email_verified: true, token_use: "access", jti: crypto.randomUUID(), iat, exp: iat + 600 }));
  const sig = crypto.sign("sha256", Buffer.from(`${head}.${body}`), { key: cfg.private_key, dsaEncoding: "ieee-p1363" });
  return `${head}.${body}.${b64u(sig)}`;
}

export async function sessionCookie(p) {
  const value = await encode({ secret: cfg.nextauth_secret, maxAge: 3600, token: { sub: p.sub, name: p.name, email: p.email, provider: "google", providerSubject: p.sub, emailVerified: true } });
  return { name: "next-auth.session-token", value, url: cfg.web, httpOnly: true, sameSite: "Lax" };
}

/** Direct API call as `who` (used to seed and to verify backend state; the browser journeys use the real UI). */
export async function api(who, method, url, body, ws) {
  const r = await fetch(cfg.api + url, { method, headers: { Authorization: `Bearer ${accessToken(PEOPLE[who])}`, "Content-Type": "application/json", ...(ws ? { "X-Workspace-Id": ws } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  let json; try { json = JSON.parse(text); } catch { json = text; }
  return { status: r.status, body: json };
}

export const BROWSER = process.env.E2E_BROWSER || path.join(process.env.USERPROFILE || "", "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe");

/** A browser context signed in as `who` with the given viewport; collects console errors and failed requests into `log`. */
export async function openAs(browser, who, viewport, log = []) {
  const ctx = await browser.newContext({ viewport, colorScheme: "light" });
  await ctx.addCookies([await sessionCookie(PEOPLE[who])]);
  const page = await ctx.newPage();
  page.on("console", (m) => m.type() === "error" && log.push({ who, kind: "console", text: m.text().slice(0, 300), url: page.url() }));
  page.on("response", (r) => r.status() >= 400 && log.push({ who, kind: "http", status: r.status(), url: r.url().replace(cfg.api, "API").replace(cfg.web, "") }));
  page.on("pageerror", (e) => log.push({ who, kind: "pageerror", text: String(e).slice(0, 300) }));
  return { ctx, page };
}
