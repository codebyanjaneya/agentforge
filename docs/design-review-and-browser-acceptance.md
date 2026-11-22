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
* The starter-workforce slug collision in shared workspaces (section 2).
* Owner decisions: D075 (shared-read policy), whether members may see tool inputs/outputs.
