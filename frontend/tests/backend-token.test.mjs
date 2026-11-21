// M1-G: the ES256 access-token issuer and the POST /api/backend-token handler.
//
// Everything is in memory with ephemeral keys; the Google identity is MOCKED (a fake session object), so none
// of this proves a real Google sign-in. The cross-language tests run the real backend verifier (M1-D) on
// tokens minted here.

import assert from "node:assert/strict";
import crypto from "node:crypto";
import { test } from "node:test";

import { decodeProtectedHeader, importSPKI, jwtVerify } from "jose";

import {
  DEFAULT_AUDIENCE,
  DEFAULT_ISSUER,
  IdentityError,
  TOKEN_LIFETIME_SECONDS,
  TokenConfigError,
  handleTokenRequest,
  issueAccessToken,
  loadSigningConfig,
  methodNotAllowed,
} from "../lib/backend-token.ts";
import { backendPythonAvailable, backendVerify, decodePart, envWith, newSigningKey } from "./token-helpers.mjs";

const GOOGLE_SUB = "112233445566778899001";
const identity = (overrides = {}) => ({ provider: "google", subject: GOOGLE_SUB, email: "user@example.com", emailVerified: true, ...overrides });
const key = newSigningKey();
const config = () => loadSigningConfig(envWith(key.privatePem));
const NOW = Math.floor(Date.now() / 1000);

// --------------------------------------------------------------------------------------- issuance and claims

test("the token has the exact header and claims the backend contract requires", () => {
  const { token, expiresAt } = issueAccessToken(identity(), config(), { now: NOW, jti: "jti-fixed" });
  assert.deepEqual(decodePart(token, 0), { alg: "ES256", typ: "JWT", kid: "test-kid-1" });
  assert.deepEqual(decodePart(token, 1), {
    iss: "agentforge-web",
    aud: "agentforge-api",
    sub: GOOGLE_SUB,
    idp: "google",
    email: "user@example.com",
    email_verified: true,
    token_use: "access",
    jti: "jti-fixed",
    iat: NOW,
    exp: NOW + 600,
  });
  assert.equal(expiresAt, NOW + 600);
  assert.equal(TOKEN_LIFETIME_SECONDS, 600);
  assert.equal(DEFAULT_ISSUER, "agentforge-web");
  assert.equal(DEFAULT_AUDIENCE, "agentforge-api");
  assert.equal(token.split(".").length, 3);
  assert.equal(Buffer.from(token.split(".")[2], "base64url").length, 64, "IEEE-P1363 r||s");
});

test("each token gets a fresh jti and the real clock is used by default", () => {
  const a = issueAccessToken(identity(), config());
  const b = issueAccessToken(identity(), config());
  assert.notEqual(decodePart(a.token, 1).jti, decodePart(b.token, 1).jti);
  assert.match(decodePart(a.token, 1).jti, /^[0-9a-f-]{36}$/);
  assert.ok(Math.abs(decodePart(a.token, 1).iat - Math.floor(Date.now() / 1000)) <= 2);
});

test("the Google subject is preserved exactly (case, digits, length)", () => {
  for (const sub of [GOOGLE_SUB, "AbC-xyz_123", "9".repeat(255), "ünïcode-sub", "0001"]) {
    const { token } = issueAccessToken(identity({ subject: sub }), config());
    assert.equal(decodePart(token, 1).sub, sub);
  }
});

test("only a verified Google identity with a valid subject and email can be signed", () => {
  const bad = [
    [{ provider: "github" }, "unsupported_provider"], [{ provider: undefined }, "unsupported_provider"], [{ provider: "Google" }, "unsupported_provider"],
    [{ subject: "" }, "invalid_subject"], [{ subject: " padded " }, "invalid_subject"], [{ subject: 12345 }, "invalid_subject"],
    [{ subject: undefined }, "invalid_subject"], [{ subject: "x".repeat(256) }, "invalid_subject"], [{ subject: "a\u0000b" }, "invalid_subject"],
    [{ emailVerified: false }, "email_not_verified"], [{ emailVerified: "true" }, "email_not_verified"], [{ emailVerified: undefined }, "email_not_verified"],
    [{ email: "" }, "invalid_email"], [{ email: "no-at-sign" }, "invalid_email"], [{ email: undefined }, "invalid_email"], [{ email: 5 }, "invalid_email"],
  ];
  for (const [override, reason] of bad) {
    assert.throws(() => issueAccessToken(identity(override), config()), (error) => error instanceof IdentityError && error.reason === reason, JSON.stringify(override));
  }
});

// --------------------------------------------------------------------------------------- signature and verifiers

