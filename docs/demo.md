# AgentForge demo: the launch-readiness workflow

A reproducible, honest demonstration of the product. **Read the disclosure first.**

## What is real and what is not

| Real (the product) | Scripted or simulated (demo mode) |
|---|---|
| The planner's validation, plan approval, the run scheduler, task dependencies | The **model** is a script (`demo-script`), not a live model. It plans one workflow and asks for tools in a fixed order. Anything else is declined. |
| The three tools that compute: **unit economics** (LTV, LTV:CAC, payback), **date arithmetic** (business days), **text analysis** (readability). Results are computed from the numbers in your objective. | The **sending tool** `demo.send_note` is a **simulation**: it sends nothing anywhere and returns a receipt. |
| Exact-action approval, the owner/admin rule, durable suspension (a restart does not lose a pending approval), the append-only run history, redaction | |

Nothing here is a live integration, a customer outcome or evidence of traction. A live model is not verified by this demo.

## Run it (local, disposable)

Backend (any shell, from `backend/`):

```
set APP_ENV=test
set AGENTFORGE_DEMO_MODE=1
set GROQ_API_KEY=any-placeholder          # NOT a real key: the legacy built-in agents read this variable at import
set DATABASE_URL=sqlite:///demo.db         # SQLite is only allowed in APP_ENV=test
python -m alembic upgrade head
python -m uvicorn main:app --port 8000
```

`AGENTFORGE_DEMO_MODE` is **refused in production** (`APP_ENV=production` fails at start). The Studio shows a **DEMO MODE** banner while it is on.

Frontend: the normal Studio setup (sign in with Google). Demo mode changes nothing about authentication.

## The five-minute walkthrough

1. **Overview.** A new workspace shows one next step: *Assemble your workforce*. Click **Add the starter workforce**: three specialists appear with the exact tools each may use (the communications lead also holds the simulated send tool).
2. **Delegate.** Click the example objective (or write your own numbers: price, margin, churn, CAC) and **Generate plan**. Declined objectives show why.
3. **Review.** Three tasks: *Check the unit economics* and *Set the launch date* run in parallel; *Tell the first customer* depends on both. Approve the plan, then start the run.
4. **Watch.** The first two specialists call real tools (see the tool trail under each task). The third checks its draft's readability with a real tool, then stops: **"Suspended: saved, waiting for your approval"**.
5. **Decide.** Open the request: who is asking, the exact tool and version, the exact values, why, the impact. **Approve this exact action** or **Reject**. Only an owner or admin may decide.
6. **Result.** Approve: the note is "sent (simulated)" and the run completes; the run history shows the full lineage. Reject: the agent reports that nothing was sent.
7. **Judgment on real numbers.** Try `$20/month, 60% margin, 8% churn, $500 CAC`: the unit economics come back *unprofitable*, so the communications lead **does not send** and asks for no approval.
8. **Durability (optional).** While a request is pending, stop the backend and start it again: the approval is still pending, and approving it resumes the exact call.

## Known limits of the demo

- The scripted model understands only launch/pricing objectives; the plan and the agents' words are scripted.
- Redis is not required for the demo; the agents' short-term memory logs a warning and carries on.
- The built-in specialists (Sales, Research, ...) need a real model key and are not part of this workflow.


## M6: live mode, a real integration and shared approvals

Demo mode above is unchanged and still disclosed. With a real model key and no `AGENTFORGE_DEMO_MODE`, the Studio shows a **LIVE MODEL** banner naming the model, plans and agent work are written by it (a provider error or rate limit is shown as such, never replaced by a script), and **`calendar.public_holidays`** is a real call to date.nager.at (the Launch Planner template may use it). The sending tool is still a **simulation** in every mode. To see shared approvals: create a workspace on the **Team** page, add people who have signed in once as admin or member, switch workspace in the sidebar; owners and admins get an inbox badge when an agent asks, members can read the request but not decide it. Each run page shows **Tokens and cost** from the provider's own usage report (a cost appears only when it can be calculated). A live free-tier key rate-limits bursts: retry the run (finished tasks are reused).


## M6b: reliability under real provider limits

