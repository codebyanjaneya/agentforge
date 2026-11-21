# Side-effecting tools: execution guarantees, API contract and operations

## What is guaranteed, and what is not

| | Guarantee |
|---|---|
| Automatic dispatch | At most ONE per logical operation (same plan task, tool and canonical validated arguments), across crashes, retries, takeovers and concurrent attempts |
| Unknown outcome | Never shown as a failure; never dispatched again automatically; resolved only by an audited reconciliation |
| Confirmed success | Never repeated by a retry: the recorded result is replayed |
| Approval | Bound to the exact tool, version and canonical arguments; consumed atomically with the dispatch authorisation; cannot authorise another invocation |
| NOT guaranteed | Exactly-once effects against an external service; recognising two differently worded requests as one operation; protection across separate standalone (REST/WebSocket) executions; that an operator's statement is true |

## The state machine (`tool_dispatches`)

`authorized` -> `dispatching` -> `succeeded` | `failed` | `unknown`; `authorized` -> `cancelled`; `unknown` -> `reconciled_succeeded` | `reconciled_failed`. Enforced by database triggers (SQLite and PostgreSQL), written together with a `tool_dispatch_*` run event. `failed` only when the tool PROVES nothing was done (`DefinitiveFailure`, or rejected arguments). A timeout, cancellation, unexpected error, crash or lost lease after `dispatching` is `unknown`.

## Per integration

| Tool | Side effect | Remote idempotency | Queryable | Automatic retry after a confirmed failure |
|---|---|---|---|---|
| `calendar.public_holidays@1` (real) | none (read-only) | n/a | n/a | n/a (repeat is harmless) |
| built-in computing tools | none | n/a | n/a | n/a |
| `demo.send_note@1` (SIMULATED) | simulated external | yes (simulated) | yes (simulated) | never |

A new side-effecting tool must declare `retry_policy` (`never` | `after_confirmed_failure`), `remote_idempotency`, `delivery` (plain-words guarantee), `external_ref_field`, and may register `lookup(idempotency_key) -> (status, external_ref, evidence)`. A tool whose remote does not honour idempotency keys gets at-most-once automatic dispatch and manual reconciliation, nothing more.

## API contract

- `GET /plan-runs/{id}/tool-invocations`, `GET /tool-invocations`, `GET /tool-invocations/{id}`: each invocation has `dispatch` (null for a tool without side effects) = `{state, outcome: confirmed|unknown|reconciled|not_sent|in_flight, performed: true|false|null, requires_reconciliation, generation, idempotency_key, external_ref, reconciliation: {decision, evidence_kind, evidence, reason, by, at, attestation_only, attempts}}`. `performed` is `null` while unknown: never guessed.
- `GET /tools`: `delivery` for side-effecting tools: `{retry_policy, remote_idempotency, can_query_remote, guarantee}`.
- `GET /reconciliations`: `{can_reconcile, items[]}`, the workspace's unknown outcomes.
- `POST /tool-invocations/{id}/reconcile` (workspace owner/admin): body `{mode: "query", reason}` or `{mode: "attest", decision: confirmed_succeeded|confirmed_failed, evidence, reason}`; unknown fields 422. Errors: 404 (also for another workspace), 403 `reconciler_role_required`, 409 `already_resolved`/`not_unknown`, 422 `lookup_unavailable`/`evidence_and_reason_required`.
- `GET /plan-runs/{id}`: tasks may have `retry: "reconcile"`; `retry_options.reason == "reconciliation_required"` with `retry_options.reconcile` (task keys).
- `POST /plan-runs/{id}/retry`: `409 reconciliation_required {tasks}` while an unknown outcome exists in a task it would re-run; acknowledging does not override it.
- Run events: `tool_dispatch_authorized | started | succeeded | failed | unknown | cancelled | replayed | reconciled | refused` (codes, task, tool ref, generation, step only: never values).

## Operations runbook: an `Outcome unknown` call

1. Open the run -> **Side effects** -> the call shows **Outcome unknown**. Nothing is retried; the run's retry button says why.
2. Prefer **Ask the remote service** (if the tool can be queried): the answer is evidence and is recorded; a service that cannot answer leaves it unknown and records the attempt.
3. Otherwise check the remote system yourself and use **Record my decision**: say what you checked and why. It is stored and shown as a person's statement, not proof.
4. After `reconciled_succeeded` a retry REPLAYS the send (nothing is sent). After `reconciled_failed` a retry may send again with a NEW idempotency key and, for a gated tool, a fresh exact-action approval.
5. If you are unsure, leave it unresolved: that is always safe.
