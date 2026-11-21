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
  assert.match(layout, /<main className="st-main" id="main">/);
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
