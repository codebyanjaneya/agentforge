// P0 of the frontend programme: the public landing page, the branded sign-in, the first-run checklist, the command palette and the foundation stylesheet.
// Pure logic is tested directly; structure and accessibility semantics are guarded at the source level (the real browser behaviour is covered by ops/e2e: journeys,
// keyboard, axe and the accessibility-tree check).

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

import { setupSteps } from "../lib/studio/onboarding.ts";
import { safeCallbackUrl, signInError } from "../lib/signin.ts";

const read = (p) => readFileSync(new URL("../" + p, import.meta.url), "utf8");
const ORIGIN = "https://app.example.com";

// ------------------------------------------------------------------------------------------------ sign-in: no open redirect

test("the sign-in callback only ever returns a same-origin path", () => {
  assert.equal(safeCallbackUrl("/studio/plans?x=1#a", ORIGIN), "/studio/plans?x=1#a");
  assert.equal(safeCallbackUrl("https://app.example.com/studio/runs/abc", ORIGIN), "/studio/runs/abc"); // NextAuth hands the page an absolute URL on THIS origin
  for (const hostile of ["https://evil.example/steal", "//evil.example", "///evil.example", "/\\evil.example", "javascript:alert(1)", "data:text/html,x", "http://app.example.com.evil.example/", "/studio\r\nLocation: x", "", null, undefined, "x".repeat(3000)]) {
    assert.equal(safeCallbackUrl(hostile, ORIGIN), "/studio", String(hostile).slice(0, 40));
  }
});

test("sign-in errors are plain sentences and never echo provider text or the code", () => {
  assert.equal(signInError(null), null);
  assert.match(signInError("AccessDenied"), /verified email/);
  assert.match(signInError("Configuration"), /not configured/);
  const other = signInError("<script>alert(1)</script>");
  assert.equal(other, "Sign-in did not complete. Try again.");
  assert.ok(!/script|alert/.test(other));
});