test("an independent verifier (jose) accepts the token; wrong key, tampering and other algorithms fail", async () => {
  const { token } = issueAccessToken(identity(), config(), { now: NOW });
  const verify = { issuer: "agentforge-web", audience: "agentforge-api", algorithms: ["ES256"], clockTolerance: 30 };
  const publicKey = await importSPKI(key.publicPem, "ES256");
  const { payload } = await jwtVerify(token, publicKey, verify);
  assert.equal(payload.sub, GOOGLE_SUB);
  assert.equal(decodeProtectedHeader(token).kid, "test-kid-1");

  const other = await importSPKI(newSigningKey().publicPem, "ES256");
  await assert.rejects(jwtVerify(token, other, verify), { code: "ERR_JWS_SIGNATURE_VERIFICATION_FAILED" });
  const [h, , s] = token.split(".");
  const forged = `${h}.${Buffer.from(JSON.stringify({ ...payload, sub: "someone-else" })).toString("base64url")}.${s}`;
  await assert.rejects(jwtVerify(forged, publicKey, verify), { code: "ERR_JWS_SIGNATURE_VERIFICATION_FAILED" });
  await assert.rejects(jwtVerify(token, publicKey, { ...verify, issuer: "other" }), { code: "ERR_JWT_CLAIM_VALIDATION_FAILED" });
  await assert.rejects(jwtVerify(token, publicKey, { ...verify, audience: "other" }), { code: "ERR_JWT_CLAIM_VALIDATION_FAILED" });
});

test("the REAL backend verifier (M1-D, Python) accepts a token minted here and returns the exact subject", { skip: !backendPythonAvailable }, () => {
  const { token } = issueAccessToken(identity(), config());
  const result = backendVerify(token, key.publicPem);
  assert.equal(result.error, undefined);
  assert.equal(result.claims.provider, "google");
  assert.equal(result.claims.subject, GOOGLE_SUB);
  assert.equal(result.claims.email, "user@example.com");
  assert.equal(result.claims.expires_at - result.claims.issued_at, 600);
  for (const sub of ["AbC-xyz_123", "ünïcode-sub"]) {
    assert.equal(backendVerify(issueAccessToken(identity({ subject: sub }), config()).token, key.publicPem).claims.subject, sub);
  }
});

test("the backend verifier rejects what the contract says it must", { skip: !backendPythonAvailable }, () => {
  const { token } = issueAccessToken(identity(), config());
  const other = newSigningKey();
  assert.equal(backendVerify(token, other.publicPem).error !== undefined, true, "wrong public key");
  assert.equal(backendVerify(token, key.publicPem, "other-kid").error !== undefined, true, "unknown kid");
  assert.equal(backendVerify(token, key.publicPem, "test-kid-1", { issuer: "someone-else" }).error !== undefined, true);
  assert.equal(backendVerify(token, key.publicPem, "test-kid-1", { audience: "another-api" }).error !== undefined, true);
  const expired = issueAccessToken(identity(), config(), { now: NOW - 3600 }).token;
  assert.equal(backendVerify(expired, key.publicPem).error !== undefined, true, "expired token");
  const [h, , s] = token.split(".");
  const forged = `${h}.${Buffer.from(JSON.stringify({ ...decodePart(token, 1), sub: "someone-else" })).toString("base64url")}.${s}`;
  assert.equal(backendVerify(forged, key.publicPem).error !== undefined, true, "tampered payload");
  assert.equal(backendVerify(token + "x", key.publicPem).error !== undefined, true, "corrupted signature");
  // configured issuer / audience overrides are honoured by the issuer and still verify
  const custom = loadSigningConfig(envWith(key.privatePem, { AUTH_JWT_ISSUER: "iss-x", AUTH_JWT_AUDIENCE: "aud-x" }));
  const customToken = issueAccessToken(identity(), custom).token;
  assert.equal(backendVerify(customToken, key.publicPem, "test-kid-1", { issuer: "iss-x", audience: "aud-x" }).error, undefined);
  assert.equal(backendVerify(customToken, key.publicPem).error !== undefined, true);
});

// --------------------------------------------------------------------------------------- signing configuration

