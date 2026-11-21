// M1-H: source-level guards for the migrated frontend. These read the actual source files (no framework needed).

import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => readFileSync(path.join(FRONTEND, relative), "utf8");

function walk(dir) {
  return readdirSync(path.join(FRONTEND, dir), { withFileTypes: true }).flatMap((entry) => {
    const relative = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(relative);
    return /\.(ts|tsx)$/.test(entry.name) ? [relative] : [];
  });
}

const TOKEN_MODULES = ["lib/api-client.ts", "lib/agent-socket.ts", "lib/backend.ts", "lib/api-config.ts"];
const PAGES = ["app/page.tsx", "app/autonomous/page.tsx", "app/history/page.tsx", "app/improve/page.tsx", "app/twin/page.tsx"];

test("the token and client modules never touch browser storage, cookies or logging", () => {
  for (const file of TOKEN_MODULES) {
    const source = read(file).replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie", "console.", "window.name", "history.pushState", "location.hash"]) {
      assert.ok(!source.includes(forbidden), `${file} must not use ${forbidden}`);
    }
  }
});

test("no page or library hard-codes a backend host or sends an email/workspace identity", () => {
  const files = [...walk("app"), ...walk("lib")];
  for (const file of files) {
    const source = read(file);
    assert.ok(!/sslip\.io|65-0-30-8/.test(source), `${file} hard-codes the backend host`);
    assert.ok(!/user_email/.test(source), `${file} still references user_email`);
    // M6: choosing a workspace is allowed ONLY in the selection store, the client's injected selector and the wiring that connects them, the switcher and the team page.
    // The server verifies membership on every request, so a value chosen here is only a request.
    const WORKSPACE_FILES = ["lib/workspace.ts", "lib/api-client.ts", "lib/backend.ts", "app/studio/_team.tsx", "app/studio/team/page.tsx", "app/studio/join/page.tsx", "lib/studio/types.ts"];
    if (!WORKSPACE_FILES.includes(file.replaceAll("\\", "/"))) {
      assert.ok(!/x-workspace-id|workspace_id|workspaceId/i.test(source.replace(/\/\/.*$/gm, "")), `${file} must not select a workspace`);
    }
  }
});

test("pages call the backend only through the typed client and the socket client", () => {
  for (const file of PAGES) {
    const source = read(file);
    assert.ok(!/\bfetch\(/.test(source), `${file} must not call fetch directly`);
    assert.ok(!/new WebSocket\(/.test(source), `${file} must not open a WebSocket directly`);
    assert.ok(!/Authorization/.test(source), `${file} must not build auth headers itself`);
  }
  assert.match(read("app/autonomous/page.tsx"), /getApi\(\)\.runAutonomous/);
  assert.match(read("app/autonomous/page.tsx"), /getApi\(\)\.listTasks/);
  assert.match(read("app/autonomous/page.tsx"), /getApi\(\)\.scheduleTask/);
  assert.match(read("app/autonomous/page.tsx"), /getApi\(\)\.deleteTask/);
  assert.match(read("app/improve/page.tsx"), /getApi\(\)\.analyzeAgent/);
  assert.match(read("app/history/page.tsx"), /getApi\(\)\.getHistory/);
  assert.match(read("app/page.tsx"), /new AgentSocket\(/);
});

test("history uses the cursor from the response, keys entries by id and exposes 'Load more'", () => {
  const source = read("app/history/page.tsx");
  assert.match(source, /next_cursor/);
  assert.match(source, /getHistory\(\{ cursor: nextCursor \}\)/);
  assert.match(source, /key=\{entry\.id\}/);
  assert.match(source, /Load more/);
  assert.ok(!/offset/i.test(source), "no offset pagination");
  assert.ok(!/key=\{index\}/.test(source));
});

test("the client sends a workspace header only for a UUID the person selected, never a workspace field, and an email only to name who to add", () => {
  const code = read("lib/api-client.ts").replace(/\/\/.*$/gm, "");
  assert.equal((code.match(/X-Workspace-Id/g) ?? []).length, 1, "exactly one place sets the header");
  assert.match(code, /options\.workspace\?\.\(\)/);
  assert.match(code, /UUID\.test\(selected\)/);
  assert.ok(!/workspace_id|{ workspace/i.test(code), "no workspace field in any body (a workspace id only ever appears in a URL path or the header)");
  assert.ok(!/user_email/i.test(code), "no caller email in any request");
  assert.equal((code.match(/email:/g) ?? []).length, 4, "the only emails in request bodies name the person being added, invited, re-roled or removed (owner-only on the server)");
  assert.match(code, /addWorkspaceMember: .*\{ email: invitee, role \}/);
});

test("sign-out forgets the in-memory backend token", () => {
  const source = read("app/auth-button.tsx");
  assert.match(source, /backendTokens\.clear\(\)/);
  assert.ok(source.indexOf("backendTokens.clear()") < source.indexOf("signOut()"), "the token is cleared before the session ends");
});

test("the home page only opens a socket for a signed-in session and reconnects through the socket client", () => {
  const source = read("app/page.tsx");
  assert.match(source, /if \(!signedIn\) return;/);
  assert.match(source, /socket\.stop\(\)/);
  assert.ok(!/ws\.send\(/.test(source), "no raw socket sends remain in the page");
});
