// M3-C: source-level guards for the Studio. They read the real source files (no framework needed) and pin the properties
// that must hold for every future edit: the typed client is the only network path, nothing token-like is persisted, no
// ownership identity is sent, plan creation/approval never starts a run, polling is a single loop, and tools stay unavailable.

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const FRONTEND = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relative) => readFileSync(path.join(FRONTEND, relative), "utf8");
const strip = (source) => source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "").replace(/\{\/\*[\s\S]*?\*\/\}/g, "");

function walk(dir) {
  return readdirSync(path.join(FRONTEND, dir), { withFileTypes: true }).flatMap((entry) => {
    const relative = path.join(dir, entry.name);
    if (entry.isDirectory()) return walk(relative);
    return /\.(ts|tsx)$/.test(entry.name) ? [relative.replaceAll("\\", "/")] : [];
  });
}

const STUDIO = [...walk("app/studio"), ...walk("lib/studio")];
const PAGES = STUDIO.filter((f) => f.endsWith("page.tsx"));

test("the Studio has every planned route and page", () => {
  for (const route of ["studio/settings", "studio/join", "studio", "studio/agents", "studio/agents/new", "studio/agents/[id]", "studio/templates", "studio/templates/[id]", "studio/plans", "studio/plans/[id]", "studio/runs", "studio/runs/[id]", "studio/tools", "studio/tools/[id]", "studio/tools/activity", "studio/approvals", "studio/approvals/[id]", "studio/team"]) {
    assert.ok(existsSync(path.join(FRONTEND, "app", route, "page.tsx")), `app/${route}/page.tsx`);
  }
  assert.ok(existsSync(path.join(FRONTEND, "app/studio/layout.tsx")));
  assert.equal(PAGES.length, 18); // sprint 5: + /studio/join (invitation acceptance); sprint 6: + /studio/settings
   // + M6: /studio/team;  // + M4-B: /studio/tools, /studio/tools/[id], /studio/tools/activity; + M4-C: /studio/approvals, /studio/approvals/[id]
});

