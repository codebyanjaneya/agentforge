# Master-context append: P0 safety sprint (2026-11-19) - append-ready

(The master-context files are not reachable from this environment and the earlier append file lived in a scratchpad that no longer exists; this file is self-contained.)

**What changed.** Side-effecting tool calls now have a durable dispatch ledger (`tool_dispatches`, migration 0016): authorized -> dispatching -> confirmed (succeeded/failed) or unknown -> reconciled; unknown is never retried automatically; a confirmed success is replayed on retry, never resent; an operation key identifies the logical operation across attempts; a duplicate-prevention gate refuses in-flight or unknown operations; reconciliation is an audited, separately authorised protocol (`tool_reconciliations`); plan-run retry is blocked while an unknown outcome exists (`reconciliation_required`). D066-D068. D046 and D050 unchanged.

**Guarantee.** At most one automatic dispatch per logical operation. NOT exactly-once against any external service. A differently worded message is a different operation. Standalone executions have no cross-attempt identity.

**Evidence.** 46 backend test files green; new `test_durable_dispatch` (26), `test_dispatch_api` (5), `test_dispatch_schema` (2); PostgreSQL 63 passed incl. real-concurrency tests; frontend 145 pass, tsc/eslint clean; 13/13 mutations killed (the PostgreSQL advisory-lock mutation survived: defence in depth only).

**Genuinely live vs simulated.** No live provider run in this sprint. The send is simulated; `calendar.public_holidays` is the only real integration (read-only).

**Not verified / blockers.** New Studio UI not verified in a real browser (tool unavailable); no real side-effecting integration to reconcile against; multi-OS-process PostgreSQL concurrency not run; standalone-execution identity; operator attestation is a statement, not proof.
