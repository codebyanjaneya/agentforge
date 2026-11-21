// Sprint 5 polish: permission-aware UX. Visibility only: the backend authorises every request (see backend tests). Pure functions, no network.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import * as view from "../lib/studio/view.ts";

const OWNER = "olivia@example.com";
const ADMIN = "adam@example.com";
const MEMBER = "maya@example.com";
const approved = { status: "approved", latest_run: null, created_by: MEMBER };
const draft = { status: "draft", latest_run: null, created_by: MEMBER };
const running = { status: "running", retry_options: null, started_by: MEMBER };
const failed = { status: "failed", retry_options: { allowed: true }, started_by: MEMBER };

test("the plan's creator, whatever their workspace role, may approve, edit, start and cancel", () => {
  for (const creator of [OWNER, ADMIN, MEMBER]) {
    const a = view.planActions({ ...approved, created_by: creator }, creator);
    assert.deepEqual([a.canApprove, a.canRevise, a.canStartRun, a.canCancel, a.canWrite], [false, true, true, true, true]);
    const d = view.planActions({ ...draft, created_by: creator }, creator);
    assert.equal(d.canApprove, true);
  }
});

test("owner and admin do NOT get write actions on somebody else's plan (the backend's rule: only the creator writes)", () => {
  for (const viewer of [OWNER, ADMIN]) {
    const a = view.planActions(approved, viewer);
    assert.deepEqual([a.canApprove, a.canRevise, a.canStartRun, a.canCancel, a.canWrite], [false, false, false, false, false]);
    assert.equal(a.canValidate, true, "validating is read-only");
    assert.match(a.startBlockedReason, /Only maya@example\.com can change or run this plan/);
    assert.equal(view.planActions(draft, viewer).canApprove, false);
  }
});

test("a member who did not create the plan sees no write action either", () => {
  const a = view.planActions({ ...approved, created_by: OWNER }, MEMBER);
  assert.equal(a.canWrite, false);
  assert.equal(a.canStartRun, false);
});

test("email comparison ignores case and spaces", () => {
  assert.equal(view.mayWrite("  MAYA@Example.com ", MEMBER), true);
  assert.equal(view.mayWrite("other@example.com", MEMBER), false);
});

test("unknown identity on either side hides nothing: the server answers (older rows, session still loading)", () => {
  assert.equal(view.mayWrite(null, MEMBER), true);
  assert.equal(view.mayWrite(MEMBER, null), true);
  assert.equal(view.planActions({ status: "approved", latest_run: null }, MEMBER).canStartRun, true);
});

test("a cancelled plan offers nothing to anyone, and the existing state rules still apply to the creator", () => {
  const cancelled = view.planActions({ status: "cancelled", latest_run: null, created_by: MEMBER }, MEMBER);
  assert.deepEqual([cancelled.canApprove, cancelled.canRevise, cancelled.canStartRun, cancelled.canCancel], [false, false, false, false]);
  const busy = view.planActions({ status: "approved", latest_run: { status: "running" }, created_by: MEMBER }, MEMBER);
  assert.equal(busy.canStartRun, false);
  assert.match(busy.startBlockedReason, /already in progress/);
});

test("only the run's starter may cancel or retry; everybody else watches", () => {
  assert.deepEqual(view.runActions(running, MEMBER), { canCancel: true, canRetry: false, canWrite: true });
  assert.deepEqual(view.runActions(failed, MEMBER), { canCancel: false, canRetry: true, canWrite: true });
  for (const viewer of [OWNER, ADMIN]) {
    assert.deepEqual(view.runActions(running, viewer), { canCancel: false, canRetry: false, canWrite: false });
    assert.deepEqual(view.runActions(failed, viewer), { canCancel: false, canRetry: false, canWrite: false });
  }
});

test("approval decisions come from the server's own can_decide (owner/admin only) and the pages use the permission helpers", () => {
  const approvals = readFileSync(new URL("../app/studio/_approvals.tsx", import.meta.url), "utf8");
  assert.match(approvals, /if \(!item\.can_decide\)/, "the decision buttons exist only when the server says the viewer may decide");
  const plan = readFileSync(new URL("../app/studio/plans/[id]/page.tsx", import.meta.url), "utf8");
  assert.match(plan, /planActions\(p, me\)/);
  assert.match(plan, /actions\.canWrite/);
  const run = readFileSync(new URL("../app/studio/runs/[id]/page.tsx", import.meta.url), "utf8");
  assert.match(run, /runActions\(run, me\)/);
  assert.match(run, /runPerm\?\.canCancel/);
  assert.match(run, /runPerm\?\.canRetry/);
});