test("signing configuration fails closed and never echoes key material", () => {
  const rsa = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 }).privateKey.export({ type: "pkcs8", format: "pem" });
  const p384 = crypto.generateKeyPairSync("ec", { namedCurve: "P-384" }).privateKey.export({ type: "pkcs8", format: "pem" });
  const cases = [
    [{}, "missing_private_key"], [{ BACKEND_TOKEN_PRIVATE_KEY: "  ", BACKEND_TOKEN_KID: "k" }, "missing_private_key"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: "not a pem", BACKEND_TOKEN_KID: "k" }, "unparseable_private_key"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: key.publicPem, BACKEND_TOKEN_KID: "k" }, "unparseable_private_key"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: rsa, BACKEND_TOKEN_KID: "k" }, "key_is_not_p256"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: p384, BACKEND_TOKEN_KID: "k" }, "key_is_not_p256"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: key.privatePem }, "invalid_kid"], [{ BACKEND_TOKEN_PRIVATE_KEY: key.privatePem, BACKEND_TOKEN_KID: "" }, "invalid_kid"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: key.privatePem, BACKEND_TOKEN_KID: " k" }, "invalid_kid"],
    [{ BACKEND_TOKEN_PRIVATE_KEY: key.privatePem, BACKEND_TOKEN_KID: "k".repeat(129) }, "invalid_kid"],
  ];
  for (const [env, reason] of cases) {
    assert.throws(() => loadSigningConfig(env), (error) => {
      assert.ok(error instanceof TokenConfigError && error.reason === reason, `${reason} vs ${error.reason}`);
      assert.ok(!String(error.message).includes("PRIVATE KEY") && !String(error.message).includes("MIG"));
      return true;
    });
  }
});

test("a PEM with literal \\n sequences (as hosting environments often store it) loads", () => {
  const escaped = key.privatePem.trim().replace(/\n/g, "\\n");
  const { token } = issueAccessToken(identity(), loadSigningConfig({ BACKEND_TOKEN_PRIVATE_KEY: escaped, BACKEND_TOKEN_KID: "k" }));
  assert.equal(token.split(".").length, 3);
});

// --------------------------------------------------------------------------------------- the endpoint

const ORIGIN = "https://app.example.com";
// the decoded SERVER-SIDE NextAuth JWT (what getToken returns); the browser session never carries these fields
const goodSession = (extra = {}) => ({ provider: "google", providerSubject: GOOGLE_SUB, emailVerified: true, email: "user@example.com", ...extra });
const request = (init = {}, url = `${ORIGIN}/api/backend-token`) =>
  new Request(url, { method: "POST", headers: { origin: ORIGIN, "sec-fetch-site": "same-origin", ...(init.headers ?? {}) }, ...(init.method ? { method: init.method } : {}) });
const deps = (session, env = envWith(key.privatePem, { NEXTAUTH_URL: ORIGIN }), extra = {}) => ({ getToken: async () => session, env, ...extra });

function assertNoStore(response) {
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("pragma"), "no-cache");
}

test("a valid session gets a token, with no-store headers and the expiry", async () => {
  const response = await handleTokenRequest(request(), deps(goodSession(), undefined, { now: () => NOW }));
  assert.equal(response.status, 200);
  assertNoStore(response);
  const body = await response.json();
  assert.deepEqual(Object.keys(body).sort(), ["expires_at", "token"]);
  assert.equal(body.expires_at, NOW + 600);
  assert.equal(decodePart(body.token, 1).sub, GOOGLE_SUB);
  if (backendPythonAvailable) assert.equal(backendVerify(body.token, key.publicPem).claims.subject, GOOGLE_SUB);
});

test("no session is 401; nothing is signed", async () => {
  for (const session of [null, {}, { email: "u@example.com" }, { providerSubject: GOOGLE_SUB }, { providerSubject: 5, email: "u@example.com" }]) {
    const response = await handleTokenRequest(request(), deps(session));
    assert.equal(response.status, 401, JSON.stringify(session));
    assertNoStore(response);
    assert.deepEqual(await response.json(), { error: "unauthenticated" });
  }
});

test("an unverified email, another provider or a bad subject is refused with 403", async () => {
  for (const [extra, error] of [[{ emailVerified: false }, "email_not_verified"], [{ emailVerified: undefined }, "email_not_verified"],
                                [{ provider: "github" }, "forbidden"], [{ providerSubject: " x " }, "forbidden"]]) {
    const response = await handleTokenRequest(request(), deps(goodSession(extra)));
    assert.equal(response.status, 403, JSON.stringify(extra));
    assert.deepEqual(await response.json(), { error });
    assertNoStore(response);
  }
});