test("NextAuth uses the branded page for sign-in and for errors, and nothing else about authentication changed", () => {
  const auth = read("lib/auth.ts");
  assert.match(auth, /pages: \{ signIn: "\/signin", error: "\/signin" \}/);
  for (const kept of ["GoogleProvider", "strategy: \"jwt\"", "signInCallback", "jwtCallback", "sessionCallback", "SESSION_MAX_AGE_SECONDS"]) assert.ok(auth.includes(kept), kept);
  const page = read("app/signin/page.tsx");
  assert.match(page, /<Suspense/); // useSearchParams on a prerendered page needs a boundary (Next 16 docs)
  assert.match(page, /robots: \{ index: false \}/);
  assert.match(read("app/signin/signin-card.tsx"), /safeCallbackUrl\(/);
});

// ------------------------------------------------------------------------------------------------ first run: derived from facts, never invented

const plan = (status) => ({ id: "p-" + status, status });
const state = (f) => setupSteps(f).steps.map((s) => `${s.id}:${s.state}`).join(" ");

test("the checklist reads real workspace facts and the first open step is the current one", () => {
  assert.equal(state({ agentCount: 0, plans: [], lastRun: null }), "workforce:current objective:todo approve:todo follow:todo");
  assert.equal(state({ agentCount: 3, plans: [], lastRun: null }), "workforce:done objective:current approve:todo follow:todo");
  assert.equal(state({ agentCount: 3, plans: [plan("draft")], lastRun: null }), "workforce:done objective:done approve:current follow:todo");
  assert.equal(state({ agentCount: 3, plans: [plan("approved")], lastRun: null }), "workforce:done objective:done approve:done follow:current");
  assert.equal(state({ agentCount: 3, plans: [plan("approved")], lastRun: { id: "r", status: "running" } }), "workforce:done objective:done approve:done follow:current");
  const finished = setupSteps({ agentCount: 3, plans: [plan("approved")], lastRun: { id: "r", status: "succeeded" } });
  assert.deepEqual([finished.done, finished.complete], [4, true]);
  assert.ok(finished.steps.every((s) => s.state === "done"));
});

test("a run that exists proves the steps before it, and a failed run still counts as read-the-result", () => {
  const s = setupSteps({ agentCount: 1, plans: [], lastRun: { id: "r", status: "failed" } });
  assert.deepEqual([s.steps[1].state, s.steps[2].state, s.steps[3].state], ["done", "done", "done"]);
});

test("the checklist states what an agent is and offers all three ways to get one", () => {
  const steps = setupSteps({ agentCount: 0, plans: [], lastRun: null }).steps;
  assert.match(steps[0].detail, /An agent is a specialist: written instructions, the tools it is allowed to use, and a model/);
  const ui = read("app/studio/_firstrun.tsx");
  for (const label of ["Add the starter workforce", "Create your own agent", "Start from a template", "Write an objective", "Review the plan", "Open the run"]) assert.ok(ui.includes(label), label);
  assert.match(ui, /aria-current=\{step\.state === "current" \? "step" : undefined\}/);
  assert.match(ui, /<ol className="st-steps">/);
  assert.ok(!/%|progress/i.test(ui.replace(/progressbar/g, "")), "no invented percentage or progress bar");
  const home = read("app/studio/page.tsx");
  assert.match(home, /action\.kind === "approve" \|\| setup\.complete/); // a waiting approval is never hidden behind the checklist
});

test("example objectives are real buttons that fill the box, move focus into it and announce it", () => {
  const c = read("app/studio/_composer.tsx");
  assert.match(c, /role="group" aria-labelledby="examples-label"/);
  assert.match(c, /Try an example/);
  assert.match(c, /type="button" className="st-example"/);
  assert.match(c, /el\.setSelectionRange\(example\.length, example\.length\)/);
  assert.match(c, /Example added to the objective box/);
  assert.match(c, /role="status" aria-live="polite"/);
});

// ------------------------------------------------------------------------------------------------ landing: public, honest, accessible

test("the public root is the landing page; the legacy dashboard moved to /classic unchanged", () => {
  const landing = read("app/page.tsx");
  assert.ok(!landing.includes('"use client"'), "the landing page is a Server Component so it can export metadata");
  assert.match(landing, /export const metadata/);
  assert.equal((landing.match(/<h1\b/g) || []).length, 1);
  assert.match(landing, /id="main"/);
  assert.match(landing, /className="st-skip"/);
  assert.ok(existsSync(new URL("../app/classic/page.tsx", import.meta.url)));
  assert.match(read("app/classic/page.tsx"), /new AgentSocket\(/); // the legacy behaviour is still there
  for (const f of ["app/autonomous/page.tsx", "app/history/page.tsx", "app/improve/page.tsx", "app/twin/page.tsx"]) assert.ok(read(f).includes('href="/classic"'), f);
  assert.ok(read("app/studio/layout.tsx").includes('href="/classic"'));
  assert.match(read("proxy.ts"), /matcher: \["\/autonomous/); // the protected routes are unchanged, and neither / nor /signin nor /classic is gated
  assert.ok(!/"\/classic|"\/signin|matcher: \[[^\]]*"\/"/.test(read("proxy.ts").split("matcher")[1]));
});

test("the landing page invents nothing: the example is labelled an illustration and there are no metrics, customers or testimonials", () => {
  const landing = read("app/page.tsx");
  assert.match(landing, /Illustration, not live data/);
  assert.ok(!/testimonial|trusted by|customers love|\b\d{2,}\s?(%|x faster|users|teams|companies)/i.test(landing), "no fabricated social proof or numbers");
  assert.ok(!/<button/.test(landing), "no fake, non-functional buttons: the example approval is described, not drawn as a control");
  assert.match(landing, /Honest limits/);
  assert.match(landing, /No independent security audit yet/);
});

// ------------------------------------------------------------------------------------------------ command palette

test("the command palette follows the ARIA combobox and listbox pattern inside a native modal dialog", () => {
  const p = read("app/studio/_palette.tsx");
  assert.match(p, /<dialog/);
  assert.match(p, /role="combobox"/);
  assert.match(p, /aria-activedescendant=/);
  assert.match(p, /aria-controls=/);
  assert.match(p, /role="listbox"/);
  assert.match(p, /role="option"/);
  assert.match(p, /aria-selected=\{selected\}/);
  assert.match(p, /role="status" aria-live="polite"/); // the result count is announced
  assert.match(p, /aria-keyshortcuts="Control\+K Meta\+K"/);
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "Enter"]) assert.ok(p.includes(`"${key}"`), key);
  assert.ok(!/from "(cmdk|kbar|@radix|react-aria|downshift)/.test(p), "no dependency added for the palette");
  assert.match(read("app/studio/layout.tsx"), /<CommandPalette \/>/);
});

// ------------------------------------------------------------------------------------------------ foundation stylesheet and React Bits setup

test("the foundation stylesheet honours reduced motion and forced colors and adds no glass, glow or neon", () => {
  const css = read("app/studio/foundation.css");
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(css, /@media \(forced-colors: active\)/);
  assert.ok(!/backdrop-filter|text-shadow|neon/i.test(css), "no glass or neon");
  assert.ok(!/box-shadow:[^;]*(rgba?\(\s*255[^)]*\)|var\(--st-ember\))[^;]*\d+px\s+\d+px\s+\d+px\s+\d+px/.test(css.replace(/0 0 0 3px var\(--st-ember-soft\)/g, "")), "no glow");
});

test("React Bits is configured by hand (no shadcn init), with the licence kept, no MCP, and nothing vendored that was not adopted", () => {
  const cfg = JSON.parse(read("components.json"));
  assert.equal(cfg.registries["@react-bits"], "https://reactbits.dev/r/{name}.json");
  assert.equal(cfg.tailwind.cssVariables, false);
  assert.ok(!existsSync(new URL("../.mcp.json", import.meta.url)) && !existsSync(new URL("../../.mcp.json", import.meta.url)), "no MCP configuration");
  assert.match(read("components/react-bits/LICENSE.md"), /MIT \+ Commons Clause/);
  assert.match(read("components/react-bits/NOTICE.md"), /No React Bits component is vendored here at present/);
  const deps = JSON.parse(read("package.json")).dependencies;
  assert.ok(!("motion" in deps) && !("gsap" in deps), "no animation dependency was added");
});

// ------------------------------------------------------------------------------------------------ review corrections (P0 visual review)

test("the public copy makes no absolute claim about secrets and is explicit about email and the live model", () => {
  const landing = read("app/page.tsx");
  const home = read("lib/studio/home.ts");
  for (const [name, text] of [["landing", landing], ["home.ts (also shown on the Studio overview)", home]]) {
    assert.ok(!/Nothing in it contains a secret value/.test(text), name + " still claims nothing contains a secret value");
  }
  assert.match(home, /Sensitive values are redacted where AgentForge recognizes them, but redaction is not guaranteed to catch every one/);
  assert.match(landing, /Email sending is limited to the provider's test recipients by default, and delivery to real recipients has not yet been verified/);
  assert.match(landing, /Demo mode uses a scripted model, and a successful run against a live model has not yet been verified/);
  assert.match(landing, /No independent security audit yet/); // the existing tone is kept
});

test("forced colors: the list markers are drawn characters with different shapes, not backgrounds that forced colors removes", () => {
  const css = read("app/studio/foundation.css");
  const block = css.slice(css.indexOf("@media (forced-colors: active)"));
  assert.match(block, /\.lp-list li::before \{ content: "✓"/);
  assert.match(block, /\.lp-list\[data-tone="todo"\] li::before \{ content: "○"/);
  assert.match(block, /\.si-points li::before \{ content: "•"/);
  assert.match(block, /content: "✓" \/ ""/); // the CSS alt-text form: the glyph is not read aloud
  assert.match(block, /background: none; border: 0/);
  // the normal-mode dots are untouched
  assert.match(css, /\.lp-list li::before \{ content: ""; position: absolute; left: 0; top: 0\.62em; width: 0\.5rem; height: 0\.5rem; border-radius: 50%; background: var\(--st-good\); \}/);
});

test("the mobile bottom navigation stays, and reserves room for itself so it never covers content or a focused control", () => {
  const css = read("app/studio/foundation.css");
  assert.match(css, /html:has\(\.st-tabbar\) \{ scroll-padding-bottom: calc\(6\.5rem \+ env\(safe-area-inset-bottom\)\); \}/);
  assert.match(css, /\.st-main \{ padding-bottom: calc\(7rem \+ env\(safe-area-inset-bottom\)\); \}/);
  assert.match(css, /\.st-tabbar > a \.st-count \{ position: absolute;/); // the badge no longer makes the bar taller
  assert.match(read("app/studio/layout.tsx"), /<nav className="st-tabbar" aria-label="Studio, compact">/); // not removed
});

test("sign-in gets a little more presence on wide screens and the narrow layout is not touched", () => {
  const css = read("app/studio/foundation.css");
  assert.match(css, /@media \(min-width: 40rem\) \{\s*\.si-card \{ max-width: 30rem; padding: 2\.4rem 2\.5rem; gap: 1\.3rem; \}/);
  assert.match(css, /^\.si-card \{ width: 100%; max-width: 27rem;/m); // the base (mobile) rule is unchanged
});

test("the run history caption makes no absolute claim that it holds no values", () => {
  const ui = read("app/studio/_approvals.tsx");
  assert.ok(!/Nothing here contains a value/.test(ui), "the old absolute caption is back");
  assert.match(ui, /This history shows what happened, including the recorded actions and their details\. Sensitive values are redacted where recognized, but redaction is not guaranteed to catch every one\./);
  // no user-facing source anywhere (except the legacy /classic pages) may say a record contains no secret or no value
  for (const f of ["app/page.tsx", "lib/studio/home.ts", "app/studio/_approvals.tsx", "app/studio/_run.tsx", "app/studio/_tools.tsx", "app/studio/_dispatch.tsx", "app/studio/_usage.tsx"]) {
    assert.ok(!/contains a secret|contains no secret|never contain[s]? a (secret|value)|Nothing here contains/i.test(read(f)), f);
  }
});
