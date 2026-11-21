// Backend URL configuration (M1-H, D028: no hard-coded hostnames, validated environment configuration).
//
// `NEXT_PUBLIC_API_BASE` must be an origin: https (http only for localhost), no credentials, path, query or
// fragment. Unset is a development convenience (http://localhost:8000); in production it is a configuration
// error and the client fails closed instead of guessing a host.

export class ApiConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ApiConfigError";
  }
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
export const DEV_DEFAULT_BASE = "http://localhost:8000";

export function resolveApiBase(raw: string | undefined, production: boolean): string {
  let value = raw?.trim();
  if (!value) {
    if (production) throw new ApiConfigError("NEXT_PUBLIC_API_BASE is not set");
    value = DEV_DEFAULT_BASE;
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new ApiConfigError("NEXT_PUBLIC_API_BASE is not a valid URL");
  }
  const local = LOCAL_HOSTS.has(url.hostname);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local)) {
    throw new ApiConfigError("NEXT_PUBLIC_API_BASE must use https (http is allowed only for localhost)");
  }
  if (url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new ApiConfigError("NEXT_PUBLIC_API_BASE must be a bare origin (no credentials, path, query or fragment)");
  }
  return url.origin;
}

export function socketUrl(base: string): string {
  const url = new URL(base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = "/ws";
  return url.toString();
}
