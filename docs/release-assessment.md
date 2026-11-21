# Release assessment (2026-11-21, closure programme)

**Verdict: STAGING-READY. NOT PRODUCTION-READY.** "Staging-ready" means the software and the runbook are complete and verified locally and in CI, so an authorised staging run can start. It is not a statement that staging has been run: nothing has been executed on staging, and nothing has been verified externally.

The three categories are kept apart on purpose. Never read an A row as a B or C row.

- **A. Verified locally** (developer machine, disposable PostgreSQL/Redis/Sentinel/nginx containers, an isolated browser harness with a scripted model and a simulated tool) **and in GitHub Actions** (Ubuntu runners, disposable containers). Both are "local" in the sense that no real external service, no staging and no production is involved.
- **B. Verified in authorised staging:** *nothing.* No staging environment has been authorised or accessed.
- **C. Verified externally / production-equivalent:** *nothing.* No real Resend send, no real model run, no real alert receiver or on-call, no real edge, no real screen reader.

## A. Verified locally and in CI (evidence on the final tree)

| Area | Evidence |
|---|---|
| Backend regression | one pytest process per file, sequential: **82 files, every exit code 0, 1,445 passed**; skips only env-gated (11 PostgreSQL invitation tests and 5 real-Redis tests, both run below) and `test_studio_samples` (skipped by design); `tests/test_live_model_check.py` (2 tests, added after that run) also passes |
| PostgreSQL | `tests/test_postgres_integration.py` **64 passed** (252 s) on a disposable PostgreSQL 16; `tests/test_invitations_postgres.py` **11 passed** (behaviour on PostgreSQL, see below) |
| Redis | `tests/test_provider_coordination.py` against a real Redis: **16 passed, 0 skipped**; Redis failure drill 14/14; Sentinel drills (degrade, closed) pass |
| Frontend | **160 passed, 0 failed, 6 skipped** (unchanged skips); `tsc`, `eslint`, `next build` clean |
| Infrastructure | `actionlint` clean; nginx drill 13/13 on a real nginx; backup/failed-restore drill and PITR drill pass; load smoke and worker-crash scenarios pass in a clean Linux container and in CI |
| Browser | invitation journey, permission matrix (members see no write actions), 15 pages x 3 layouts axe **0 violations**, reflow at 320/640 px, dialogs and keyboard verified (`docs/accessibility.md`) |
| Live model | the harness (`ops/live_model_check.py`, `--dry-run` tested in CI) runs one bounded plan through the real run path; the single authorised real run reached the provider and was refused (`unsupported_model`) | see the live-model section |
| GitHub Actions | the workflow ran on `main`; its first runs failed for real portability reasons that were fixed; the latest runs passed all four jobs (see the final report for the exact run) |

**Invitations on PostgreSQL (the gap that was open):** token returned once and only its SHA-256 stored (the raw token is in no column of any table and in no log); exactly 7-day expiry with the boundary refused; single use; withdraw/expired/revoked/used refused with one indistinguishable error; verified-email match with case/space normalisation; a token never grants another workspace; role change, removal and leave with the sole-owner invariant and pending invitations unaffected; six simultaneous accepts create one membership; accept-versus-withdraw always ends consistent; concurrent invitations for one email leave one open; concurrent role updates end valid; an injected failure rolls back the consumed token and the membership; database constraints refuse a second open invitation, an owner role, bad expiry, accepted+revoked and acceptor mismatch; migration 0016 -> 0017 -> downgrade -> re-upgrade keeps existing data and builds the partial unique index.

### Assessment by dimension

| Dimension | Implemented / tested / environment | Remaining blocker | Owner action |
|---|---|---|---|
| Functional completeness | yes / yes / local + CI + browser | none known for the documented scope; no owner transfer; invitation e-mail is not sent (link is copied) | decide on owner transfer and invitation e-mail |
| Authorization and tenant isolation | yes / yes (SQLite, PostgreSQL, browser) / local | none known | none |
| External side-effect safety | yes / yes against a faithful local mock (approval binding, at-most-once, unknown outcome, reconciliation audit) | **D073: real Resend sandbox send and real unknown-outcome test not done** | provide a sandbox key, an authorised recipient and a go-ahead for one external send |
| Recovery and durability | yes / local drills only (`docs/recovery-and-staging.md`) | **no staging RTO/RPO; no restore time at real data size** | authorise staging, then run the runbook |
| Observability and alerting | yes / local receivers | **D074: no real receiver, no on-call, no external monitor** (the in-process evaluator cannot report its own death) | provide a non-production receiver and the on-call route; stand up an external uptime check |
| Security | yes / unit, PostgreSQL, secret scan (only fake fixtures found), nginx drill | no external penetration test; real edge/certificates untested | authorise a staging edge; consider an external review |
| Accessibility | automated + keyboard + dialog checks pass; two defects fixed | **no screen-reader test performed** | run the script in `docs/accessibility.md` with NVDA and VoiceOver |
| Provider reliability | yes / failure modes tested locally (errors, timeouts, 429, cooldown, cap, malformed, accounting) | **no successful live-model run: the one authorised run ended at the provider with `unsupported_model` (the configured model is no longer served); see the section below** | choose a currently served model (for example the platform default `openai/gpt-oss-120b`) and authorise one more bounded run, and fix `MODEL_NAME` in `backend/.env` yourself (I do not edit that file) |
| Operational readiness | runbook, alerts, backup job, drills written and drilled locally | staging unexecuted | authorise staging |
| Deployment readiness | CI green, config gate, migrations forward-only with a tested downgrade for 0017 | no deployment has been rehearsed anywhere | authorise staging; confirm a rollback owner |

