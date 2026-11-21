// NextAuth callbacks for the Google provider (M1-G, D018/D019).
//
// Pure functions with no imports, so they are unit-tested directly under node:test.
//
// Identity rules:
//   * the Google `sub` is the provider subject and is carried EXACTLY as Google sent it (no trimming, no case
//     folding); `account.providerAccountId` must equal it or sign-in is refused;
//   * `email_verified === true` is mandatory (D018): an unverified Google account cannot sign in at all;
//   * the Google `sub` lives ONLY in the encrypted server-side NextAuth JWT (read by the token endpoint through
//     getToken); the session handed to browser code is an allow-list of what the UI needs (`user` and `expires`).
//     It never contains the subject, an access token, a key or any provider token (D028).

export const GOOGLE = "google";
export const SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60; // D018: NextAuth session lifetime is 7 days
const MAX_SUBJECT_CHARS = 255; // the backend's auth_identities.provider_subject is VARCHAR(255)
const MAX_EMAIL_CHARS = 320;

type Loose = Record<string, unknown> | null | undefined;

export function isValidSubject(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_SUBJECT_CHARS &&
    value === value.trim() &&
    // printable, no control characters (mirrors the backend verifier)
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

export function isValidEmail(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= MAX_EMAIL_CHARS && value.includes("@");
}

export function signInCallback({ account, profile }: { account?: Loose; profile?: Loose }): boolean {
  if (!account || !profile) return false;
  if (account.provider !== GOOGLE) return false;
  if (!isValidSubject(profile.sub)) return false;
  if (account.providerAccountId !== profile.sub) return false; // the two views of the Google subject must agree
  if (profile.email_verified !== true) return false;
  return isValidEmail(profile.email);
}

// `account` and `profile` are present only on the sign-in call; later calls just return the stored token.
export function jwtCallback({
  token,
  account,
  profile,
}: {
  token: Record<string, unknown>;
  account?: Loose;
  profile?: Loose;
}): Record<string, unknown> {
  if (account && profile) {
    token.provider = GOOGLE;
    token.providerSubject = profile.sub; // exact Google `sub`
    token.emailVerified = profile.email_verified === true;
    token.email = profile.email;
  }
  return token;
}

// Browser-visible session: rebuilt from an allow-list, so nothing else can ever leak into it by accident.
export function sessionCallback({
  session,
}: {
  session: Record<string, unknown>;
  token?: Record<string, unknown>;
}): Record<string, unknown> {
  const user = (session.user ?? {}) as Record<string, unknown>;
  return {
    user: { name: user.name ?? null, email: user.email ?? null, image: user.image ?? null },
    expires: session.expires,
  };
}
