// API access-token issuer (M1-G, D018). Server-only: it reads the private signing key.
//
// The token contract is the one the backend verifier (backend/auth/tokens.py, M1-D) enforces:
//   header  { alg: "ES256", typ: "JWT", kid }
//   claims  iss, aud, sub (the exact Google subject), idp ("google"), email, email_verified (true),
//           token_use ("access"), jti, iat, exp = iat + 600
// Signing uses node:crypto only (no dependency); ES256 needs the raw IEEE-P1363 signature (M1-0 baseline).
//
// Nothing here logs or returns the key, a token's claims, or an error message from the crypto layer.

import crypto from "node:crypto";

export const DEFAULT_ISSUER = "agentforge-web";
export const DEFAULT_AUDIENCE = "agentforge-api";
export const TOKEN_LIFETIME_SECONDS = 600; // D018: exactly 10 minutes
const MAX_KID_CHARS = 128;
const MAX_SUBJECT_CHARS = 255;
const MAX_EMAIL_CHARS = 320;

export class TokenConfigError extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(reason);
    this.reason = reason;
  }
}

export type SigningConfig = {
  privateKey: crypto.KeyObject;
  kid: string;
  issuer: string;
  audience: string;
};

export type Identity = { provider: unknown; subject: unknown; email: unknown; emailVerified: unknown };

type Env = Record<string, string | undefined>;

const b64u = (value: string | Buffer) => Buffer.from(value).toString("base64url");

// Environment variables hold the PEM either with real newlines or with literal "\n" sequences.
function normalizePem(raw: string): string {
  return raw.includes("\\n") ? raw.replace(/\\n/g, "\n") : raw;
}

export function loadSigningConfig(env: Env): SigningConfig {
  const pem = env.BACKEND_TOKEN_PRIVATE_KEY;
  if (!pem || !pem.trim()) throw new TokenConfigError("missing_private_key");
  let privateKey: crypto.KeyObject;
  try {
    privateKey = crypto.createPrivateKey(normalizePem(pem));
  } catch {
    throw new TokenConfigError("unparseable_private_key"); // never include the crypto error text
  }
  if (privateKey.asymmetricKeyType !== "ec" || privateKey.asymmetricKeyDetails?.namedCurve !== "prime256v1") {
    throw new TokenConfigError("key_is_not_p256"); // ES256 means ECDSA over P-256
  }
  const kid = env.BACKEND_TOKEN_KID;
  if (!kid || kid.length > MAX_KID_CHARS || kid !== kid.trim() || /[\u0000-\u001f\u007f]/.test(kid)) {
    throw new TokenConfigError("invalid_kid");
  }
  const issuer = env.AUTH_JWT_ISSUER?.trim() || DEFAULT_ISSUER;
  const audience = env.AUTH_JWT_AUDIENCE?.trim() || DEFAULT_AUDIENCE;
  return { privateKey, kid, issuer, audience };
}

export class IdentityError extends Error {
  readonly reason: string;
  constructor(reason: string) {
    super(reason);
    this.reason = reason;
  }
}

// Refuses to sign anything the backend would reject, or that is not a verified Google identity.
function validateIdentity(identity: Identity): { subject: string; email: string } {
  const { provider, subject, email, emailVerified } = identity;
  if (provider !== "google") throw new IdentityError("unsupported_provider");
  if (
    typeof subject !== "string" ||
    !subject ||
    subject.length > MAX_SUBJECT_CHARS ||
    subject !== subject.trim() ||
    /[\u0000-\u001f\u007f]/.test(subject)
  ) {
    throw new IdentityError("invalid_subject");
  }
  if (emailVerified !== true) throw new IdentityError("email_not_verified");
  if (typeof email !== "string" || !email || email.length > MAX_EMAIL_CHARS || !email.includes("@")) {
    throw new IdentityError("invalid_email");
  }
  return { subject, email };
}

export function issueAccessToken(
  identity: Identity,
  config: SigningConfig,
  options: { now?: number; jti?: string } = {},
): { token: string; expiresAt: number } {
  const { subject, email } = validateIdentity(identity);
  const iat = Math.floor(options.now ?? Date.now() / 1000);
  const exp = iat + TOKEN_LIFETIME_SECONDS;
  const header = { alg: "ES256", typ: "JWT", kid: config.kid };
  const payload = {
    iss: config.issuer,
    aud: config.audience,
    sub: subject, // the exact Google subject, never normalized
    idp: "google",
    email,
    email_verified: true,
    token_use: "access",
    jti: options.jti ?? crypto.randomUUID(),
    iat,
    exp,
  };
  const input = `${b64u(JSON.stringify(header))}.${b64u(JSON.stringify(payload))}`;
  const signature = crypto.sign("sha256", Buffer.from(input), { key: config.privateKey, dsaEncoding: "ieee-p1363" });
  return { token: `${input}.${b64u(signature)}`, expiresAt: exp };
}

