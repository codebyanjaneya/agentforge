// Application wiring for the backend client (M1-H): one token manager per browser tab (memory only) and lazily
// created clients, so a missing or invalid NEXT_PUBLIC_API_BASE surfaces as a normal `ApiError("config")` when a
// call is made instead of crashing the page at import time.

import { ApiError, createApiClient, createTokenManager, type ApiClient } from "./api-client";
import { ApiConfigError, resolveApiBase, socketUrl } from "./api-config";
import { activeWorkspace } from "./workspace";

const tokens = createTokenManager({});
let client: ApiClient | undefined;

function base(): string {
  try {
    // NEXT_PUBLIC_* must be referenced literally so Next can inline it into the client bundle.
    return resolveApiBase(process.env.NEXT_PUBLIC_API_BASE, process.env.NODE_ENV === "production");
  } catch (error) {
    if (error instanceof ApiConfigError) throw new ApiError("config");
    throw error;
  }
}

export function getApi(): ApiClient {
  client ??= createApiClient({ baseUrl: base(), tokens, workspace: activeWorkspace });
  return client;
}

export function getSocketUrl(): string {
  return socketUrl(base());
}

export const backendTokens = {
  get: () => tokens.get(),
  invalidate: (token: string) => tokens.invalidate(token),
  /** Forget the in-memory token (sign-out, session loss). */
  clear: () => tokens.clear(),
};
