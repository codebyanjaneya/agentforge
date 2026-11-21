// M1-0 baseline: ES256 JWT signing with Node's built-in crypto (no signing dependency).
//
// The access-token issuer (M1-G) will sign with node:crypto. These tests pin the facts it depends
// on: ES256 needs the raw IEEE-P1363 signature (r || s, 64 bytes for P-256), NOT Node's default
// DER encoding, and an independent verifier (jose) must accept the result.
//
// `jose` is only used here to verify independently. It is a transitive dependency of next-auth,
// not a direct dependency, and nothing in this baseline adds or changes any dependency.
// Everything is in memory: no files, no network, no secrets.

import assert from "node:assert/strict";
import crypto from "node:crypto";
import { test } from "node:test";

import { decodeProtectedHeader, importSPKI, jwtVerify } from "jose";

const b64u = (value) => Buffer.from(value).toString("base64url");

function newKeyPair() {
  return crypto.generateKeyPairSync("ec", { namedCurve: "P-256" });
}

function signingInput(header, payload) {
  return `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(payload))}`;
}

// ES256 per RFC 7518 §3.4: signature = r || s (IEEE P1363), 64 bytes.
function signEs256(header, payload, privateKey, dsaEncoding = "ieee-p1363") {
  const input = signingInput(header, payload);
  const signature = crypto.sign("sha256", Buffer.from(input), { key: privateKey, dsaEncoding });
  return { input, signature, token: `${input}.${b64u(signature)}` };
}

function claims(overrides = {}) {
  const iat = Math.floor(Date.now() / 1000);
  return {
    iss: "agentforge-web",
    aud: "agentforge-api",
    sub: "provider-subject-123",
    idp: "google",
    email: "user@example.com",
    email_verified: true,
    token_use: "access",
    iat,
    exp: iat + 600,
    jti: crypto.randomUUID(),
    ...overrides,
  };
}

const HEADER = { alg: "ES256", typ: "JWT", kid: "test-key-1" };
const VERIFY = { issuer: "agentforge-web", audience: "agentforge-api", algorithms: ["ES256"], clockTolerance: 30 };

async function joseKey(publicKey) {
  return importSPKI(publicKey.export({ type: "spki", format: "pem" }), "ES256");
}

test("ES256 requires the 64-byte IEEE-P1363 signature, not Node's default DER", () => {
  const { privateKey, publicKey } = newKeyPair();
  const input = Buffer.from("m1-0 baseline");

  const p1363 = crypto.sign("sha256", input, { key: privateKey, dsaEncoding: "ieee-p1363" });
  assert.equal(p1363.length, 64, "P-256 P1363 signature is exactly r||s = 64 bytes");
  assert.equal(crypto.verify("sha256", input, { key: publicKey, dsaEncoding: "ieee-p1363" }, p1363), true);

  // The default (DER) encoding is a different format and is NOT valid in a JWT.
  const der = crypto.sign("sha256", input, privateKey);
  assert.notEqual(der.length, 64, "DER signatures are variable length (typically 70-72 bytes)");
  assert.ok(der.length >= 8 && der.length <= 72);
  assert.equal(crypto.verify("sha256", input, publicKey, der), true, "DER verifies only as DER");
  assert.equal(
    crypto.verify("sha256", input, { key: publicKey, dsaEncoding: "ieee-p1363" }, der),
    false,
    "a DER signature must not verify as P1363",
  );
});

test("an independent verifier (jose) accepts a Node-signed ES256 token and sees the kid", async () => {
  const { privateKey, publicKey } = newKeyPair();
  const payload = claims();
  const { token } = signEs256(HEADER, payload, privateKey);

  const { payload: verified, protectedHeader } = await jwtVerify(token, await joseKey(publicKey), VERIFY);

  assert.deepEqual(protectedHeader, HEADER);
  assert.equal(decodeProtectedHeader(token).kid, "test-key-1");
  assert.deepEqual(verified, payload);
  assert.equal(verified.exp - verified.iat, 600, "access token lifetime is exactly 10 minutes");
  for (const name of ["iss", "aud", "sub", "idp", "email", "email_verified", "token_use", "iat", "exp", "jti"]) {
    assert.ok(name in verified, `claim ${name} present`);
  }
});

test("jose rejects the same token if it was signed with DER instead of P1363", async () => {
  const { privateKey, publicKey } = newKeyPair();
  const { token } = signEs256(HEADER, claims(), privateKey, "der");
  await assert.rejects(jwtVerify(token, await joseKey(publicKey), VERIFY), { code: "ERR_JWS_SIGNATURE_VERIFICATION_FAILED" });
});

