# Decision confirmation request: D069 to D074

**Recorded 2026-11-21:** D069, D070, D071 and D072 were confirmed by the owner with exactly the meanings and trade-offs in the table below (now "Accepted" in `decision.md`). D073 and D074 stay "Directed, owner confirmation pending" (conditions below). History: until 2026-11-21 all six were "Directed by the sprint briefs, owner confirmation pending." D046 and D050 are untouched.

| ID | What it means in operation | What confirming approves | Trade-off you accept |
|---|---|---|---|
| **D069** Execution truth | A task is "succeeded" only if the server's dispatch ledger proves its external actions. A model that says "I sent it" while the ledger shows an unknown or failed send cannot make the task green. | The server, not the model, decides task and run success for tools with side effects. | A run with an unknown outcome shows *partly completed / needs reconciliation* instead of success, until a person reconciles it. More honest, less "green". |
| **D070** Shared rate limits | Per-user request limits are counted in Redis, so they hold across API processes; if Redis is down each process falls back to its own bounded counter. | Redis as the shared counter, with a documented per-process fallback. | During a Redis outage the limit is per process (a user can get up to N times the limit across N processes). Visible as a gauge. |
| **D071** Provider coordination | A 429 from the model provider cools down every process; a global cap limits concurrent provider calls. Default when Redis is untrusted is `closed`: refuse admission. `degrade` is an explicit opt-in that drops the cap. | The `closed` default, the quarantine after a Redis server change, `PROVIDER_LEASE_SECONDS` (default 90 s). | Availability is traded for the cap: measured 6 `rate_limited` task failures and ~47 s p95 during a 9 s outage. Redis Cluster is unsupported; losing the origin key together with the slots is undetectable. |
| **D072** Observability baseline | `/metrics` (token protected, bounded labels), JSON logs with correlation ids, `/ready` separate from `/health`. | The metric names and label sets as the operational contract. | Renaming a metric later is a breaking change for dashboards and alerts. No distributed tracing yet. |
| **D073** `email.send@1` via Resend | The first real side-effecting tool: one recipient, sandbox recipients only by default, an approval of the exact message per call, a stable idempotency key, **no** key-based lookup claimed. | Resend as the first integration and the "unknown is reconciled by a person" rule for it. | Real Resend calls are **unverified** (no sandbox key was available); a lost answer needs a person. Not exactly-once. |
| **D074** Alerting and edge | In-process alert evaluator with a webhook and Redis de-duplication; request-size and Host limits in the app, with an nginx reference config. | Alerting without a new service; the app-level protections. | A production receiver, on-call routing and the real edge are **unverified**; an in-process evaluator dies with the process (an external monitor is still advised). |

## Recorded outcome (2026-11-21)

| ID | Status now | Condition for the remaining ones |
|---|---|---|
| D069 | Accepted (owner-confirmed) | none |
| D070 | Accepted (owner-confirmed) | none |
| D071 | Accepted (owner-confirmed) | none |
| D072 | Accepted (owner-confirmed) | none |
| D073 | Directed, owner confirmation pending | a real Resend **sandbox** send and an unknown-outcome / reconciliation test are verified |
| D074 | Directed, owner confirmation pending | webhook delivery, a production receiver, on-call routing and real edge protections are verified in an authorised environment |

D046 and D050 were not touched. Recording these decisions changed no implementation and no deployment configuration.

## Verification status of the two pending decisions (2026-11-21, final closure programme)

Statuses are unchanged: D073 and D074 stay "Directed, owner confirmation pending". Only the owner can change that, and only after the external evidence below exists.

**D073 (`email.send@1`, Resend).** Done locally against a loopback mock that models only what Resend documents: sandbox-only recipients; a rejected non-sandbox recipient and a malformed or multi-recipient message never reach the network or the approval queue; no approval path means nothing is sent; a rejected approval sends nothing; an approved one sends exactly once with the operation's stable idempotency key; an approval authorises only the exact message (changed recipient, subject or text is refused); provider success, rejection, 429/5xx, drop-after-accept (unknown), reconciliation to succeeded and to failed with one audit row, no automatic redispatch, no key-based lookup claimed (Resend honours `Idempotency-Key` but documents no lookup); the API key appears in no stored column or event. **Still required for confirmation:** a real Resend **sandbox** send and an unknown-outcome/reconciliation test against the real service. No sandbox key exists in any authorised location (`RESEND_API_KEY`, `EMAIL_FROM`, `EMAIL_ALLOWED_DOMAINS` are not set anywhere; `backend/.env` was not modified). Needed from the owner: a sandbox key via the secret store or an environment variable, an authorised test recipient (for example `delivered@resend.dev`), and an explicit go-ahead for the one external send.

**D074 (alerting and edge).** Done locally: the alert state machine, recovery, de-duplication across processes, Redis outage, bounded retries, 4xx/5xx/non-HTTP/slow/truncated receiver behaviour, redirects never followed (a defect found and fixed this sprint), restart behaviour, secret redaction, closed label vocabulary; nginx drill 13/13 on a real nginx (limits, 413, hidden endpoints, header overwrite, WebSocket over HTTP/1.1, TLS floor); the in-process evaluator's limits and the need for an independent external monitor are documented (`docs/operations.md` section 18). **Still required for confirmation, in an authorised environment:** webhook delivery to a real non-production receiver including de-duplication, a production receiver, on-call routing, and the real edge (certificates, real hostnames, CDN if any). Needed from the owner: the receiver URL/secret via the secret store, the on-call routing to test, and an authorised environment. Local nginx is not production-edge verification.

Sprint 5 adds no new decision that needs a number: invitations and shared read access are recorded in `flow.md` section 25.36 as implemented behaviour. If you want them as D075/D076 (shared workspace read policy; invitation security model) say so.
