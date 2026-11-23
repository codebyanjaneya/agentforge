# Design review, browser acceptance and permission review (2026-11-22)

Evidence level: **verified in a real browser against the real app and backend in an isolated, test-only harness** (scripted model, simulated send tool). Nothing here is evidence for a live model, a real email, a staging or a production deployment. No billable call and no email was made in this phase.

## 1. The test-only harness (`ops/e2e/`)

* `stack.py start|stop` builds and starts the **unmodified** app: FastAPI (port 8040, SQLite, `APP_ENV=test`, `AGENTFORGE_DEMO_MODE=1`) and the Next server (port 3040). It never uses 3010/8010, never reads `backend/.env` values (everything is passed explicitly, `DATABASE_URL` included) and refuses to start when its ports are taken.
* **Authentication is not bypassed or weakened.** Fresh throw-away ES256 keys and a fresh `NEXTAUTH_SECRET` are generated into `ops/e2e/.run` (git-ignored) on every start and deleted by `stop`. A browser carries a NextAuth session cookie encrypted with that throw-away secret; the unmodified app decodes it, issues its normal ES256 access token, and the unmodified backend verifies it against the throw-away public key. A cookie made for this stack is worthless against any other deployment (different secret and key), and **no application source was changed for it** (nothing to disable in production or staging; Google sign-in code is untouched, and the frontend auth tests still pass).
* `seed.mjs` creates owner, admin, member, starter and stranger **through the real API** (owner creates the workspace and adds the others with the real role rules). The database is a disposable SQLite file removed on the next start.
* `journeys.mjs` (23 steps), `shots.mjs` (every page at 1440, 1280, 390, 320 with an overflow check and console/network capture).

## 2. Visual review: what was wrong and what changed

Screenshots reviewed: 16 pages at 4 widths, the mobile More sheet, before and after. Curated pairs are in `docs/screens/`.

| Finding (found in the screenshots) | Page | Fix |
|---|---|---|
| "Waiting for you" card squeezed the request title into a one-word-per-line column | Overview (desktop) | the status/countdown now sits under the title in the narrow aside; title clamps to two lines |
| "New objective" wrapped onto two lines in the top bar | every page, desktop | `white-space: nowrap` |
| Dependency graph small and lost in empty space | plan and run pages | larger nodes (232x64) and type (13.5/12 px) |
| 3 px horizontal overflow at 320 px | run pages | run id wraps (`overflow-wrap: anywhere`) |
| Usage tables crushed to `not repo rted` at narrow widths | run page, 320 | tables keep a readable width and scroll inside their labelled region |
| Stage rail wasted width; the suspended banner was a sliver beside its button | run page, 320 | smaller rail and nodes, banner wraps the button under the text |
| "Side effects" heading wrapped beside its caption | run page | heading does not wrap from 640 px |
| Account row in the More sheet wrapped awkwardly | mobile | wraps cleanly, link does not break |

**Found by the browser journeys (not by eye):**

* After a failed submit on the agent forms the error was off-screen and focus stayed on the button, so keyboard and screen-reader users got no signal (WCAG 3.3.1). Focus now moves to the first invalid field (`useFocusFirstError`), verified in the browser (it moves again on the next invalid field).
* **Defect, not fixed here:** agent slugs are unique per *workspace* but agents are private per *user*. In a shared workspace only the first person can add the starter workforce; for everyone else `POST /starter-workforce` answers 200 with `created: false` for all three, they get no agents, and planning then answers "add the starter workforce" (a loop). Fixing it needs a decision (per-user slugs, or a workspace-shared workforce) and a migration, so it is recorded, not patched.

Not claimed: axe was not re-run on the changed pages in this phase, and no screen reader was used.

## 3. Browser journeys (all against the real app; critical steps checked in the API)

23 of 23 passed on the final stack. The only console entry is the expected 409 of an unacknowledged retry.

Owner objective -> plan (row checked) -> graph -> approve (explicit, revision recorded) -> start run -> exact-action approval pending; member reads plan, run and approval with no enabled write action, and the API refuses the member's decision (403); admin approves in the browser, the approval is consumed once, the run succeeds, a second decision is refused (409); tool calls readable by owner, admin and member; usage; cancel from the UI (status `cancelled`); retry refused without acknowledgement and accepted with it (attempt 2); member cannot cancel the owner's run; stranger gets 403 for a foreign workspace, 404 for ids, empty tool-call list, and no data in the UI; agent create (with focus-on-error) and save as version 2; member does not see the owner's private agents; invitation created in the UI, a never-signed-in person joins on a 390 px screen, membership verified, reuse refused; tool registry, tool page (delivery guarantee) and dispatch activity; settings; templates; mobile More sheet; garbage session cookie shows the sign-in gate; invalid bearer token 401.

**Not run in a browser in this phase** (stated, not hidden): reconciliation of an unknown outcome (producing a real unknown outcome needs a process crash mid-dispatch; it is covered by the existing backend tests and drills), the plan *revision* editor, the wrong-e-mail invitation refusal, the admin-role journeys beyond approving, workspace role change/removal in the UI, notifications badge timing, and a live-model or Resend journey (not authorised in this phase).