test("only POST: other methods get 405 with Allow: POST and no-store", async () => {
  for (const method of ["GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]) {
    const response = await handleTokenRequest(new Request(`${ORIGIN}/api/backend-token`, { method }), deps(goodSession()));
    assert.equal(response.status, 405, method);
    assert.equal(response.headers.get("allow"), "POST");
    assertNoStore(response);
  }
  const direct = methodNotAllowed();
  assert.equal(direct.status, 405);
  assert.equal(direct.headers.get("allow"), "POST");
});

test("same-origin is required: missing, foreign or cross-site requests are 403 even with a valid session", async () => {
  const headers = (h) => new Request(`${ORIGIN}/api/backend-token`, { method: "POST", headers: h });
  for (const h of [{}, { origin: "https://evil.example" }, { origin: "null" }, { origin: ORIGIN + ".evil.example" }, { origin: "http://app.example.com" },
                   { origin: ORIGIN, "sec-fetch-site": "cross-site" }, { origin: ORIGIN, "sec-fetch-site": "same-site" }, { origin: ORIGIN, "sec-fetch-site": "none" }]) {
    const response = await handleTokenRequest(headers(h), deps(goodSession()));
    assert.equal(response.status, 403, JSON.stringify(h));
    assert.deepEqual(await response.json(), { error: "cross_origin_request" });
    assertNoStore(response);
  }
  // a session lookup must not even happen for a foreign origin
  let looked = false;
  await handleTokenRequest(headers({ origin: "https://evil.example" }), { getToken: async () => { looked = true; return goodSession(); }, env: envWith(key.privatePem, { NEXTAUTH_URL: ORIGIN }) });
  assert.equal(looked, false);
  // with no NEXTAUTH_URL the request's own origin is the reference
  const noUrl = await handleTokenRequest(request(), deps(goodSession(), envWith(key.privatePem)));
  assert.equal(noUrl.status, 200);
});

test("missing or invalid signing configuration is a sanitized 500 and issues nothing", async () => {
  for (const env of [{ NEXTAUTH_URL: ORIGIN }, { NEXTAUTH_URL: ORIGIN, BACKEND_TOKEN_PRIVATE_KEY: "garbage", BACKEND_TOKEN_KID: "k" }]) {
    const response = await handleTokenRequest(request(), deps(goodSession(), env));
    assert.equal(response.status, 500);
    assertNoStore(response);
    const text = await response.text();
    assert.deepEqual(JSON.parse(text), { error: "token_issuance_unavailable" });
    assert.ok(!/garbage|PRIVATE|key/i.test(text.replace("token_issuance_unavailable", "")));
  }
  const broken = await handleTokenRequest(request(), { getToken: async () => { throw new Error("db password hunter2"); }, env: envWith(key.privatePem, { NEXTAUTH_URL: ORIGIN }) });
  assert.equal(broken.status, 500);
  assert.ok(!(await broken.text()).includes("hunter2"));
});

test("no response or error text contains the private key or the token's claims on failure paths", async () => {
  const privateBody = key.privatePem.split("\n")[1];
  for (const session of [null, goodSession({ emailVerified: false })]) {
    const text = await (await handleTokenRequest(request(), deps(session))).text();
    assert.ok(!text.includes(privateBody) && !text.includes("BEGIN") && !text.includes(GOOGLE_SUB) && !text.includes("user@example.com"));
  }
});

// ------------------------------------------------------------------------------------ sprint 3: issuance ceiling per person

test("issuance is limited per person per minute, answers 429 with Retry-After, and is per subject", async () => {
  const env = envWith(key.privatePem, { NEXTAUTH_URL: ORIGIN, TOKEN_ISSUE_PER_MINUTE: "3" });
  const T = 1_700_000_000 + 7; // seconds; a fixed clock inside one window
  const statuses = [];
  for (let i = 0; i < 5; i++) statuses.push((await handleTokenRequest(request(), deps(goodSession({ providerSubject: "limit-subject-a" }), env, { now: () => T }))).status);
  assert.deepEqual(statuses, [200, 200, 200, 429, 429]);
  const blocked = await handleTokenRequest(request(), deps(goodSession({ providerSubject: "limit-subject-a" }), env, { now: () => T }));
  assert.equal(blocked.status, 429);
  assert.ok(Number(blocked.headers.get("retry-after")) >= 1);
  assertNoStore(blocked);
  assert.deepEqual(await blocked.json(), { error: "too_many_requests" });
  const other = await handleTokenRequest(request(), deps(goodSession({ providerSubject: "limit-subject-b" }), env, { now: () => T }));
  assert.equal(other.status, 200); // another person is unaffected
  const later = await handleTokenRequest(request(), deps(goodSession({ providerSubject: "limit-subject-a" }), env, { now: () => T + 61 }));
  assert.equal(later.status, 200); // the next window
});

test("an invalid TOKEN_ISSUE_PER_MINUTE falls back to the default instead of disabling the limit", async () => {
  const T = 1_700_001_000;
  for (const bad of ["0", "-1", "abc", "100000", ""]) {
    const env = envWith(key.privatePem, { NEXTAUTH_URL: ORIGIN, TOKEN_ISSUE_PER_MINUTE: bad });
    let last = 200;
    for (let i = 0; i < 31; i++) last = (await handleTokenRequest(request(), deps(goodSession({ providerSubject: `fallback-${bad}` }), env, { now: () => T }))).status;
    assert.equal(last, 429, `value "${bad}" must keep the default limit of 30`);
  }
});
