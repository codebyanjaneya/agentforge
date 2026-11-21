# Accessibility record and manual screen-reader script

Status in one line: **automated and keyboard checks passed; screen-reader testing has NOT been performed** (no NVDA, JAWS or VoiceOver is available in the build environment; Windows Narrator exists but its speech output cannot be captured or verified by the tooling used, so it is not claimed). Do not describe Studio as "screen-reader tested" until the script below has been run by a person and recorded.

## What was verified (2026-11-21, isolated harness: real app, scripted model, simulated tool; Chromium headless shell, axe-core with wcag2a, wcag2aa, wcag21a, wcag21aa, wcag22aa and best-practice)

15 Studio pages (overview, plans, plan draft, plan approved, runs, run, approvals, agents, new agent, templates, tools, tool detail, tool activity, team, join) at three layouts each (1280 dark, 640 light = 200 % zoom equivalent, 320 light = reflow):

| Check | Result |
|---|---|
| axe violations (45 page/layout combinations) | **0** |
| Horizontal scroll at 320 and 640 CSS px (WCAG reflow) | none on any page |
| One `<main>`, `lang` set, a document title, exactly one `h1`, no skipped heading level, no duplicate ids, labelled navs | all pages |
| Tables | the two run tables have captions and header cells |
| Keyboard: 87 Tab presses on plan, run, team and tool pages | every stop has a visible focus indicator; the only exception is the Next.js development overlay (`nextjs-portal`), which does not exist in a production build |
| Modal dialogs (approve plan, cancel plan) | native `<dialog>` modal; focus moves in; Tab never reaches the inert page; Escape closes; focus returns to the trigger; each dialog's accessible name is its own title |
| Reduced motion (`prefers-reduced-motion: reduce`) | no repeating animation on the run, overview or approvals pages |
| Live regions | present on every page that reports an outcome (team, join, run, approvals) |

**Defects found by this audit and fixed:** (1) every confirmation dialog used the same `id="st-dialog-title"`; with three dialogs on the plan page `aria-labelledby` resolved to the first one, so a screen reader would have announced "Approve revision 1?" for the cancel dialog; each dialog now has its own `useId()`. (2) On narrow screens the mobile top bar sat outside any landmark (axe `region`); it is now a `<header>`. Both have guard tests (`frontend/tests/studio-a11y.test.mjs`).

**Not covered by any automated check, and therefore still open:** the quality of announcements (are status changes read in a sensible order and not twice), reading order of dense run timelines, whether the wording of errors helps, and real-device zoom/text-spacing behaviour. These need a person with assistive technology.

## Manual screen-reader script (for a person to run; record the result next to each step)

Environments to cover at minimum: **NVDA (latest) + Firefox or Chrome on Windows 11**, and **VoiceOver + Safari on macOS**. Record product commit, OS, browser, screen reader and version, date, tester.

1. **Sign in and landmarks.** Open `/studio`. List landmarks (NVDA: `D`; VoiceOver rotor): banner (mobile only), navigation "Studio"/"Primary", main. Expected: one main; skip to it works.
2. **Overview.** Read the heading ("Overview" h1 then sections). Expected: the next step is announced as text, not only by colour.
3. **Plan review (draft).** Open a draft plan. Expected: the status badge is read as text; the tasks list is a list; "Approve this plan", "Edit tasks", "Validate", "Cancel plan" are buttons with those names.
4. **Approve dialog.** Press "Approve this plan". Expected: "Approve revision N? dialog" is announced, focus on "Not now"; Tab cycles inside; Escape closes and focus is back on "Approve this plan". Repeat for "Cancel plan": the announced title must be "Cancel this plan?" (not the approve title).
5. **Start run, then run page.** Expected: the status ("Running", "Completed", "Failed") is read; changes of status are announced politely at most once; the task table is read with column headers; side-effect state (sent / unknown / reconciled) is read as words.
6. **Approvals.** Open a pending approval. Expected: who is asking, tool, exact values and impact are read in that order; the Approve/Reject buttons (owner/admin only) are named; a member sees the explanation instead.
7. **Team and invitations.** Create an invitation. Expected: the success message (invitation link, "shown only now") is announced (aria-live) and the read-only link field is reachable and labelled "Invitation link". Withdraw, change role (labelled "Role of <email>"), remove and leave are reachable by keyboard and announce their result or error.
8. **Join page.** Open an invitation link in a signed-in browser. Expected: the result of "Join workspace" ("You joined X" or "This invitation can't be used") is announced.
9. **Zoom and reflow.** At 200 % browser zoom and at 320 px width nothing is cut off and no horizontal scrolling is needed (already verified by script; confirm by eye).
10. **Errors.** Trigger a failure (stop the API, then press any action). Expected: an error message is announced and the page remains operable.

Anything that fails is a defect to fix before the claim "screen-reader tested" is made.