## 4. Permission review: tool-invocation reads

**Finding.** Commit `4b2cf47` made tool-invocation reads workspace-wide without a recorded decision. Checked against the repository: that matches the documented sprint-5 rule (`flow.md` 25.36: "every member of a workspace READS its plans, runs, usage, events and tool invocations; only the creator/starter WRITES"), and the code for plans, runs, executions, usage and events was already shared-read (`_seen`). The tool-invocation store was the one reader still per-user, which made a run page show *no tool calls* to the admin who has to approve and reconcile them. So the change fixed a code/policy mismatch; it did not widen anything beyond the documented policy. D046 and D050 are not touched (approving and reconciling stay owner/admin). The module docstring still described the old rule and has been corrected (comment only).

**Matrix (as implemented and tested)**

| Data / action | Owner | Admin | Member (not the starter) | Starter / creator | Non-member / other workspace |
|---|---|---|---|---|---|
| Plans, runs, events, task results, usage (read) | yes | yes | yes | yes | no (403 / indistinguishable from absent) |
| Tool calls of a run: list with 140-char previews, detail with the stored (redacted, bounded) input and output | yes | yes | yes | yes | no |
| Dispatch/side-effect state of a call | yes | yes | yes | yes | no |
| Approval request and its exact values (read) | yes | yes | yes (bounded context, no e-mail of the requester) | yes | no |
| Approve / reject (D050, Proposed, unchanged) | yes | yes | **no** (403) | no unless owner/admin | no |
| Reconcile an unknown outcome | yes | yes | no | no unless owner/admin | no |
| Revise / approve plan, start / retry / cancel run | creator/starter only | creator/starter only | creator/starter only | yes | no |
| Custom agent definitions | own only | own only | own only | own only | no |
| Create invitations, change roles, remove members | yes | no | no | no | no |

