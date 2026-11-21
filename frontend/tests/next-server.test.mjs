// M1-G: the REAL Next.js server (`next dev`) exercising getServerSession(authOptions) in a route handler,
// the NextAuth session callback and proxy.ts. OPT-IN (it starts a server and compiles pages):
//   RUN_NEXT_SERVER_TESTS=1 node --test tests/next-server.test.mjs
//
// Google is MOCKED: there is no real sign-in. Session cookies are forged with next-auth's own `encode` and a test
// secret, so they are what NextAuth would issue after a successful Google login with the same claims. The OAuth
// round trip itself (and the `signIn` callback against a real Google response) is NOT covered here.

import assert from "node:assert/strict";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

import { backendPythonAvailable, backendVerify, decodePart, newSigningKey } from "./token-helpers.mjs";

const require = createRequire(import.meta.url);
const { encode } = require("next-auth/jwt");
const enabled = process.env.RUN_NEXT_SERVER_TESTS === "1";
const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SECRET = "test-only-nextauth-secret-" + crypto.randomBytes(8).toString("hex");
const SUB = "112233445566778899001";
const key = newSigningKey();
let server;
let base;
let origin;
let logs = "";

async function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

async function cookie(claims = {}, secret = SECRET) {
  const token = await encode({
    secret,
    token: { sub: SUB, email: "user@example.com", provider: "google", providerSubject: SUB, emailVerified: true, ...claims },
    maxAge: 7 * 24 * 60 * 60,
  });
  return `next-auth.session-token=${token}`;
}

const post = (headers = {}) => fetch(`${base}/api/backend-token`, { method: "POST", headers: { origin, ...headers }, redirect: "manual" });