The model provider is retried with bounded, jittered backoff that honours `Retry-After`; the run page shows **Model provider** states (waiting before retry, retrying, recovered, rate limit exhausted, provider unavailable). A task can **require** an action (for example the send): it is complete only when that tool was really invoked, so a model that merely says it sent the note fails the task; the call itself still pauses for a person's approval. A free-tier key can hit a long usage limit after several runs (the Studio says "asked us to wait about N minutes"); retry the run later, finished tasks are reused. Run the live backend with `PROVIDER_CONCURRENCY=1` on a free tier. The send is a simulation in every mode.


## P0: what the send step guarantees

The send is a **simulation**: nothing leaves AgentForge. Even so it is run through the real dispatch protocol, so you can see what a real action would get. It is dispatched **at most once automatically**: if the process stops after the request started, the outcome is shown as **Outcome unknown** (not as failed), the run cannot be retried until someone reconciles it, and a retry after a confirmed send **replays** the recorded result instead of sending again. The simulated remote can be asked, so "Ask the remote service" resolves an unknown outcome with evidence; "Record my decision" stores a person's statement and labels it as such. AgentForge does **not** claim exactly-once delivery to any real service: see `docs/side-effects.md`.


**Sprint 2 (user-visible).** A task whose external action could not be proven now ends as **Failed** with "An external action of this task has an unknown outcome, so the task is not reported as done. Reconcile the action first." (or "...failed...") instead of "Succeeded" next to a warning; reconcile it on the run page, then start a new run. Too many requests answer **429** with `Retry-After`; the Studio shows the server message. Operators: `GET /ready` (readiness), optional `GET /metrics` (set `METRICS_TOKEN`), `LOG_FORMAT=json`; see `docs/operations.md`.

**Sprint 3 (user-visible).** A retry attempt can no longer finish as Completed over an earlier confirmed failure of the same task: when the tool policy refuses the repeat send, the attempt ends "Partly completed" and the task shows "An external action of this task failed...". Signing in repeatedly is limited per person (`TOKEN_ISSUE_PER_MINUTE`, default 30 per minute).

**Sprint 4 (user-visible).** A new tool, **Send an email (Resend)**, exists only when an operator configures it; it is sandboxed to Resend's test addresses by default, needs a person's approval of the exact recipient, subject and text for every call, and says plainly that an unknown outcome can only be reconciled by checking Resend (there is no "ask the remote service" for it). The simulated "Send a note" is unchanged and still labelled simulated. Operators: live alerts via `ALERT_WEBHOOK_URL`, backup status via `BACKUP_STATUS_FILE`, a Redis Sentinel option, request-size and Host limits; see `docs/operations.md` sections 16-21.


## Sprint 5: the team journey and the 3-minute YC script

Everything below was run in an isolated harness (real FastAPI app, real JWT verification, real database; **the model is a script and `demo.send_note` is a simulated tool**; the "LIVE MODEL" banner you see in those screenshots is the Studio's configuration label, not evidence of a live model). Reproduce: `docs/operations.md` section 23.

**Script (about 3 minutes, one presenter, two browser profiles).**

1. *Owner, Team page.* "Create a shared workspace" -> **Launch Crew**. Under *Invite by email* type a teammate who has **never signed in**, role Member, **Create invitation**. A single-use link appears once ("AgentForge does not send email: copy this link"). Say so: delivery is by you, not by us.
2. *Overview.* **Add the starter workforce**, write an objective, **Generate plan**, review the three tasks, **Approve**, **Start run** (three separate, explicit steps).
3. *Teammate profile, not signed in.* Open the link: the sign-in gate appears and, after sign-in, returns to the invitation with the secret intact (it is removed from the address bar). **Join workspace**. A person signed in with a different email sees "This invitation can't be used", and so does a second use of the link.
4. *Teammate, Plans and Runs.* They see the owner's plan and run with **created by / started by** and the full timeline and cost, but cannot change them (revise, approve, cancel, start and retry stay with the person who made them; approvals are decided by owner or admin).
5. *Owner, Team.* Change the teammate's role, then **Remove**; **Leave workspace** is the teammate's own button; the sole owner can be neither removed nor leave.
6. *Tools, any tool with side effects.* The **Delivery guarantee** section states what is and is not guaranteed (at most one automatic dispatch, no exactly-once claim, reconciliation by a person).

**Honest labels for the audience.** Real: authentication gate, invitations (hashed single-use token, expiry, verified-email match), roles, shared read access, plan/run/approval/dispatch ledger, the three computing tools. Simulated or scripted: the model, `demo.send_note`. Not verified: a live model run, a real Resend send, a real production deployment, GitHub CI.
