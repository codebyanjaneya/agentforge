// M1-G: NextAuth callbacks (pure functions). MOCKED Google profiles: this proves our handling of the fields, not
// that Google really sends them.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  GOOGLE,
  SESSION_MAX_AGE_SECONDS,
  isValidEmail,
  isValidSubject,
  jwtCallback,
  sessionCallback,
  signInCallback,
} from "../lib/auth-callbacks.ts";

const SUB = "112233445566778899001";
const account = (extra = {}) => ({ provider: "google", providerAccountId: SUB, type: "oauth", ...extra });
const profile = (extra = {}) => ({ sub: SUB, email: "user@example.com", email_verified: true, name: "U", ...extra });

test("session lifetime is exactly 7 days", () => {
  assert.equal(SESSION_MAX_AGE_SECONDS, 7 * 24 * 60 * 60);
  assert.equal(GOOGLE, "google");
});

test("sign-in accepts only a verified Google identity whose two subject views agree", () => {
  assert.equal(signInCallback({ account: account(), profile: profile() }), true);
  const refused = [
    { account: account({ provider: "github" }), profile: profile() },
    { account: account({ provider: undefined }), profile: profile() },
    { account: account({ providerAccountId: "999" }), profile: profile() },
    { account: account({ providerAccountId: undefined }), profile: profile() },
    { account: account(), profile: profile({ sub: undefined }) },
    { account: account(), profile: profile({ sub: "" }) },
    { account: account({ providerAccountId: " x " }), profile: profile({ sub: " x " }) },
    { account: account(), profile: profile({ email_verified: false }) },
    { account: account(), profile: profile({ email_verified: "true" }) },
    { account: account(), profile: profile({ email_verified: undefined }) },
    { account: account(), profile: profile({ email: "" }) },
    { account: account(), profile: profile({ email: "no-at-sign" }) },
    { account: account(), profile: profile({ email: undefined }) },
    { account: undefined, profile: profile() },
    { account: account(), profile: undefined },
    { account: null, profile: null },
  ];
  for (const input of refused) assert.equal(signInCallback(input), false, JSON.stringify(input));
});

test("the exact Google sub is stored in the JWT, not a normalized copy", () => {
  for (const sub of [SUB, "AbC-xyz_123", "0001", "ünïcode"]) {
    const token = jwtCallback({ token: { sub: "other-default" }, account: account({ providerAccountId: sub }), profile: profile({ sub }) });
    assert.equal(token.providerSubject, sub);
    assert.equal(token.provider, "google");
    assert.equal(token.emailVerified, true);
    assert.equal(token.email, "user@example.com");
  }
});

test("later jwt calls (no account/profile) keep the stored identity unchanged", () => {
  const stored = { provider: "google", providerSubject: SUB, emailVerified: true, email: "user@example.com" };
  const again = jwtCallback({ token: { ...stored }, account: undefined, profile: undefined });
  assert.deepEqual(again, stored);
  // a refresh carrying a different profile without an account must not overwrite the subject
  const noAccount = jwtCallback({ token: { ...stored }, profile: profile({ sub: "attacker" }) });
  assert.equal(noAccount.providerSubject, SUB);
});

test("the browser session is an allow-list: no Google subject, no token, no key, nothing else", () => {
  const token = {
    provider: "google", providerSubject: SUB, emailVerified: true, email: "user@example.com",
    accessToken: "SHOULD-NEVER-APPEAR", access_token: "GOOGLE-ACCESS", id_token: "GOOGLE-ID", refresh_token: "GOOGLE-REFRESH",
    backendToken: "API-TOKEN", privateKey: "KEY",
  };
  const session = sessionCallback({
    session: { user: { name: "U", email: "user@example.com", image: "https://img/x.png", extra: "NOPE", id: SUB }, expires: "x", providerSubject: SUB, accessToken: "SHOULD-NEVER-APPEAR" },
    token,
  });
  assert.deepEqual(session, { user: { name: "U", email: "user@example.com", image: "https://img/x.png" }, expires: "x" });
  const text = JSON.stringify(session);
  for (const secret of [SUB, "SHOULD-NEVER-APPEAR", "GOOGLE-ACCESS", "GOOGLE-ID", "GOOGLE-REFRESH", "API-TOKEN", "KEY\"", "providerSubject", "NOPE"]) {
    assert.ok(!text.includes(secret), secret);
  }
  assert.deepEqual(Object.keys(session).sort(), ["expires", "user"]);
});

test("the subject stays in the server-side JWT even though the session no longer carries it", () => {
  const token = jwtCallback({ token: {}, account: account(), profile: profile() });
  assert.equal(token.providerSubject, SUB);
  assert.equal("providerSubject" in sessionCallback({ session: { user: {}, expires: "x" }, token }), false);
});

test("a minimal or empty session still yields a safe shape", () => {
  assert.deepEqual(sessionCallback({ session: {} }), { user: { name: null, email: null, image: null }, expires: undefined });
});

test("validators", () => {
  assert.equal(isValidSubject("x".repeat(255)), true);
  assert.equal(isValidSubject("x".repeat(256)), false);
  for (const bad of ["", " a", "a ", "a\nb", 5, null, undefined]) assert.equal(isValidSubject(bad), false);
  assert.equal(isValidEmail("a@b"), true);
  for (const bad of ["", "ab", 5, null, "a@".padEnd(321, "x")]) assert.equal(isValidEmail(bad), false);
});
