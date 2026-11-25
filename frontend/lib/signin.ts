// Pure helpers for the branded sign-in page (kept free of React so node:test can exercise them).

const DEFAULT_DESTINATION = "/studio";

/**
 * Where to send a person after Google sign-in. Accepts a same-origin relative path ("/studio/plans?x=1") or an absolute URL on THIS origin (NextAuth hands the page an
 * absolute callbackUrl); anything else (another origin, protocol-relative "//host", backslashes, control characters, javascript:) falls back to the Studio. This is the
 * defence against an open redirect through ?callbackUrl=.
 */
export function safeCallbackUrl(raw: string | null | undefined, origin: string): string {
  if (!raw || raw.length > 2048 || /[\u0000-\u001f\u007f\\]/.test(raw)) return DEFAULT_DESTINATION;
  let url: URL;
  try {
    url = new URL(raw, origin);
  } catch {
    return DEFAULT_DESTINATION;
  }
  if (url.origin !== origin) return DEFAULT_DESTINATION;
  if (raw.startsWith("//")) return DEFAULT_DESTINATION;
  const target = `${url.pathname}${url.search}${url.hash}`;
  return target.startsWith("/") && !target.startsWith("//") ? target : DEFAULT_DESTINATION;
}

const MESSAGES: Record<string, string> = {
  AccessDenied: "That Google account can't be used: Google did not report a verified email address for it. Try another Google account.",
  Configuration: "Sign-in is not configured on this deployment. Tell whoever runs AgentForge.",
};

/** The sentence for a NextAuth error code (?error=...), or null when there is no error. Never echoes the code or any provider text. */
export function signInError(code: string | null | undefined): string | null {
  if (!code) return null;
  return MESSAGES[code] ?? "Sign-in did not complete. Try again.";
}