Known limits that stand: legacy short-term chat memory is not Sentinel-aware; loss of the Redis origin key together with every slot is undetectable; Redis Cluster is unsupported; `PROVIDER_REDIS_DOWN=degrade` stays an explicit opt-in; the in-process alert evaluator restarts from empty state; invitation lifetime is a constant.

## Live-model validation: the one authorised run (2026-11-21, 14:26:58Z to 14:27:04Z)

**Outcome: the provider refused the model. No model output exists.** Nothing here shows the platform working against a live model, and nothing here shows it failing: it shows that the deployment's configured model is no longer served.

| Item | Evidence |
|---|---|
| Authorisation used | exactly one run, using the key in `backend/.env` (read inside the process, never printed, copied or written; `backend/.env` not modified); the owner chose the model configured there |
| Provider / exact model identifier | Groq / `llama-3.3-70b-versatile` (`MODEL_NAME` in `backend/.env`) |
| Limits set before the first request | at most 3 provider requests in total (a wrapper refuses a fourth), `max_tokens` 400 per request, 30 s per request, 90 s per run, one attempt per request (SDK retries off, no retry layer), one task and no parallel calls, estimated-cost ceiling $0.01 |
| Objective | one task: use the read-only `math.calculate` tool to compute 17*23 and answer in one sentence; the plan was scripted (no model call for planning); no side-effecting tool was granted, no email, no Redis, no staging or production; a throw-away SQLite database, not the database named in `backend/.env` |
| Plan run ID | `4151bb01-862f-425a-b3f7-30a54e27a793` |
| Execution ID | `174bcfb4-cec2-4b82-99d0-9245c894ee71` |
| Result | provider request 1 returned the fixed error code **`unsupported_model`** after **0.317 s**; run `failed`, task `failed` with error code `unsupported_model`; wall clock 0.489 s |
| Provider requests made | 1 (the cap was never approached); no retry was made and no second run was started |
| Tokens and cost | 0 prompt tokens, 0 completion tokens, no usage rows, **$0.00** estimated (a refused request returns no usage; the provider's own invoice was not consulted) |
| Tool ledger | no tool invocation; side-effecting dispatches: 0 |
| What this proves | the key was accepted far enough for the provider to evaluate the model name (the error is a model error, not an authentication error; that is an inference from the error class, not a statement from the provider); the failure surfaced honestly as `failed` / `unsupported_model` rather than as a success; the hard limits and the single-attempt policy held |
| What this does NOT prove | that any model answers through the platform, that a tool call round-trips with a live model, token accounting against a real response, latency of a real completion, and the claim "task completion reflects model evidence" with a live model (that last point is covered only by the scripted-provider tests and the `--dry-run` harness check) |

The configured model name is the blocker, not the platform: `llama-3.3-70b-versatile` was noted as retired by the provider in an earlier session (a code comment in `backend/config.py` says the platform default `openai/gpt-oss-120b` "is verified live with tool calling"; that statement is from an earlier session and was **not** re-verified now). I did not substitute another model, because the owner chose the configured one for this single run. The platform's cost accounting has no price for `llama-3.3-70b-versatile` (only for `openai/gpt-oss-120b` and `-20b`), so a successful run of the configured model would have reported `unknown_pricing`.

**To get the missing evidence:** change `MODEL_NAME` in `backend/.env` yourself to a model the provider currently serves (the platform default is a candidate), and explicitly authorise one more bounded run; then `python ../ops/live_model_check.py --yes-spend-one-bounded-run` (from `backend/`) records the model, run ID, task status, latency, tokens and cost. The same applies to custom agents: any agent version pinned to the retired model will fail the same way until its model is edited.

## What would change the verdict

STAGING-READY becomes **production-ready** only when, in authorised environments: the staging runbook has been executed with measured RTO/RPO; D073 and D074 have their external evidence and the owner has confirmed them; a live-model run has passed within a spending cap; a human screen-reader pass has been recorded; and the owner accepts the remaining limits above.
