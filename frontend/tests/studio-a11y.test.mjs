// Sprint 5 closure: guards for accessibility defects found by the browser audit (see docs/accessibility.md). Source-level: the browser evidence is recorded in the docs.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const read = (p) => readFileSync(new URL("../" + p, import.meta.url), "utf8");

test("every confirmation dialog gets its OWN title id (a shared id gave every dialog the first one's accessible name)", () => {
  const ui = read("app/studio/_ui.tsx");
  assert.ok(!/id="st-dialog-title"/.test(ui) && !/aria-labelledby="st-dialog-title"/.test(ui), "no hard-coded dialog title id");
  assert.match(ui, /const titleId = useId\(\)/);
  assert.match(ui, /aria-labelledby=\{titleId\}/);
  assert.match(ui, /<h2 className="st-h2" id=\{titleId\}>/);
});

test("the mobile top bar is a landmark (header), so no content sits outside a region at narrow widths", () => {
  const layout = read("app/studio/layout.tsx");
  assert.match(layout, /<header className="st-topbar">/);
  assert.match(layout, /<main className="st-main" id="main" tabIndex={-1}>/);
  assert.ok(!/<div className="st-topbar">/.test(layout));
});

test("dialogs are native modal <dialog> elements (focus trap, inert background, Escape and focus restoration come from the platform) and Escape runs the cancel handler", () => {
  const ui = read("app/studio/_ui.tsx");
  assert.match(ui, /<dialog /);
  assert.match(ui, /showModal\(\)/);
  assert.match(ui, /onCancel=\{\(event\) => \{ event\.preventDefault\(\); if \(!busy\) onCancel\(\); \}\}/);
});

test("reduced motion is honoured in both style sheets", () => {
  assert.match(read("app/globals.css"), /prefers-reduced-motion: reduce/);
  assert.match(read("app/studio/studio.css"), /prefers-reduced-motion: reduce/);
});

test("the dependency graph is an image with a title and a text description of every dependency, a labelled keyboard-scrollable region, and state written as words", () => {
  const graph = read("app/studio/_graph.tsx");
  assert.match(graph, /role="img" aria-labelledby=\{`\$\{titleId\} \$\{descId\}`\}/);
  assert.match(graph, /<desc id=\{descId\}>/);
  assert.match(graph, /role="region" aria-label=\{`\$\{caption\}: scrollable`\} tabIndex=\{0\}/);
  assert.match(graph, /pending: "Waiting"[\s\S]*succeeded: "Succeeded"[\s\S]*failed: "Failed"/, "every state has a word, not only a colour");
  assert.match(read("app/studio/plans/[id]/page.tsx"), /<TaskGraph /);
  assert.match(read("app/studio/runs/[id]/page.tsx"), /<TaskGraph /);
});

test("the sidebar names where you are working, groups the places, and the mobile bar keeps the account reachable under More", () => {
  const layout = read("app/studio/layout.tsx");
  assert.match(layout, /<WorkspaceSwitcher \/>/);
  for (const group of ["Work", "Workforce", "Workspace"]) assert.match(layout, new RegExp(`label: "${group}"`));
  assert.match(layout, /<details\s+key={pathname}\s+className="st-more"\s+onKeyDown=[\s\S]*?"Escape"/);
  assert.match(layout, /href="#main" className="st-skip"/);
  assert.match(layout, /id="main" tabIndex=\{-1\}/);
  assert.match(layout, /st-more-account/);
  assert.match(read("app/studio/settings/page.tsx"), /<caption className="sr-only">[\s\S]*<th scope="col">Capability<\/th>/);
});