test("tampered payload and wrong key are rejected by both Node and jose", async () => {
  const { privateKey, publicKey } = newKeyPair();
  const other = newKeyPair();
  const { input, signature, token } = signEs256(HEADER, claims(), privateKey);

  const [h, , s] = token.split(".");
  const forged = `${h}.${b64u(JSON.stringify(claims({ sub: "someone-else" })))}.${s}`;
  await assert.rejects(jwtVerify(forged, await joseKey(publicKey), VERIFY), { code: "ERR_JWS_SIGNATURE_VERIFICATION_FAILED" });
  assert.equal(
    crypto.verify("sha256", Buffer.from(forged.split(".").slice(0, 2).join(".")), { key: publicKey, dsaEncoding: "ieee-p1363" }, signature),
    false,
  );

  await assert.rejects(jwtVerify(token, await joseKey(other.publicKey), VERIFY), { code: "ERR_JWS_SIGNATURE_VERIFICATION_FAILED" });
  assert.equal(crypto.verify("sha256", Buffer.from(input), { key: other.publicKey, dsaEncoding: "ieee-p1363" }, signature), false);
});

test("algorithm pinning: a verifier pinned to ES256 refuses other algorithms and 'none'", async () => {
  const { publicKey } = newKeyPair();
  const key = await joseKey(publicKey);
  const unsigned = `${b64u(JSON.stringify({ alg: "none", typ: "JWT" }))}.${b64u(JSON.stringify(claims()))}.`;
  await assert.rejects(jwtVerify(unsigned, key, VERIFY), (error) => error.code === "ERR_JOSE_ALG_NOT_ALLOWED" || error.code === "ERR_JWS_INVALID");
  const hs256 = `${b64u(JSON.stringify({ alg: "HS256", typ: "JWT", kid: "test-key-1" }))}.${b64u(JSON.stringify(claims()))}.${b64u("x")}`;
  await assert.rejects(jwtVerify(hs256, key, VERIFY), { code: "ERR_JOSE_ALG_NOT_ALLOWED" });
});

test("30-second clock leeway: expired 29 s ago is accepted, expired 31 s ago is rejected", async () => {
  const { privateKey, publicKey } = newKeyPair();
  const key = await joseKey(publicKey);
  const now = Math.floor(Date.now() / 1000);

  const within = signEs256(HEADER, claims({ iat: now - 700, exp: now - 29 }), privateKey).token;
  await jwtVerify(within, key, VERIFY);

  const beyond = signEs256(HEADER, claims({ iat: now - 700, exp: now - 31 }), privateKey).token;
  await assert.rejects(jwtVerify(beyond, key, VERIFY), { code: "ERR_JWT_EXPIRED" });
});

test("issuer and audience mismatches are rejected", async () => {
  const { privateKey, publicKey } = newKeyPair();
  const key = await joseKey(publicKey);
  const wrongIssuer = signEs256(HEADER, claims({ iss: "someone-else" }), privateKey).token;
  const wrongAudience = signEs256(HEADER, claims({ aud: "another-api" }), privateKey).token;
  await assert.rejects(jwtVerify(wrongIssuer, key, VERIFY), { code: "ERR_JWT_CLAIM_VALIDATION_FAILED" });
  await assert.rejects(jwtVerify(wrongAudience, key, VERIFY), { code: "ERR_JWT_CLAIM_VALIDATION_FAILED" });
});

test("key serialisation matches what the backend config accepts: public SPKI PEM, private PKCS#8 PEM", () => {
  const { privateKey, publicKey } = newKeyPair();
  const publicPem = publicKey.export({ type: "spki", format: "pem" });
  const privatePem = privateKey.export({ type: "pkcs8", format: "pem" });
  assert.match(publicPem, /^-----BEGIN PUBLIC KEY-----\n/);
  assert.match(publicPem, /-----END PUBLIC KEY-----\n$/);
  assert.ok(!publicPem.toUpperCase().includes("PRIVATE KEY"), "the public half must never contain private key material");
  assert.match(privatePem, /^-----BEGIN PRIVATE KEY-----\n/);
  // A private key can be reloaded from PEM, as the issuer will do from its server-only environment.
  const reloaded = crypto.createPrivateKey(privatePem);
  const { signature } = signEs256(HEADER, claims(), reloaded);
  assert.equal(signature.length, 64);
});