**Sensitivity.** Inputs and outputs are stored redacted (secrets) and bounded, and the error text is fixed. A member can therefore read, for example, the recipient, subject and body that an owner's agent asked to send. That is the same information the approval page and the run's final output already show every member, so the tool-read change adds no new class of data. It is personal data of third parties (a recipient's address) and of the owner's business, and that is a **product decision for the owner**.

**Recommendation.** (1) Keep the current policy: it is consistent. Tightening tool reads alone would be security theatre, because the same content is in the run result and the approval page; if member-to-member privacy is wanted, change plans, runs, tool calls and approvals together (for example a workspace setting "members see only their own runs", with owner/admin always seeing all). (2) Record the shared-read policy as a numbered decision (offered as D075 in `docs/decision-confirmation.md`) so it is the owner's confirmed rule, not an implementation detail. (3) If the owner wants less exposure with no loss of function: show members previews only and keep full input/output detail for owner, admin and the starter. That is a small, separate change and was **not** made.

## 5. Remaining blockers

* No live-model success (needs a served `MODEL_NAME` and explicit approval); no real Resend send (D073); no real alert receiver (D074); no human screen-reader pass.
* (resolved 2026-11-23, see section 6) the starter-workforce slug collision.
* (D075 accepted in principle 2026-11-23, wording awaits final confirmation).

---

# Update 2026-11-23: the remaining gaps, the starter-workforce fix, D075

Same evidence level as above: real browser, real app and backend, isolated throw-away stack (`ops/e2e/`), scripted model, simulated send. No live-model call, no email, no staging or production access, no deployment. Google sign-in and production authentication are untouched (the harness only mints cookies for its own throw-away secret; no application auth code changed).

## 6. The starter-workforce defect: resolution

**Cause (found by reading the ownership model first).** `custom_agents` rows are private to `(user_id, workspace_id)`: every read and every `resolve` filters on both. Only the slug uniqueness (`UNIQUE (workspace_id, slug)`) and the "slug taken" check were workspace-wide. The first member to add the starter workforce took the three slugs; for everyone else the insert hit the constraint, `POST /starter-workforce` answered 200 with `created: false` for all three, they had no agents, and planning answered "add the starter workforce" (a loop).

**Fix (D076).** Migration `0018_agent_slug_per_user`: `UNIQUE (workspace_id, slug)` becomes `UNIQUE (workspace_id, user_id, slug)`. A pure relaxation: existing rows satisfy it, nothing is rewritten, no agent changes owner, **no personal agent becomes visible to anyone else**. The duplicate check uses the same scope. The 200-agent cap is now per user so one member cannot exhaust it for the others. `downgrade()` refuses (changing nothing) while two users of a workspace share a slug.

**Verified.**

| Check | Result |
|---|---|
| Migration on SQLite (rebuild with foreign keys deferred): agents and versions identical after upgrade, second user's same slug allowed, same user's refused, FK to the rebuilt table survives, models equal the migrated schema, downgrade refusal and recovery | 3 tests pass |
| Same migration on **PostgreSQL 16** (disposable container, token-marked, port 54399): old constraint name refused before, new name after, data untouched, downgrade refused with the clash and unchanged, downgrade and re-upgrade after removing it | passes |
| Eight members of one workspace add the same three slugs at the same instant (24 threads, barrier) | no error, each has exactly their own three, 24 rows |
| Per-user cap: the owner reaching the cap does not lock the member out | passes |
| Browser/API: owner, admin and member add the workforce simultaneously (all `created: true`, 3 each, distinct ids, a second call `created: false` and no duplicates); another member clicks "Add the starter workforce" in the UI and then generates a plan; a member can neither list nor open another member's agent (404) | pass |

## 7. Remaining browser acceptance gaps (second harness suite `journeys2.mjs`: 13/13)

| Journey | How it was driven | Result |
|---|---|---|
| **Unknown-outcome reconciliation** | the simulated send is slowed (harness-only launcher, application code untouched); an admin approves in the browser; once the dispatch is in flight the API process is **hard-killed and restarted**; after recovery the call is `unknown`, the run is **not** reported as succeeded, a member sees "Outcome unknown" and "Only a workspace owner or admin can reconcile" (API 403); "Ask the remote service" leaves it unknown and records the attempt; the admin then records "NOT performed" with evidence, which the ledger labels as a person's statement; the owner can retry | pass |
| **Plan revision editor** | edit an *approved* plan in the UI: revision 2 saved, approval withdrawn, revision 1 kept in the history; an invalid edit is refused with a visible error and no revision is created; a member cannot edit someone else's plan | pass |
| **Wrong-email invitation** | a person signed in with another e-mail clicks "Join workspace": "This invitation can't be used", no membership; the invited e-mail can still join afterwards | pass |
| **Role change and removal** | the owner changes a member to admin and back in the UI (backend role verified each time; the admin can read approvals); the owner removes a member: their API access ends at once (403), the UI shows no workspace data; the owner cannot be removed | pass |
| **Notification timing** | the admin's already-open page: the inbox badge appeared 0.8 s and 1.3 s after the approval existed in two runs (limit asserted: 20 s; the documented poll is 15 s) and fell 0.3 s after the decision | pass |

**A defect found and fixed here (reconciliation).** The simulated remote keeps its records in process memory. After the very crash that creates an unknown outcome it has forgotten every key, yet its answer "no record" was read as proof that nothing was sent (`not_found` plus remote idempotency means confirmed failed), which would allow a retry and a duplicate send. It now answers `unknown` for a key it does not remember (it can still confirm what it remembers) and its delivery text says so. A real remote that retains keys is unaffected by this change; `email.send@1` has no lookup at all.

## 8. Accessibility

**Fresh axe-core 4.10.2 scan** (`axe.mjs`; tags wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa): 18 pages plus agent form with errors, plan editor, confirm dialog open, team page with a pending invitation and (mobile) the open More sheet; dark and light themes; 1280, 390 and 320 px. **136 scans, 0 violations.** The scanner was sanity-checked: it reports `button-name`, `color-contrast` and `image-alt` on injected defects.

**Keyboard-only regression** (`keyboard.mjs`, Tab/Enter/Space/Escape/arrows only): **9/9** after two fixes.

| Check | Result |
|---|---|
| Every Tab stop on the overview shows a focus ring and is on screen; nav, New objective and the objective box reachable; no positive tabindex | pass |
| **No skip link** (the first stop was the brand link; about 12 stops before the content) | **fixed**: "Skip to content" is the first stop and the target `main` takes focus (WCAG 2.4.1) |
| Plan approval by keyboard: Approve opens the dialog, focus moves into it, the page behind is inert (past the last button focus goes to the browser UI, never to a page element), Escape closes and returns focus to the trigger, Enter confirms | pass |
| Agent form: Enter submits; focus lands on the first invalid field | pass |
| Team role select operable with the arrow keys | pass |
| Approval decided with Space on "Reject this action" | pass |
| Mobile More sheet: Enter opens it, its links follow in order | pass |
| **The More sheet ignored Escape** | **fixed**: Escape closes it and returns focus to the "More" button |

**Still not done:** no human screen-reader pass (NVDA, VoiceOver); axe and keyboard checks do not replace it. `docs/accessibility.md` keeps the manual script.

## 9. D075 (shared tool reads): what was verified, what changed

Recorded in `decision.md` as **D075**, status *accepted in principle by the owner, wording awaiting final confirmation*, with the exact policy and trade-offs. While writing it the shared view was audited: the **dispatch idempotency key was returned to every member**. It is internal protocol detail the UI never used, so it was removed from every shared view. A test now scans everything a member can read about tool calls for credential-shaped values and internal field names. Redaction remains pattern-based best effort (stated in D075). The permission matrix in section 4 stands.

## 10. Honest limits of this evidence

Scripted model, simulated send, SQLite for the browser stack (PostgreSQL only for the backend suites). The crash is a hard kill of one API process, not an infrastructure failure. Notification timing was measured on an idle local machine. Nothing here is a live-model, real-email, staging or production result.