before(async () => {
  if (!enabled) return;
  const port = await freePort();
  base = `http://localhost:${port}`;
  origin = base;
  server = spawn(process.execPath, [path.join(FRONTEND, "node_modules", "next", "dist", "bin", "next"), "dev", "-p", String(port)], {
    cwd: FRONTEND,
    env: {
      ...process.env,
      NODE_ENV: "development",
      NEXT_TELEMETRY_DISABLED: "1",
      NEXTAUTH_SECRET: SECRET,
      NEXTAUTH_URL: base,
      GOOGLE_CLIENT_ID: "test-client-id",
      GOOGLE_CLIENT_SECRET: "test-client-secret",
      BACKEND_TOKEN_PRIVATE_KEY: key.privatePem,
      BACKEND_TOKEN_KID: "test-kid-1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (d) => (logs += d));
  server.stderr.on("data", (d) => (logs += d));
  const deadline = Date.now() + 120000;
  while (!/Ready|ready in/.test(logs)) {
    if (Date.now() > deadline || server.exitCode !== null) throw new Error("next dev did not start:\n" + logs.slice(-1500));
    await new Promise((r) => setTimeout(r, 500));
  }
});

after(() => {
  if (server && server.exitCode === null) server.kill();
});

test("getToken in the route handler: no cookie is 401, a valid session cookie gets a token with the exact Google sub", { skip: !enabled }, async () => {
  const none = await post();
  assert.equal(none.status, 401);
  assert.equal(none.headers.get("cache-control"), "no-store");
  assert.equal(none.headers.get("pragma"), "no-cache");
  assert.deepEqual(await none.json(), { error: "unauthenticated" });

  const ok = await post({ cookie: await cookie() });
  assert.equal(ok.status, 200, logs.slice(-800));
  assert.equal(ok.headers.get("cache-control"), "no-store");
  const body = await ok.json();
  const claims = decodePart(body.token, 1);
  assert.equal(claims.sub, SUB, "the token carries the exact Google sub taken from the server-side JWT");
  assert.equal(claims.iss, "agentforge-web");
  assert.equal(claims.aud, "agentforge-api");
  assert.equal(claims.exp - claims.iat, 600);
  assert.equal(body.expires_at, claims.exp);
  if (backendPythonAvailable) assert.equal(backendVerify(body.token, key.publicPem).claims.subject, SUB);
});

test("forged, foreign-secret, unverified and non-Google sessions never get a token", { skip: !enabled }, async () => {
  assert.equal((await post({ cookie: await cookie({}, "another-secret-entirely") })).status, 401);
  assert.equal((await post({ cookie: "next-auth.session-token=garbage" })).status, 401);
  assert.equal((await post({ cookie: await cookie({ emailVerified: false }) })).status, 403);
  assert.equal((await post({ cookie: await cookie({ provider: "github" }) })).status, 403);
  assert.equal((await post({ cookie: await cookie({ providerSubject: undefined }) })).status, 401);
});

test("same-origin and method rules hold on the real server", { skip: !enabled }, async () => {
  const c = await cookie();
  assert.equal((await fetch(`${base}/api/backend-token`, { method: "POST", headers: { cookie: c } })).status, 403, "no Origin");
  assert.equal((await post({ cookie: c, origin: "https://evil.example" })).status, 403);
  assert.equal((await post({ cookie: c, "sec-fetch-site": "cross-site" })).status, 403);
  for (const method of ["GET", "PUT", "PATCH", "DELETE"]) {
    const r = await fetch(`${base}/api/backend-token`, { method, headers: { cookie: c, origin } });
    assert.equal(r.status, 405, method);
    assert.equal(r.headers.get("allow"), "POST");
    assert.equal(r.headers.get("cache-control"), "no-store");
  }
});

test("NextAuth session callback on the real server never exposes the Google subject or any token", { skip: !enabled }, async () => {
  const response = await fetch(`${base}/api/auth/session`, { headers: { cookie: await cookie({ accessToken: "LEAK", access_token: "LEAK", id_token: "LEAK" }) } });
  assert.equal(response.status, 200, logs.slice(-1500));
  const session = await response.json();
  assert.deepEqual(Object.keys(session).sort(), ["expires", "user"], "the browser session is an allow-list");
  assert.equal(session.user.email, "user@example.com");
  const text = JSON.stringify(session);
  assert.ok(!text.includes("LEAK"));
  assert.ok(!text.includes(SUB), "the Google subject must not be visible to browser code");
  for (const name of ["providerSubject", "provider", "emailVerified"]) assert.equal(name in session, false, name);
  assert.deepEqual((await (await fetch(`${base}/api/auth/session`)).json()), {}, "no cookie: empty session");
  // the session lifetime configured in authOptions (7 days) is what NextAuth reports
  const days = (Date.parse(session.expires) - Date.now()) / 86400000;
  assert.ok(days > 6.9 && days <= 7.01, `session expires in ${days} days`);
});

test("proxy.ts: feature pages redirect to sign-in without a verified session; the landing page stays public", { skip: !enabled }, async () => {
  for (const page of ["/autonomous", "/history", "/improve", "/twin", "/studio", "/studio/agents", "/studio/plans", "/studio/runs", "/studio/tools", "/studio/approvals", "/studio/templates", "/studio/plans/00000000-0000-0000-0000-000000000000"]) {
    const r = await fetch(`${base}${page}`, { redirect: "manual" });
    assert.ok([302, 303, 307, 308].includes(r.status), `${page} -> ${r.status}`);
    const location = new URL(r.headers.get("location"), base); // Next may answer a relative Location
    assert.equal(location.pathname, "/api/auth/signin");
    assert.equal(location.searchParams.get("callbackUrl"), page);
  }
  const unverified = await fetch(`${base}/history`, { redirect: "manual", headers: { cookie: await cookie({ emailVerified: false }) } });
  assert.ok([302, 303, 307, 308].includes(unverified.status));
  const forged = await fetch(`${base}/history`, { redirect: "manual", headers: { cookie: await cookie({}, "wrong-secret") } });
  assert.ok([302, 303, 307, 308].includes(forged.status));
  const allowed = await fetch(`${base}/history`, { redirect: "manual", headers: { cookie: await cookie() } });
  assert.equal(allowed.status, 200, "a verified session reaches the page");
  assert.equal((await fetch(`${base}/`, { redirect: "manual" })).status, 200, "the landing page is public");
  // API routes are not gated by the proxy (they enforce their own rules)
  assert.equal((await fetch(`${base}/api/auth/providers`)).status, 200);
});

test("every migrated page compiles and renders on the real server for a signed-in session (no hard-coded host)", { skip: !enabled }, async () => {
  const c = await cookie();
  for (const page of ["/", "/autonomous", "/history", "/improve", "/twin", "/studio", "/studio/agents", "/studio/agents/new", "/studio/plans", "/studio/runs", "/studio/tools", "/studio/tools/activity", "/studio/tools/math.calculate", "/studio/approvals", "/studio/approvals/00000000-0000-0000-0000-000000000000", "/studio/templates", "/studio/templates/sales-outreach", "/studio/plans/00000000-0000-0000-0000-000000000000", "/studio/runs/00000000-0000-0000-0000-000000000000", "/studio/agents/00000000-0000-0000-0000-000000000000"]) {
    const r = await fetch(`${base}${page}`, { redirect: "manual", headers: { cookie: c } });
    assert.equal(r.status, 200, `${page} -> ${r.status}\n${logs.slice(-600)}`);
    const html = await r.text();
    assert.ok(!/sslip\.io|65-0-30-8/.test(html), `${page} still embeds the old backend host`);
    assert.ok(!html.includes(SUB), `${page} must not embed the Google subject`);
  }
});