test("the Studio reaches the backend only through the typed client: no fetch, WebSocket or hand-built auth headers", () => {
  for (const file of STUDIO) {
    const source = strip(read(file));
    assert.ok(!/(?<![.\w])fetch\(/.test(source), `${file} must not call the global fetch directly`);
    assert.ok(!/new WebSocket\(/.test(source), `${file} must not open a WebSocket`);
    assert.ok(!/Authorization|Bearer /.test(source), `${file} must not build auth headers`);
    assert.ok(!/XMLHttpRequest|axios/.test(source), `${file} must not use another HTTP client`);
  }
  for (const file of PAGES.filter((f) => !/studio\/(layout)/.test(f))) assert.match(read(file), /getApi\(\)|ObjectiveComposer/, `${file} uses the shared client`);
});

test("nothing is persisted in the browser by the Studio (no backend token, no storage, no cookies, no URL state)", () => {
  for (const file of STUDIO) {
    const source = strip(read(file));
    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie", "history.pushState", "location.hash", "window.name", "console."]) {
      assert.ok(!source.includes(forbidden), `${file} must not use ${forbidden}`);
    }
    // sprint 5: an invitation token is not the backend token; only the Team form (shows it once) and the join page (submits it) handle it
    if (!["app/studio/_members.tsx", "app/studio/join/page.tsx"].includes(file.replaceAll("\\", "/"))) assert.ok(!/backendTokens|createTokenManager|\.token\b/.test(source), `${file} must not touch the backend token`);
  }
});

test("no ownership identity and no hard-coded host in the Studio", () => {
  for (const file of STUDIO) {
    const source = strip(read(file));
    if (file.replaceAll("\\", "/") !== "app/studio/join/page.tsx" && file.replaceAll("\\", "/") !== "lib/studio/types.ts") assert.ok(!/user_id|userId|workspace_id|workspaceId|x-workspace-id|user_email/i.test(source), `${file} must not carry an ownership identity`); // join selects the joined workspace, nothing more
    assert.ok(!/sslip\.io|65-0-30-8|https?:\/\/(?!react\.dev)/.test(source.replace(/xmlns="[^"]+"/g, "")), `${file} must not hard-code a host`);
  }
});

test("a run is started ONLY by startRunFlow from an explicit, confirmed action; creating and approving a plan never start one", () => {
  for (const file of STUDIO.filter((f) => f !== "lib/studio/flows.ts")) {
    assert.ok(!/\.startRun\(/.test(strip(read(file))), `${file} must start runs only through startRunFlow`);
  }
  const flows = strip(read("lib/studio/flows.ts"));
  assert.equal((flows.match(/\.startRun\(/g) ?? []).length, 1, "exactly one call site");
  assert.match(flows, /startRunFlow[\s\S]*status !== "approved"/);
  const plan = read("app/studio/plans/[id]/page.tsx");
  assert.equal((plan.match(/startRunFlow\(/g) ?? []).length, 1);
  // the approve confirmation calls approvePlanFlow and nothing that starts work
  const approve = plan.slice(plan.indexOf('open={dialog === "approve"}'), plan.indexOf('open={dialog === "start"}'));
  assert.match(approve, /approvePlanFlow/);
  assert.ok(!/startRun|router\.push/.test(approve), "approval must not start or navigate to a run");
  assert.match(approve, /Approving does not start any work/);
  // the start confirmation is a separate dialog the user must confirm
  assert.match(plan.slice(plan.indexOf('open={dialog === "start"}')), /Start this run now\?/);
  // the composer only generates a plan
  const composer = read("app/studio/_composer.tsx");
  assert.ok(!/startRun|approvePlan/.test(composer));
  assert.match(composer, /Nothing runs until you approve it and start it/);
});

test("run progress comes from ONE poller per page with no ad-hoc fetch loops", () => {
  const run = read("app/studio/runs/[id]/page.tsx");
  assert.equal((run.match(/createPoller</g) ?? []).length, 1);
  assert.equal((run.match(/getApi\(\)\.getRun\(/g) ?? []).length, 1, "the poller is the only reader of the run");
  assert.match(run, /instance\.stop\(\)/, "the poller is torn down when the page unmounts");
  assert.match(run, /visibilitychange/);
  for (const file of PAGES.concat(STUDIO.filter((f) => f.endsWith("_run.tsx") || f.endsWith("_ui.tsx")))) {
    const source = strip(read(file));
    // setInterval only drives clocks (elapsed time), never requests
    for (const match of source.matchAll(/setInterval\(([\s\S]*?)\n\s*\}?,?\s*\d+\)/g)) {
      assert.ok(!/getApi\(\)/.test(match[1]), `${file}: an interval must not issue requests`);
    }
  }
  assert.ok(!/setInterval\([^)]*getApi/.test(strip(read("app/studio/runs/page.tsx"))));
});

test("tools are chosen from the registry only: the picker lists what the backend reports, offers nothing it cannot run, and sends only ids", () => {
  const fields = read("app/studio/_agent-form.tsx");
  assert.match(fields, /ToolPicker/);
  assert.ok(!/Tools are not available yet/.test(fields), "the 'no tools exist' notice is gone");
  const picker = read("app/studio/_tools.tsx");
  assert.match(picker, /getApi\(\)\.listTools\(\)/, "the options come from the real catalog");
  assert.match(picker, /!\(tool\.latest\.executable \|\| tool\.latest\.grantable\)/, "a tool the backend says cannot be granted is never selectable (an approval-gated one is: each call still asks)");
  assert.match(picker, /MAX_TOOLS_PER_AGENT/);
  assert.ok(!/<input[^>]*type="text"|<textarea/.test(picker), "no free-text tool entry: a tool cannot be typed in");
  const forms = read("lib/studio/forms.ts");
  assert.match(forms, /allowed_tools: \[\.\.\.form\.tools\]/);
  assert.match(forms, /the backend re-validates and pins every one/);
});

test("the tool screens are read-only: no tool is ever run from the browser and nothing is invented", () => {
  for (const file of STUDIO.filter((f) => /tools|_tools/.test(f))) {
    const source = strip(read(file));
    assert.ok(!/\.(startRun|retryRun|runAutonomous|approvePlan|cancelRun)\(/.test(source), `${file} must not start or change anything`);
    assert.ok(!/Math\.random|faker|lorem/i.test(source), `${file} must not fabricate data`);
    assert.ok(!/dangerouslySetInnerHTML|innerHTML/.test(source), `${file} must render stored input and output as text only`);
  }
  assert.match(read("app/studio/tools/activity/page.tsx"), /getApi\(\)\.listToolInvocations/);
  assert.match(read("app/studio/runs/[id]/page.tsx"), /getApi\(\)\.getRunToolInvocations/);
});

test("dangerous or irreversible actions are confirmed in an accessible dialog", () => {
  const ui = read("app/studio/_ui.tsx");
  assert.match(ui, /<dialog/);
  assert.match(ui, /showModal\(\)/);
  assert.match(ui, /onCancel/);
  for (const [file, texts] of [
    ["app/studio/plans/[id]/page.tsx", ["Approve revision", "Start this run now?", "Cancel this plan?"]],
    ["app/studio/runs/[id]/page.tsx", ["Cancel this run?"]],
    ["app/studio/agents/[id]/page.tsx", ["Archive this agent?", "Restore version"]],
  ]) {
    const source = read(file);
    for (const text of texts) assert.ok(source.includes(text), `${file} confirms: ${text}`);
  }
});

test("the layout requires a session, the proxy gates /studio and the classic dashboard links to it", () => {
  const layout = read("app/studio/layout.tsx");
  assert.match(layout, /useSession/);
  assert.match(layout, /status === "authenticated"/);
  assert.match(read("proxy.ts"), /"\/studio\/:path\*"/);
  assert.match(read("app/page.tsx"), /href="\/studio"/);
  for (const href of ["/studio", "/studio/plans", "/studio/runs", "/studio/approvals", "/studio/agents", "/studio/tools", "/studio/templates"]) assert.ok(layout.includes(`"${href}"`), href);
});

test("accessibility basics are present: landmarks, focus styles, reduced motion, labelled controls", () => {
  const css = read("app/studio/studio.css");
  assert.match(css, /:focus-visible/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(read("app/studio/layout.tsx"), /<nav[^>]*aria-label/);
  assert.match(read("app/studio/layout.tsx"), /aria-current/);
  assert.match(read("app/studio/_ui.tsx"), /role=\{role \?\?/);
  assert.match(read("app/studio/_run.tsx"), /aria-expanded/);
  assert.match(read("app/studio/runs/[id]/page.tsx"), /aria-live="polite"/);
  // every form control in the shared field component is tied to a label
  assert.match(read("app/studio/_ui.tsx"), /htmlFor=\{htmlFor\}/);
});