// ----------------------------------------------------------------------------------------------- endpoint

const NO_STORE = { "Cache-Control": "no-store", "Pragma": "no-cache" };

function respond(status: number, body: unknown, extra: Record<string, string> = {}): Response {
  return Response.json(body, { status, headers: { ...NO_STORE, ...extra } });
}

export function methodNotAllowed(): Response {
  return respond(405, { error: "method_not_allowed" }, { Allow: "POST" });
}

// The decoded, server-side NextAuth JWT (never sent to the browser): what NextAuth's `getToken` returns.
type TokenLike = { provider?: unknown; providerSubject?: unknown; emailVerified?: unknown; email?: unknown };

export type TokenEndpointDeps = {
  getToken: () => Promise<TokenLike | null>;
  env: Env;
  now?: () => number;
};

function expectedOrigin(request: Request, env: Env): string {
  const configured = env.NEXTAUTH_URL?.trim();
  if (configured) {
    try {
      return new URL(configured).origin;
    } catch {
      /* fall through to the request origin */
    }
  }
  return new URL(request.url).origin;
}

const ISSUE_WINDOW_SECONDS = 60;
const ISSUE_DEFAULT = 30;
const ISSUE_MAX_KEYS = 10000;
const issued = new Map<string, { window: number; count: number }>();

/** Whole seconds to wait, or null when this person may be issued a token now. `TOKEN_ISSUE_PER_MINUTE` (1..600) overrides the default; anything else falls back to the default. */
export function tokenIssuanceWait(subject: string, env: Env, nowMs: number): number | null {
  const raw = Number(env.TOKEN_ISSUE_PER_MINUTE);
  const limit = Number.isInteger(raw) && raw >= 1 && raw <= 600 ? raw : ISSUE_DEFAULT;
  const nowS = nowMs / 1000;
  const window = Math.floor(nowS / ISSUE_WINDOW_SECONDS);
  if (issued.size > ISSUE_MAX_KEYS) issued.clear(); // bounded memory
  const entry = issued.get(subject);
  const count = (entry && entry.window === window ? entry.count : 0) + 1;
  issued.set(subject, { window, count });
  return count <= limit ? null : Math.max(1, Math.ceil(ISSUE_WINDOW_SECONDS - (nowS % ISSUE_WINDOW_SECONDS)));
}

// POST only (D018). Order: method -> same origin -> server-side session token (401) -> verified identity (403) ->
// signing config (500).
export async function handleTokenRequest(request: Request, deps: TokenEndpointDeps): Promise<Response> {
  if (request.method !== "POST") return methodNotAllowed();

  const origin = request.headers.get("origin");
  const fetchSite = request.headers.get("sec-fetch-site");
  if (!origin || origin !== expectedOrigin(request, deps.env) || (fetchSite !== null && fetchSite !== "same-origin")) {
    return respond(403, { error: "cross_origin_request" });
  }

  let session: TokenLike | null;
  try {
    session = await deps.getToken();
  } catch {
    return respond(500, { error: "token_issuance_unavailable" });
  }
  if (!session || typeof session.providerSubject !== "string" || !session.email) {
    return respond(401, { error: "unauthenticated" });
  }

  // Sprint 3: a per-person ceiling on issuance (a token lasts 10 minutes, so a normal client asks a few times an hour). Per Next.js process and in memory on purpose: no cross-stack dependency;
  // unauthenticated callers never reach this point (they cost a cookie decrypt and get 401), so floods without a session belong to the edge proxy.
  const wait = tokenIssuanceWait(session.providerSubject, deps.env, deps.now ? deps.now() * 1000 : Date.now());
  if (wait !== null) return respond(429, { error: "too_many_requests" }, { "Retry-After": String(wait) });

  let config: SigningConfig;
  try {
    config = loadSigningConfig(deps.env);
  } catch {
    console.error("Access-token signing is not configured correctly");
    return respond(500, { error: "token_issuance_unavailable" });
  }

  try {
    const { token, expiresAt } = issueAccessToken(
      { provider: session.provider, subject: session.providerSubject, email: session.email, emailVerified: session.emailVerified },
      config,
      { now: deps.now ? deps.now() : undefined },
    );
    return respond(200, { token, expires_at: expiresAt });
  } catch (error) {
    if (error instanceof IdentityError) {
      return respond(403, { error: error.reason === "email_not_verified" ? "email_not_verified" : "forbidden" });
    }
    console.error("Access-token issuance failed");
    return respond(500, { error: "token_issuance_unavailable" });
  }
}
