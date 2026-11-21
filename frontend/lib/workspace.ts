// The workspace the person chose to work in (M6). It is only a REQUEST: the backend checks membership and role on every call, so a forged or stale value is refused (403),
// never honoured. Kept per browser tab (sessionStorage; every access guarded: storage can be missing or blocked) and announced to the page with a window event.

const KEY = "agentforge.workspace";
export const WORKSPACE_EVENT = "agentforge:workspace";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

let memory: string | null = null; // used when storage is unavailable

export function activeWorkspace(): string | null {
  try {
    const stored = typeof window !== "undefined" ? window.sessionStorage.getItem(KEY) : null;
    if (stored && UUID.test(stored)) return stored;
    if (stored === null) return memory;
  } catch {
    return memory;
  }
  return null;
}

/** Select a workspace (null = the personal one). Anything that is not a UUID selects the personal workspace. */
export function selectWorkspace(id: string | null): void {
  const value = id && UUID.test(id) ? id : null;
  memory = value;
  try {
    if (value) window.sessionStorage.setItem(KEY, value);
    else window.sessionStorage.removeItem(KEY);
  } catch {
    /* the in-memory value is used */
  }
  if (typeof window !== "undefined") window.dispatchEvent(new CustomEvent(WORKSPACE_EVENT, { detail: value }));
}
