# AgentForge — Decision Register

> Permanent, append-only register (D017). Never delete entries. To change a decision, mark it **Superseded** and link its replacement. All decisions below D001–D017 were approved by the product owner on 2026-11-12. **Approval ≠ implemented**; "Implementation status" reflects the repo at commit `c841cef` (see `flow.md`).

## Index
| ID | Title | Status | Implementation |
|---|---|---|---|
| D001 | Product direction | Accepted | Partially aligned (prototype) |
| D002 | Primary users | Accepted | Not implemented |
| D003 | Workforce creation | Accepted | Partial (7 hard-coded agents) |
| D004 | Execution model | Accepted | Not implemented |
| D005 | LLM architecture | Accepted | **Conflict** (Groq-only) |
| D006 | Initial deployment | Accepted | **Conflict / unverified** (no Docker in repo) |
| D007 | Modular monolith + workers | Accepted | Partial (monolith yes, workers no) |
| D008 | FastAPI | Accepted | Implemented |
| D009 | Next.js + TypeScript | Accepted | Implemented |
| D010 | PostgreSQL | Accepted | Partial (2 tables, no migrations) |
| D011 | pgvector | Accepted | Not implemented |
| D012 | Redis + Streams | Accepted | Partial (Redis yes, Streams no) |
| D013 | Structured plans / DAG | Accepted | Not implemented |
| D014 | Permissioned tool interface | Accepted | Not implemented |
| D015 | Five-layer memory | Accepted | Partial (working + flat episodic) |
| D016 | Tenant isolation, RBAC, encrypted creds | Accepted | **Conflict** (none) |
| D017 | Living docs | Accepted | Implemented by this audit |
| D018–D021 | *Reserved* for M0.1 authentication decisions (not yet recorded) | — | — |
| D022 | Typed fail-fast configuration, CORS and docs policy | Accepted | Implemented (T0); partial overall (legacy env reads remain, JWT enforcement deferred to T5) |
| D023 | Test isolation (SQLite files, no network, no real `.env`) | Accepted | Implemented (T0) |
| D024 | Read-only startup schema-version guard; migrations as a separate deployment step | Accepted | Implemented (T1-H) in the repository; not deployed to any environment; no environment verified adopted |
| D018 | API access tokens: Next.js-issued ES256 JWT verified by FastAPI | Accepted | Not implemented (M1-0 prerequisites verified: dependencies pinned, Node ES256 baseline) |
| D019 | Identity model: internal UUID users, `(provider, provider_subject)`, email as attribute | Accepted | Not implemented (M1-A, M1-D) |
| D020 | Telegram secure linking (separate mandatory pre-production task S1) | Accepted (scope) | Not implemented |
| D021 | Twilio webhook and SSRF protection (separate mandatory pre-production task S2) | Accepted (scope) | Not implemented |
| D025 | Workspaces, memberships and authorization | Accepted | Not implemented (M1-B, M1-E) (historical text; implemented in M1-B/M1-E, header enabled by D057) |
| D026 | WebSocket authentication | Accepted | Not implemented (M1-F) |
| D027 | Memory ownership, history pagination, legacy quarantine and voice isolation | Accepted | Not implemented (M1-C, M1-E, M1-F) |
| D028 | Frontend token handling, dependencies and environment configuration | Accepted | Partial (M1-0: dependencies pinned, `node:test` baseline) |
| D029 | Release gating: rate limiting, production authorization and safety claims | Accepted | Not implemented (gating rules) |
| D030 | Plan execution: in-process scheduler on the M2-B runtime, polling progress, at-most-once dispatch | Accepted | Implemented (M3-B) |
| D031 | Run ownership: PostgreSQL-backed leases with fencing in a separate `run_leases` table | Accepted | Implemented (M4-A1) |
| D032 | Plan-run recovery is lease-based; the unconditional startup sweep of plan runs is replaced | Accepted | Implemented (M4-A1) |
| D033 | Persisted cancellation requests and graceful shutdown | Accepted | Implemented (M4-A1) |
| D034 | M4-A1 scope boundaries | Accepted | In force |
| D035 | Task and execution records start and finish in one fenced transaction | Accepted | Implemented (M4-A2) |
| D036 | Explicit retry attempts with carried-over results | Accepted (mechanism); classification proposed | Implemented (M4-A2) |
| D037 | Durable worker entrypoint on the same ownership protocol | Accepted (entrypoint); auto-continuation proposed | Implemented (M4-A2) |
| D038 | Append-only execution event history | Accepted; vocabulary proposed | Implemented (M4-A2) |
| D039 | Process ownership of executions and database-wide limits | Accepted (mechanism); defaults proposed | Implemented (M4-A2) |
| D040 | Code-defined, versioned, immutable tool registry with exact pinning | Accepted (mechanism); event vocabulary proposed | Implemented (M4-B) |
| D041 | Durable, fenced tool invocation: intent before effect, never silently re-run | Accepted | Implemented (M4-B) |
| D042 | Only read-only, side-effect-free tools are executable; approvals are M4-C | Accepted (boundary); approval binding proposed | Implemented (M4-B, policy only) |
| D043 | Provider-neutral tool calling and a bounded agent loop | Accepted (design; limits 4/8/4 approved 2026-11-18) | Implemented (M4-B) |
| D044 | The Studio presents tools read-only, from real data | Accepted | Implemented (M4-B) |
| D045 | Brand ember preserved; not used for small text where it fails WCAG AA | Accepted | Implemented (M4-B closure) |
| D046 | A human approval authorises ONE exact tool invocation and is consumed atomically with it | Accepted (mechanism); 900 s window proposed | Implemented (M4-C) |
| D047 | Policy: anything not explicitly allowed needs approval; a grant is not an approval | Accepted (boundary); allowlist proposed | Implemented (M4-C, boundary only) |
| D048 | Waiting for approval is in-process and fails closed on recovery | Accepted; durable suspend proposed | Implemented (M4-C) |
| D049 | The approval fixture is a simulation and opt-in; approval events proposed | Accepted (simulation rule); events and approver roles proposed | Implemented (simulation rule only) |
| D050 | Approver permissions: only a workspace owner or admin may decide | **Proposed** (implemented as the initial rule) | Implemented (M4-C finalization) |
| D051 | Trusted tool policy: an allowlist of exact tool versions, not declared metadata | Accepted | Implemented (M4-C finalization) |
| D052 | Durable suspension of a task waiting for approval | Accepted (supersedes D048 for plan tasks) | Implemented (M4-C finalization) |
| D053 | Approval lifecycle in the append-only run history | Accepted | Implemented (M4-C finalization) |
| D054 | Demo mode: an explicit, labelled, production-refusing way to run the journey without a model key | Accepted | Implemented (M5) |
| D055 | The launch-readiness workforce is the reference use case, with judgment from real tool results | Accepted | Implemented (M5) |
| D056 | The Studio home states one next step from real data and invents nothing | Accepted | Implemented (M5) |
| D057 | `X-Workspace-Id` is honoured only after server-side membership verification; owners create workspaces and add members | Accepted (2026-11-19 sprint directive) | Implemented (M6); amends the D025 reservation |
| D058 | In-app notifications are durable rows written in the approval-request transaction; the inbox badge counts what still waits | Accepted | Implemented (M6) |
| D059 | Model usage is recorded per provider request; cost is calculated from reported tokens with a versioned price, never estimated | Accepted (mechanism); the price table is configured, not verified | Implemented (M6) |
| D060 | A resumed task acquires capacity atomically with the consumption of its approval and holds a slot until it ends | Accepted | Implemented (M6) |
| D061 | The first real integration is `calendar.public_holidays` (Nager.Date): read-only, one fixed https host, auto-run allowlisted | Accepted | Implemented (M6); live-call verified |
| D062 | The primary button uses a deliberate accessible fill (deeper ember in light mode) | Accepted | Implemented (M6); axe-verified |
| D063 | Provider requests are retried with Retry-After-aware, jittered, bounded backoff, a provider cooldown and a concurrency limit; tool invocations never are | Accepted | Implemented (M6b) |
| D064 | A task that requires an action declares it structurally (`required_tool`); only a recorded successful invocation completes it | Accepted | Implemented (M6b) |
| D065 | A provider usage limit that asks for longer than a task may wait is not slept through; it ends as an honest, distinct `exhausted` | Accepted | Implemented (M6b); live-observed |
| D066 | Side-effecting tool calls have a durable dispatch ledger: authorized -> dispatching -> confirmed or unknown; unknown is never retried automatically | Accepted (P0 safety directive) | Implemented (P0) |
| D067 | Operation identity and the duplicate-prevention gate: replay a confirmed operation, refuse an in-flight or unknown one | Accepted | Implemented (P0) |
| D068 | Reconciliation: a separately authorised, audited protocol; a person's statement is never recorded as proof | Accepted | Implemented (P0) |
| D069 | Execution truth: a task is successful only if the server's dispatch ledger proves its external actions; unknown or confirmed-failed side effects block success whatever the model says | Directed (sprint 2); owner confirmation pending | Implemented |
| D070 | Per-user rate limits shared across processes through Redis, with a bounded per-process fallback | Directed (sprint 2); owner confirmation pending | Implemented |
| D071 | Provider cooldown and a global concurrency cap shared across processes through Redis (Lua, server clock, leases, bounded admission) | Directed (sprint 2); owner confirmation pending | Implemented; verified with 3 real processes |
| D072 | Observability baseline: bounded-label Prometheus metrics (token-protected), correlated JSON logs, separate readiness | Directed (sprint 2); owner confirmation pending | Implemented |
| D073 | The first real side-effecting integration is `email.send@1` through Resend: sandbox recipients by default, exact-action approval per call, stable idempotency key, NO key-based lookup claimed | Directed (sprint 4); owner confirmation pending | Implemented; real Resend calls UNVERIFIED |
| D074 | Live alerting without a new service (in-process evaluator + webhook, Redis de-duplication) and application-level edge protections (request-size limit, Host allowlist) behind a documented reverse-proxy configuration | Directed (sprint 4); owner confirmation pending | Implemented; production receiver/edge UNVERIFIED |

---

## Product decisions

### D001 — Product direction: Hybrid Autonomous AI Workforce Platform
- **Status:** Accepted
- **Context:** AgentForge today is a "multi-agent startup OS" demo: seven text-generating agents.
- **Decision:** Evolve into a hybrid autonomous AI workforce platform (autonomous agents + human oversight).
- **Reasoning:** Owner-approved product direction.
- **Consequences:** Agents must perform real, auditable work; requires tools, approvals, persistence, scheduling.
- **Implementation status:** Prototype only. Agents return text; "autonomous" actions are keyword labels (`backend/autonomous.py`).
- **Related:** `backend/agents/*`, `backend/orchestrator.py`, `backend/autonomous.py`.

### D002 — Primary users: individual builders and businesses
- **Status:** Accepted
- **Context:** Current identity is a Google email string; no accounts/orgs.
- **Decision:** Serve both individuals and businesses.
- **Reasoning:** Owner-approved.
- **Consequences:** Needs personal workspaces and team workspaces (ties to D016).
- **Implementation status:** Not implemented (no users/workspaces tables).
- **Related:** `backend/memory/long_term.py`, `frontend/app/api/auth/[...nextauth]/route.ts`.

### D003 — Workforce creation: prebuilt templates + custom agents
- **Status:** Accepted
- **Context:** Agents are 7 hard-coded Python modules, ~identical code, prompts in source.
- **Decision:** Ship prebuilt agent templates and allow user-defined custom agents.
- **Reasoning:** Owner-approved.
- **Consequences:** Agents become data (DB-defined prompt/model/tools/permissions) rather than modules.
- **Implementation status:** Partial: 7 prebuilt roles exist as code; no template store, no custom agents.
- **Related:** `backend/agents/*.py`, `backend/orchestrator.py` (`_agents` dict), `backend/telegram_bot.py` (`AGENT_DESCRIPTIONS`, duplicated list).

### D004 — Execution model: autonomous with configurable human approval
- **Status:** Accepted
- **Context:** No external actions exist; no approval concept.
- **Decision:** Agents may execute autonomously; approval policy is configurable per workspace/agent/tool.
- **Reasoning:** Owner-approved.
- **Consequences:** Requires action records, approval queue, audit log, and tool risk levels (D014).
- **Implementation status:** Not implemented. `status:"completed"` in `autonomous.py` is a keyword heuristic, not execution.
- **Related:** `backend/autonomous.py`, `backend/tasks/scheduler.py` (stores tasks but never runs them).

### D005 — LLM architecture: multi-provider from day one
- **Status:** Accepted
- **Context:** Groq SDK imported directly in 4 modules, client built in 9 places.
- **Decision:** Provider-agnostic LLM layer from the start of 2.0.
- **Reasoning:** Owner-approved; avoids vendor lock-in.
- **Consequences:** Introduce a provider interface + per-agent model config; route all calls through it.
- **Implementation status:** **Conflicts with existing code** (Groq-only; no abstraction; `langchain` is listed in requirements but unused). Existing implementation is not changed here.
- **Related:** `backend/agents/*.py`, `backend/improvement/analyzer.py`, `backend/voice_handler.py`, `backend/requirements.txt`.

### D006 — Initial deployment: single cloud VM, Dockerized app + worker containers
- **Status:** Accepted
- **Context:** Repo has no Dockerfiles/compose/NGINX; frontend hard-codes `https://65-0-30-8.sslip.io`, implying a single VM already exists (unverified); frontend appears to run on Vercel.
- **Decision:** Single VM, Docker for app and worker containers.
- **Reasoning:** Owner-approved; low cost and operational simplicity.
- **Consequences:** Need Dockerfiles, compose, reverse proxy/TLS, volumes, backups. Whether the Vercel frontend stays is an open question (not decided here).
- **Implementation status:** Not in repo → **conflict/unverified**.
- **Related:** `frontend/app/page.tsx` (`WS_URL`), other pages (`API_BASE`).

## Architectural decisions

### D007 — Modular monolith with independent background workers
- **Status:** Accepted
- **Context:** One FastAPI process holds API, WS, Telegram polling; no worker process.
- **Decision:** Modular monolith; long-running work in separate worker processes.
- **Reasoning:** Owner-approved.
- **Consequences:** Move Telegram polling and scheduled/agent execution out of the web process; define module boundaries.
- **Implementation status:** Partial (monolith exists, modules not decoupled; no workers).
- **Related:** `backend/main.py` (`lifespan`), `backend/telegram_bot.py`.

### D008 — Backend framework: FastAPI
- **Status:** Accepted. **Implementation:** Implemented (`backend/main.py`). Context: already in use. Decision/Reasoning: keep. Consequences: async-correctness work needed (sync Redis/SQLAlchemy currently block the loop). **Related:** `backend/main.py`, `backend/requirements.txt` (unpinned).

### D009 — Frontend: Next.js + TypeScript
- **Status:** Accepted. **Implementation:** Implemented (Next 16.2.9, React 19, TS 5; App Router). Context: already in use. Consequences: remove hard-coded URLs, add API client layer and typed contracts. **Related:** `frontend/app/**`, `frontend/package.json`.

### D010 — Primary database: PostgreSQL
- **Status:** Accepted. **Implementation:** Partial: SQLAlchemy models `agent_memories`, `telegram_links` only; schema via `create_all` at import; no migrations. Consequence: adopt migrations (e.g. Alembic) before schema growth. **Related:** `backend/memory/long_term.py`.

### D011 — Semantic retrieval: PostgreSQL + pgvector
- **Status:** Accepted. **Implementation:** Not implemented (no embeddings; long-term memory is raw replies, not read by agents). Consequences: requires pgvector-enabled Postgres image/extension, an embedding provider (interacts with D005). **Related:** `backend/memory/long_term.py`.

### D012 — Cache and messaging: Redis, Redis Streams as initial event/task transport
- **Status:** Accepted. **Implementation:** Partial: Redis used for chat window, a task hash and reports (no TTLs); no Streams/consumer groups/pub-sub. **Related:** `backend/memory/short_term.py`, `backend/tasks/scheduler.py`, `backend/improvement/self_improver.py`.

### D013 — Agent orchestration: structured plans and persisted dependency graphs (DAG)
- **Status:** Accepted
- **Context:** CEO output is free text; delegation is substring match on "sales agent" etc.; sequential; no aggregation; nothing persisted.
- **Decision/Consequences:** Replace with schema-validated plans, persisted task nodes with dependencies, state machine, retries, aggregation.
- **Implementation status:** Not implemented; existing delegation (`AgentOrchestrator._extract_delegations`) is a replacement candidate. **Related:** `backend/orchestrator.py`, `backend/agents/ceo.py`.

### D014 — Tool execution: standardized, permission-controlled interface
- **Status:** Accepted. **Implementation:** Not implemented (agents have no tools). Consequences: tool registry, JSON-schema inputs, per-tool permissions, audit trail, approval hooks (D004), credential access (D016). **Related:** none yet.

### D015 — Memory architecture: working, episodic, semantic, procedural, shared workspace
- **Status:** Accepted
- **Implementation status:** Partial. Working ≈ Redis `short_term:{email}:{agent}` (20 msgs); episodic ≈ PG `agent_memories` (unsummarized, write-only for agents); semantic/procedural/shared: absent. **Related:** `backend/memory/*.py`, `backend/improvement/*.py`.

### D016 — Security: workspace tenant isolation, RBAC, encrypted credentials
- **Status:** Accepted
- **Context:** Backend has **no authentication**; `user_email` is client-asserted in WS bodies, POST bodies and URL paths; Telegram linking is unverified; Twilio webhooks unauthenticated; no secrets store.
- **Decision:** Workspace-level isolation, RBAC, encrypted credentials.
- **Implementation status:** **Conflicts with existing code** (none implemented; see `flow.md` §18 and gap analysis below). Existing code not modified.
- **Related:** `backend/main.py`, `backend/telegram_bot.py`, `backend/voice_handler.py`, `frontend/app/api/auth/[...nextauth]/route.ts`.

### D017 — Documentation: `flow.md` and `decision.md` are mandatory living documents
- **Status:** Accepted. **Implementation:** Created by the Phase 0 audit (2026-11-12). Rule: update both in any change that alters flows, endpoints, schema, or decisions; decisions are append-only with new IDs.

---

## Conflict log (approved decisions vs. existing code)
| Decision | Conflict | Handling |
|---|---|---|
| D005 | Groq hard-wired, no provider layer | Document only; resolve in implementation phase |
| D006 | No Docker/compose in repo; hosting setup unknown | Verify actual VM; add containers later |
| D016 | No auth, spoofable identity, SSRF/credential leak in `/voice/process` | See gap analysis; fix before any real tool execution |
| D013/D014/D004 | Orchestration & "autonomous" are text heuristics | Treat existing as prototype only |

## Gap analysis (Phase 0, vs. AgentForge 2.0 target)

### A. Already implemented and reusable
- FastAPI app + WS plumbing shape (`main.py`) — as starting skeleton. Telegram/voice channel adapters (`telegram_bot.py` UX: agent switch, voice via Whisper, message splitting) — reusable after auth fix. Next.js shell, theming, NextAuth Google login, WS reconnect/ping client. Per-user key scoping convention.

### B. Implemented but needs refactoring
1. Seven duplicated agent modules → one generic agent runner + data-defined templates (D003). *Why:* each new agent is a code copy.
2. Direct Groq coupling → provider interface (D005).
3. Sync Redis/SQLAlchemy in async paths → async drivers or threadpool. *Why:* one slow query stalls all WS/Telegram users.
4. `create_all` at import → migrations (D010).
5. Hard-coded URLs/CORS → env-driven config.
6. Telegram polling inside API process → worker (D007).

### C. Partially implemented
Scheduler (store only, never executes — needs worker + Streams, D012); self-improvement (report-only, no feedback into prompts); long-term memory (stored, unused by agents); autonomous actions (labels only); history (no pagination).

### D. Missing entirely
Tool interface and real integrations (D014); approval/audit system (D004); DAG planner/executor with aggregation (D013); pgvector memory (D011); workspaces/RBAC/users (D016); encrypted credential vault; custom agent builder (D003); background workers; Docker/compose/NGINX/CI; tests; migrations; observability; LLM cost/usage tracking; real Digital Twin data model.

### E. Security vulnerabilities / risks (ranked)
1. **No backend authentication; identity = client-supplied email** (IDOR on `GET /history/{email}`, `POST/GET /improve`, WS, `/autonomous/run`). Anyone knowing an email reads that person's agent history. *Fix:* verify NextAuth JWT/session server-side, derive identity from token.
2. **Telegram account linking unverified**; deep-link token is base64(email), forgeable. *Fix:* one-time signed nonce issued to authenticated web session.
3. **`/voice/process` SSRF + credential leak:** `RecordingUrl` is attacker-controlled and fetched with Twilio basic-auth; no `X-Twilio-Signature` check. *Fix:* validate Twilio signature; restrict host to `api.twilio.com`.
4. Unauthenticated, unlimited LLM endpoints → cost-exhaustion DoS (no rate limits/quotas); WS has no origin check.
5. `/tasks` global and unauthenticated: anyone can list/delete everyone's scheduled tasks.
6. Error strings returned to WS clients (`Agent failed: {exc}`).
7. Prompt-injection surface will become serious once tools exist (D014).
8. Secrets hygiene: `.env.local` with a Vercel OIDC token exists locally (git-ignored; confirm never committed — history not scanned exhaustively); README ships a sample DB password.
9. Redis/Postgres exposure and auth settings unknown **[Unverified]**.

### F. Infrastructure limitations
Single process holds API+bot (multi-worker breaks Telegram polling); Redis short-term race (read-modify-write); no TTLs → unbounded growth; unbounded `/history`; no backups/health/readiness; no pinned dependencies; no CI/tests; hard-coded IP hostname.

### G. Unknown / requires verification
Actual production topology and TLS; Redis persistence config; real DB schema state; Vercel env (`NEXTAUTH_SECRET`); whether deployed frontend matches this commit (CORS only allows localhost yet frontend REST calls point to the VM); Git history for leaked secrets; Python version used.

## Recommended implementation order (proposal; needs approval)
1. Safety net: tests for orchestrator/memory + pinned deps + env-driven config + Dockerfiles/compose (D006) + Alembic baseline.
2. Identity: server-side auth, users/workspaces/RBAC skeleton, fix Telegram linking and Twilio validation (D016).
3. LLM provider abstraction + single generic agent runner with template data (D005, D003).
4. Workers + Redis Streams; move Telegram and scheduler execution out of API (D007, D012).
5. Plan/DAG schema + persistence + executor; replace keyword delegation (D013).
6. Tool interface, approvals, audit log (D014, D004).
7. Memory layers incl. pgvector (D015, D011); evaluation feedback loop.
8. Real Digital Twin from persisted data.

*No application code was modified during this audit. Further decisions (D018+) are appended below.*


---

## Decisions appended after Phase 0

> IDs D018–D021 are reserved for the M0.1 authentication decisions proposed in `docs/security/M0.1_AUTH_ARCHITECTURE.md` §8 and have **not** yet been recorded here; they will be appended when that work is implemented. The entries below were approved for M0.1 / T0 on 2026-11-12.

### D022 — Configuration: typed, fail-fast settings with explicit environment parsing
- **Status:** Accepted
- **Context:** Configuration was read from `os.environ` in ~20 scattered places with no validation, no environment concept, and a localhost-only CORS list; FastAPI docs were always on.
- **Decision:** A single frozen pydantic `BaseModel` (`backend/config.py`) built by explicit parsing (no `pydantic-settings`). `APP_ENV` defaults to `production`. Errors name variables, never values; secrets are `SecretStr`. Production CORS origins come only from `CORS_ALLOWED_ORIGINS` (default empty); credentials are disabled; methods `GET/POST/DELETE`, headers `Authorization/Content-Type`. Docs/OpenAPI are off in production and enabling them there fails startup validation. Auth signing-key settings are defined now but enforcement waits for T5.
- **Reasoning:** Fail-safe defaults; bearer-token design needs no credentialed CORS; avoids an extra dependency whose errors echo input.
- **Consequences:** An unset `APP_ENV` is production (dashboard origin must be configured); legacy modules keep direct env reads until migrated (tracked by an allow-list test).
- **Implementation status:** **Implemented** (T0) for settings, CORS and docs policy; **partial** overall (legacy env reads remain; JWT enforcement deferred to T5).
- **Related:** `backend/config.py`, `backend/app_factory.py`, `backend/main.py`, `backend/tests/test_config.py`, `backend/tests/test_app_policy.py`.
- **Relation to D016/D008:** supports D016 (security) and D008 (FastAPI) without changing them.

### D023 — Test isolation: SQLite-file tests, no network, no real `.env`
- **Status:** Accepted
- **Context:** There were no tests, and importing `main` required a live PostgreSQL (import-time `create_all`) and real credentials.
- **Decision:** pytest only (pinned `pytest==9.2.0` in `backend/requirements-dev.txt`); synchronous `TestClient`; `PYTHON_DOTENV_DISABLED=1` before imports; never a Telegram token in tests; SQLite file databases in `tmp_path` (allowed only with `APP_ENV=test`); an autouse guard blocks outbound network (TCP connect and non-local DNS); the unused `langsmith_plugin` (installed transitively with langchain) is disabled in `pytest.ini`. `create_all` moved from import time to explicit `init_db()` in the application lifespan (transitional until Alembic in T1).
- **Reasoning:** Deterministic, offline, safe against leaking a developer's `.env` or touching local services.
- **Consequences:** Importing the app no longer needs a database; tables are created at startup. `pytest-asyncio`, `fakeredis`, `pydantic-settings` deliberately not added.
- **Implementation status:** **Implemented** (T0): 70 tests passing locally on Python 3.11.9 / pytest 9.2.0.
- **Related:** `backend/tests/*`, `backend/pytest.ini`, `backend/memory/long_term.py` (`init_db`), supersedes nothing (implements part of D010's migration prerequisite; Alembic remains pending, D010 stays Partial).

### D024 — Startup schema-version guard; migrations are a separate explicit deployment step
- **Status:** Accepted (owner approval 2026-11-14)
- **Context:** Startup used the transitional `init_db()` / `Base.metadata.create_all()`, which cannot alter existing tables, hides drift, and lets any process create schema. Alembic (D010 prerequisite) now exists with baseline `0001_baseline`.
- **Decision:** `init_db()`/`create_all` are removed from application code. `lifespan` runs `backend/migrations/schema_guard.py::verify_schema_version`, a **read-only** check (no DDL, DML, `create_all`, upgrade, stamp or downgrade) that accepts startup only when the script directory has exactly one head and the database records exactly one non-empty, known revision equal to that head. PostgreSQL: `current_schema()` must be `public`, checked before `alembic_version` is read; a query failure or non-public/NULL schema fails closed. Behaviour is **identical in every `APP_ENV`** (production, development and test are all strict); there is no auto-migration anywhere in startup. Failures raise `SchemaVersionError` carrying a fixed code and message only (never URL, host, user or raw driver text); PostgreSQL connections use a 10-second connect timeout and a read-only transaction request. Tests build schemas with the real `alembic upgrade head` on temporary SQLite files.
- **Reasoning:** Fail-closed startup prevents running against an unmanaged or unexpected schema; keeping migrations out of the web process avoids multi-replica races and unexpected DDL at boot; one code path in all environments avoids "works in dev" drift.
- **Consequences:** An unadopted database (no `alembic_version`) prevents startup, so each environment must be adopted per `docs/database/T1_G_ADOPTION_RUNBOOK.md` **before** this code is deployed; brand-new environments must run `alembic upgrade head` before first start. `alembic.ini` and `migrations/versions/` must ship with the backend (missing scripts fail closed). The guard checks the recorded revision only, not schema parity. Restarting older code against a database already upgraded past its scripts fails with `UNKNOWN_REVISION`, so future migrations need expand/contract ordering.
- **Implementation status:** **Implemented** (T1-H) and tested on SQLite and with mocks only. PostgreSQL runtime behaviour (`current_schema()`, `postgresql_readonly`, `connect_timeout`, real driver errors) is **unverified**; no environment has been adopted or deployed.
- **Related:** `backend/migrations/schema_guard.py`, `backend/main.py`, `backend/memory/long_term.py`, `backend/tests/test_schema_guard.py`, `backend/tests/test_app_startup.py`, `backend/tests/conftest.py`; refines D010 and D023 (the `init_db()` transition described in D023 is superseded for startup).


---

## M1 Platform Foundation decisions (appended 2026-11-14)

> Approved by the product owner on 2026-11-14 in the corrected M1 implementation plan. IDs D018–D021 were reserved in the index above for the M0.1 authentication decisions; they are recorded here and the original "reserved" index row is left untouched as history. **Approval ≠ implemented**: every entry below is **not implemented** unless its status line says otherwise.

### Clarification — 2026-11-14 (append-only; no earlier entry is edited)
- **D018–D021:** previously reserved IDs, now recorded below. **D025–D029** are new.
- **D010:** unchanged. For the record: since D010 was written, an Alembic baseline (`0001_baseline`), a pre-flight classifier, a startup schema guard (D024) and PostgreSQL 16.15 runtime verification (T1-I) exist in the repository; D010 stays **Partial** until the M1 identity/workspace schemas land. No environment has been adopted or deployed.
- **D023:** its statement that `init_db()` creates tables at startup is superseded for startup by D024.
- **Implementation defaults for M1 (defaults, not decisions; they may be changed by a dated entry):** access tokens carry JWT header `typ: "JWT"` and a claim `token_use: "access"` (both validated by the backend); new tables and constraints use explicit names, with no metadata-level naming convention; there are no per-request database writes for identity (no `last_login_at`; a stored email is updated only when a verified token carries a different one); WebSocket close codes are 4401 (unauthenticated, invalid, timed out), 4403 (Origin not allowed) and 1009 (message too large); in production the WebSocket Origin allow-list is empty until configured.

### D018 — API access tokens: Next.js-issued ES256 JWT verified by FastAPI
- **Status:** Accepted (2026-11-14)
- **Context:** The backend has no authentication (identity is a client-supplied email). The frontend uses NextAuth (Google). Cross-origin browser REST and WebSocket clients must reach FastAPI.
- **Decision:** The Next.js server validates the NextAuth session server-side and issues a short-lived API JWT; FastAPI verifies it. The API token lifetime is exactly 10 minutes; the NextAuth session lifetime is 7 days. Tokens are signed with ES256. The private key exists only in the Next.js/Vercel server environment; FastAPI receives public keys indexed by `kid`. The verifier pins the algorithm to ES256 and validates `kid`, issuer, audience, expiry, issued-at, token type and required claims, with 30 seconds of clock leeway. `sub` is the provider subject and `idp` identifies the provider; claims also include `email`, `email_verified`, `jti` and the token type. Node's built-in crypto is preferred for signing if compatibility and tests prove it reliable. The token endpoint is POST-only, same-origin checked, returns `Cache-Control: no-store` and `Pragma: no-cache`, answers 405 for other methods and 401 without a valid session. A verified email is required for Google sign-in and for token issuance.
- **Reasoning:** Works across origins and for WebSocket; the backend verifies without NextAuth internals; asymmetric keys keep the backend unable to mint tokens; short lifetime bounds revocation lag.
- **Consequences:** Next.js becomes the token issuer and holds the private key; key rotation uses `kid` with an overlap window; frontend and backend authentication integration must be deployed together (D028).
- **Implementation status:** **Not implemented.** Prerequisites verified in M1-0 (2026-11-14): PyJWT 2.13.0 and cryptography 50.0.2 pinned and installed; Node built-in ES256 signing with IEEE-P1363 encoding verified by `frontend/tests/es256.test.mjs`, independently by `jose` and by PyJWT.
- **Related:** `docs/security/M0.1_AUTH_ARCHITECTURE.md`, `backend/config.py` (`auth_jwt_*` settings, unused so far), D019, D022, D026, D028.

### D019 — Identity model: internal UUID users, `(provider, provider_subject)`, email as an attribute
- **Status:** Accepted (2026-11-14)
- **Context:** Identity is currently an email string used as a key in Redis, PostgreSQL and URLs.
- **Decision:** Users have an internal UUID identity. An external identity is uniquely identified by `(provider, provider_subject)`; the JWT `sub` carries the provider subject and `idp` the provider, and the backend resolves the internal UUID through that mapping. Email is an attribute, never an identity key. There is no automatic account linking by email. A user is provisioned on the first valid authenticated identity token, subject to the identity mapping. No client-supplied `user_email` or similar field may establish identity.
- **Reasoning:** Emails change and collide; a stable subject-based mapping is needed for workspaces and for separating accounts safely.
- **Consequences:** Two external identities with the same email are two users; nothing merges them silently. Inactive users are rejected.
- **Implementation status:** **Not implemented** (planned: M1-A schema `0002_identity`, M1-D backend authentication).
- **Related:** D018, D025, D027.

### D020 — Telegram secure linking (separate mandatory pre-production task S1)
- **Status:** Accepted (2026-11-14) as a scope definition; implementation is a separate, separately reviewed task.
- **Context:** `/link <email>` and a base64-email `/start` deep link let anyone attach a chat to any email; the bot has no private-chat restriction.
- **Decision:** Disable and invalidate the legacy `/link <email>` and base64 `/start` flows. Linking is private-chat-only; group chats are rejected. Linking uses a secure, expiring, single-use challenge. Ownership and revocation rules are explicit and silent reassignment is prevented. Tests cover forged, replayed, expired and cross-user link attempts.
- **Reasoning:** Linking must prove control of an authenticated account, not assert an email.
- **Consequences:** Until S1 ships, M1 makes the Telegram handlers fail closed (no Principal can be built from legacy links); the bot is not usable for agent execution. Not authorized for production changes.
- **Implementation status:** **Not implemented.**
- **Related:** D019, D029, `backend/telegram_bot.py`.

### D021 — Twilio webhook and SSRF protection (separate mandatory pre-production task S2)
- **Status:** Accepted (2026-11-14) as a scope definition; implementation is a separate, separately reviewed task.
- **Context:** `/voice/*` webhooks are unsigned, and `RecordingUrl` is fetched with Twilio credentials without a host check.
- **Decision:** Validate Twilio request signatures officially, over the exact external URL and request parameters. Restrict `RecordingUrl` to permitted Twilio media hosts and expected paths. Prevent redirects from forwarding credentials. Apply connection and read timeouts and response-size limits. Sanitize all failures. Tests cover forged signatures, malicious URLs, redirects, private IPs, oversized responses and invalid content types.
- **Reasoning:** Webhook identity is the signature, and an attacker-controlled URL must never receive credentials.
- **Consequences:** Until S2 ships the webhook and SSRF findings remain open; an unsigned caller can also choose a `CallSid` namespace. Not authorized for production changes.
- **Implementation status:** **Not implemented.**
- **Related:** D027 (voice isolation), D029, `backend/voice_handler.py`.

### D025 — Workspaces, memberships and authorization
- **Status:** Accepted (2026-11-14)
- **Context:** D016 requires workspace isolation and RBAC; no users, workspaces or authorization exist.
- **Decision:** Roles are `owner`, `admin` and `member`; there is no viewer role. A personal workspace is created in the same transaction as a new user. A workspace must always have an owner; the invariant is enforced in application/service logic and tested. `X-Workspace-Id` is reserved and every non-empty value is rejected throughout M1. Resource access is scoped through the authenticated Principal and explicit authorization checks; possession of a workspace UUID is never authorization. Role validity uses a named CHECK constraint, and the T1-F pre-flight is extended to compare declared CHECK constraints without weakening its existing checks.
- **Reasoning:** Isolation must come from enforced membership, not from identifiers; personal workspaces must not substitute for authorization.
- **Consequences:** Enabling the workspace header for member workspaces is a later, separately approved step.
- **Implementation status:** **Not implemented** (planned: M1-B schema `0003_workspaces`, M1-E authorization).
- **Related:** D016, D019, D027.

### D026 — WebSocket authentication
- **Status:** Accepted (2026-11-14)
- **Context:** `/ws` is anonymous, has no Origin check, accepts an email per message and returns raw exception text.
- **Decision:** Validate Origin against the configured allow-list. Authenticate through the first WebSocket message within 5 seconds; invalid or absent authentication closes the connection with the documented close code. Bind the verified Principal to the connection and never accept identity from later frames. Enforce message-size limits, one in-flight job per connection, generic errors and token-expiry handling.
- **Reasoning:** Browsers cannot set WebSocket headers and tokens in URLs are logged; the first-message pattern avoids both.
- **Consequences:** Clients must re-authenticate on reconnect and before token expiry.
- **Implementation status:** **Not implemented** (planned: M1-F).
- **Related:** D018, D022 (allowed origins).

### D027 — Memory ownership, history pagination, legacy quarantine and voice isolation
- **Status:** Accepted (2026-11-14)
- **Context:** `agent_memories` is keyed by a client-supplied email; history is unbounded; unscoped fallbacks and a shared voice identity exist.
- **Decision:** History pagination defaults to 50 records with a maximum page size of 100, using an opaque cursor over `(created_at, id)` with stable ordering. Legacy `agent_memories` rows are never backfilled by email and stay inaccessible through Principal-scoped queries unless a separately approved recovery process is created. Unscoped Redis and memory fallbacks are removed. Voice uses an anonymous service principal with an isolated `CallSid` namespace and never reads or writes user memory. No legacy memory backfill is performed.
- **Reasoning:** An email is a claim, not ownership; legacy data cannot be attributed safely.
- **Consequences:** Legacy PostgreSQL memories and old Redis keys are neither exposed nor silently deleted; they become unreachable by normal code paths. Recovery would need its own decision.
- **Implementation status:** **Not implemented** (planned: M1-C schema `0004_memory_ownership`, M1-E, M1-F).
- **Related:** D019, D025, D021.

### D028 — Frontend token handling, dependencies and environment configuration
- **Status:** Accepted (2026-11-14)
- **Context:** The frontend sends the user's email, hardcodes the backend hostname, and has no test runner.
- **Decision:** Alembic is already pinned; PyJWT and cryptography are approved and must be pinned before use. Frontend and server-side authentication tests use `node:test` unless a concrete limitation requires a separately approved dependency. Tokens stay in browser memory only, never `localStorage` or `sessionStorage`. Hardcoded backend hostnames are removed in favour of validated environment configuration. Frontend and backend authentication integration must be deployed together.
- **Reasoning:** Minimal new dependencies; limit XSS exposure of tokens; a mixed deployment would break every authenticated call.
- **Consequences:** `jose` is present only as a transitive dependency of next-auth and is used only to verify tokens in tests.
- **Implementation status:** **Partially implemented (M1-0, 2026-11-14):** PyJWT 2.13.0 and cryptography 50.0.2 pinned in `backend/requirements.txt` and installed; `node:test` baseline added. Token handling, environment configuration and client migration are not implemented (M1-G, M1-H).
- **Related:** D018, `frontend/tests/es256.test.mjs`, `backend/requirements.txt`.

### D029 — Release gating: rate limiting, production authorization and safety claims
- **Status:** Accepted (2026-11-14)
- **Context:** M1 builds the identity boundary but not abuse controls or the remaining Critical-finding fixes.
- **Decision:** Rate limiting is a separate task and is mandatory before production deployment; it is not part of M1 implementation. Production deployment, migration, stamp, AWS access and production database operations remain unauthorized. There is no production credential rotation or infrastructure change in M1. Local test results are not a claim of production safety.
- **Reasoning:** Keeps M1 reviewable and prevents a green local suite from being read as release readiness.
- **Consequences:** Production release additionally requires S1, S2 and the rate-limiting task to be accepted, the per-environment database adoption and migrations to be authorized, and a coordinated frontend/backend release.
- **Implementation status:** **Not implemented** (gating rules only).
- **Related:** D020, D021, D024, `docs/database/T1_G_ADOPTION_RUNBOOK.md`.

### D030 — Plan execution: in-process scheduler on the M2-B runtime, polling progress, at-most-once dispatch
- **Status:** Accepted (2026-11-16, M3-B; pending review)
- **Context:** Approved plans (M3-A) must execute reliably in dependency order, against the exact approved agent versions, without a second runtime.
- **Decision:** Every task runs through the existing `ExecutionService.run` (new optional `pin` and `on_started` hooks only). A per-run asyncio scheduler in the API process drives the DAG; run and task-run state is persisted (migration 0008) with database-enforced legal transitions and partial unique indexes (one active run per plan, one succeeded run per revision). Dispatch is at-most-once per task (a compare-and-set that also links the M2-B execution), nothing is retried automatically, and a new run is the explicit retry. Progress is exposed by REST polling; the WebSocket protocol is unchanged.
- **Reasoning:** One runtime keeps limits, records, timeouts and safe errors identical; the app is one process and executions are I/O-bound, so a queue/worker tier would add infrastructure without a current need; exactly-once cannot be proven with external LLM calls, so the guarantee claimed is the one the persistence actually provides.
- **Consequences:** Limits, the run registry and the orphan sweep are per process; a crash mid-task marks it `failed/interrupted` (outcome of the provider call unknown); multi-process scale-out requires a shared limiter, leased driver ownership and a queue (M4).
- **Implementation status:** **Implemented (M3-B)**; not deployed.
- **Related:** D030 builds on the M2-B execution runtime and M3-A plans; `agentplatform/plan_runs.py`, `migrations/versions/0008_plan_runs.py`, `flow.md` §25.14.


### D031 — Run ownership: PostgreSQL-backed leases with fencing in a separate `run_leases` table
- **Status:** Accepted (2026-11-17, owner approval for M4-A1: D-1, D-2, D-3)
- **Context:** The M3-B scheduler is in-process (D030) and nothing recorded which process drives a running run, so a second process or a restart could not tell a live run from an orphaned one (`flow.md` §25.16/§25.17, risks R1/R4/R5, scenario F12).
- **Decision:** A running plan run is owned through a lease row (`run_leases`: run, owner id, generation = fencing token, expiry, persisted cancel request). Acquisition, renewal, takeover and release are conditional single-statement updates; every driver write checks `(owner, generation)` in the same transaction (a stale owner can neither complete a task, propagate a result, finish or close a run, nor overwrite a newer owner's state). On PostgreSQL every expiry is computed and compared with the database clock. No Redis Streams or other new infrastructure (D012 is not used for this). The lease is a SEPARATE table because the accepted `plan_runs` triggers (migration 0008) abort any UPDATE of a running run. Initial settings: lease TTL 30 s, heartbeat 10 s, recovery sweep 10 s (code constants `LeaseConfig`).
- **Reasoning:** State and ownership commit atomically in the one database that is already the source of truth; the accepted M3-B triggers and contracts stay untouched; the same code runs on SQLite (development, tests) and PostgreSQL.
- **Consequences:** Migration `0009_run_leases` (additive, one table, reversible); a run never exists without a lease (created in the run's own transaction). Multi-process safety of the plan-run state machine is proven only where tested on PostgreSQL (see `flow.md` §25.18); standalone (REST/WebSocket) executions still have no owner record (D032).
- **Implementation status:** **Implemented (M4-A1)**; not deployed.
- **Related:** D030, D012, `backend/agentplatform/run_leases.py`, `backend/agentplatform/plan_runs.py`, `backend/migrations/versions/0009_run_leases.py`, `flow.md` §25.16–25.18.

### D032 — Plan-run recovery is lease-based; the unconditional startup sweep of plan runs is replaced; standalone executions keep their semantics
- **Status:** Accepted (2026-11-17, owner approval for M4-A1: D-4, D-5)
- **Context:** The M3-B startup sweep closed EVERY running run and execution at any process start, so a second process (rolling deploy, overlapping restart) would interrupt another process's live work.
- **Decision:** At startup and every 10 s a process closes only the running plan runs whose lease is expired or missing, once (the take-over is a compare-and-set), after reconciling work that was already persisted (a task whose linked execution ended becomes that outcome). Recovery NEVER re-dispatches: a task that was in flight ends `failed`/`interrupted` (the provider call's outcome is unknown; this is not claimed to be "did not happen"), unstarted tasks end `skipped`, the run ends `partial` or `failed`; the linked execution records are closed with their run. The standalone-execution startup sweep (`ExecutionStore.fail_orphaned`) keeps its single-process semantics and gets NO age policy; its one change is that it no longer touches executions that belong to a task of a still-running plan run (their run's owner, or lease recovery, closes them).
- **Reasoning:** Satisfies "a second process must not interrupt a live run" without inventing a recovery policy for executions that have no owner record. The status vocabulary of the accepted schema (`failed`/`interrupted`) is the safest existing outcome; no new status was added.
- **Consequences:** After a crash a run is closed within about one lease term plus one sweep interval instead of at once (a user-visible delay); runs that predate the migration have no lease and are recovered at the first startup. A late answer of a taken-over driver is not adopted. A standalone execution of another live process is still closed by a second process's startup (known, unchanged; needs a per-execution owner, planned for M4-A2).
- **Implementation status:** **Implemented (M4-A1)**; not deployed.
- **Related:** D030, D031, `backend/agentplatform/plan_runs.py` (`recover_expired`), `backend/agentplatform/execution.py`, `backend/main.py` (lifespan), `flow.md` §25.18.

### D033 — Persisted cancellation requests and graceful shutdown
- **Status:** Accepted (2026-11-17, owner approval for M4-A1: D-6 and the shutdown requirements)
- **Context:** Cancelling a run only worked in the process that drove it; from another process the database rows were closed while the real driver kept calling the provider (risk R4).
- **Decision:** Cancelling writes a persisted request (`run_leases.cancel_requested_at`, any process may, idempotent); the owner observes it at its next heartbeat (at most 10 s) or immediately when the request is served by the owner process, starts no further task, cancels its in-flight calls where the client supports cancellation, and records `cancelled`. A non-owner never writes run state while the lease is live; if the lease is expired it takes the run over and closes it as cancelled. Graceful shutdown refuses new runs (`503 shutting_down`), stops dispatching, cancels running tasks with a bounded wait and records `failed`/`interrupted`, and releases a lease only after its driver can no longer write; a driver that does not stop in time keeps (and renews) its lease until it ends. A driver that cannot renew for a whole term (for example, an unreachable database) stops itself without writing.
- **Reasoning:** A cancellation must be observable across processes without letting a non-owner mutate a run it does not own; fencing guarantees that a stale owner cannot overwrite it.
- **Consequences:** An already-dispatched provider request cannot be recalled once sent: cancelling aborts the client's wait, and whether the provider still processed the request is unknowable (no tool or external side effect exists today). The REST contract is unchanged apart from the new fixed `503 shutting_down` error for a stopping process.
- **Implementation status:** **Implemented (M4-A1)**; not deployed.
- **Related:** D030, D031, `backend/agentplatform/plan_runs.py`, `backend/agentplatform/plan_runs_api.py`, `flow.md` §25.18.

### D034 — M4-A1 scope boundaries
- **Status:** Accepted (2026-11-17, owner approval for M4-A1: D-7)
- **Context:** M4 is split into bounded milestones (`flow.md` §25.17); A1 is ownership and recovery only.
- **Decision:** Out of M4-A1: tool registry and tool execution, human approval gates, automatic retries or re-dispatch, a general-purpose durable queue, a new event/audit system, database-enforced global concurrency limits, Redis or any other new infrastructure, and any frontend change.
- **Reasoning:** Keeps the change reviewable and the accepted M1–M3 contracts intact.
- **Consequences:** `Limits` and the per-user active-run cap remain per process; there is still no audit/event log and no queued state (M4-A2); no side-effecting capability exists (M4-B/M4-C).
- **Implementation status:** **In force.**
- **Related:** D004, D012, D013, D014, D030–D033, `flow.md` §25.17.


### D035 — Task and execution records start and finish in one fenced transaction
- **Status:** Accepted (2026-11-18, owner directive for M4-A2 Part 1)
- **Context:** In M4-A1 the execution record and the task run were created and finished in separate commits (risk: an execution without its task link, or a succeeded execution with an unfinished task).
- **Decision:** A plan task starts (`start_task`: execution record + admission + owner row + `pending→running` link + event) and finishes (`finish_task_execution`: execution + task run + event) in ONE transaction that first checks the lease fence. A stale owner writes nothing; both records then stay `running` for lease recovery. Custom-agent memory is written after the commit.
- **Reasoning:** The smallest boundary that makes the two records agree without changing the accepted `plan_runs`/`agent_executions` schemas or triggers.
- **Consequences:** A result is used only after it is committed; a database failure at either boundary leaves both records unchanged and recovery closes them together. Standalone executions keep their simple finish.
- **Implementation status:** **Implemented (M4-A2)**; not deployed.
- **Related:** D030–D033, `agentplatform/plan_runs.py`, `agentplatform/execution.py`, `flow.md` §25.19.

### D036 — Explicit retry attempts with carried-over results
- **Status:** Accepted (2026-11-18, owner directive for M4-A2 Part 2) for the mechanism; the classification table is **Proposed — awaiting owner approval**.
- **Context:** A new attempt re-ran every task, repeating the work (and, in the future, the effects) of tasks that had already succeeded.
- **Decision:** A retry is a new attempt of the same approved revision of the latest run that ended `partial`, `failed` or `cancelled`: succeeded tasks are carried over (`plan_task_carryovers`; never dispatched again, their persisted results feed dependants), the rest run again at their pinned versions, the attempt is linked to the one it retries (`plan_run_retries`, unique per source run) with the authorising user and the acknowledged tasks. **Proposed:** a dispatched task is `uncertain` unless the provider rejected the request (`rate_limited`, `unavailable`, `auth`, `unsupported_model`, `unsupported_provider`, `bad_request`); an uncertain task is retried only when acknowledged per task.
- **Reasoning:** Preserves successful work, makes every retry decision explicit and auditable, and keeps an unknown outcome from ever being retried silently.
- **Consequences:** No new status was added (the schema cannot say "outcome unknown"; `failed/interrupted` plus the retry class carries it). `POST /plans/{id}/runs` still starts a full new attempt.
- **Implementation status:** **Implemented (M4-A2)**; not deployed.
- **Related:** D013, D030, `flow.md` §25.19.

### D037 — Durable worker entrypoint on the same ownership protocol
- **Status:** Accepted (2026-11-18, owner directive for M4-A2 Part 3) for the entrypoint and `api-only` mode; automatic continuation of orphaned runs is **Proposed — awaiting owner approval**.
- **Context:** Runs were driven only inside the API process.
- **Decision:** `python worker.py` runs the embedded code on its own: it heartbeats a process row, claims runs by lease takeover, and shuts down gracefully. `RUN_EXECUTION=api-only` makes the API record runs as unclaimed for workers; `embedded` (default) is unchanged. **Proposed:** a worker continues an orphaned run only when no task is in flight (finished tasks kept, pending ones dispatched for the first time); a run with a task in flight is closed by recovery and never re-dispatched. The run's scope is re-authorised from the database at claim time.
- **Reasoning:** One protocol, no queue, no Redis, no second service; reliability comes from ownership, fencing and persisted state.
- **Consequences:** An api-only deployment requires a worker; unclaimed runs wait (visible as `ownership: unclaimed`; no timeout policy yet).
- **Implementation status:** **Implemented (M4-A2)**; not deployed.
- **Related:** D031–D033, D012 (Redis not used), `agentplatform/worker.py`, `worker.py`.

### D038 — Append-only execution event history
- **Status:** Accepted (2026-11-18, owner directive for M4-A2 Part 4); the event vocabulary is **Proposed — awaiting owner approval**.
- **Context:** D004 requires an audit trail; terminal rows alone show no timeline, actor or approval.
- **Decision:** `execution_events` records run/task lifecycle facts in the same transaction as the transition, scoped by user and workspace, totally ordered by `id`, immutable by trigger, with details restricted to a whitelist (codes, keys, ids, numbers: never task text, results or secrets). The API returns the actor type only.
- **Reasoning:** An event exists if and only if its transition committed; the closed vocabulary keeps it an audit record, not a log.
- **Consequences:** Plan runs only; no retention policy; lease renewals are not recorded; no UI beyond a client method.
- **Implementation status:** **Implemented (M4-A2)**; not deployed.
- **Related:** D004, `agentplatform/run_events.py`, `flow.md` §25.19.

### D039 — Process ownership of executions and database-wide limits
- **Status:** Accepted (2026-11-18, owner directive for M4-A2 Part 5) for the mechanism; the default numbers and the recovery delay are **Proposed — awaiting owner approval**. It partially supersedes the standalone-execution rule recorded in D032 (D032 itself is not edited).
- **Context:** Limits were per process, the per-user run cap was check-then-insert, and a standalone execution had no owner so any process starting up closed it.
- **Decision:** Every process heartbeats `worker_processes`; every execution records its owner. Standalone recovery closes a running execution only when its owner process is not alive (legacy executions without an owner keep the old startup rule). Admission is checked in the execution-creating transaction against live processes' executions, serialised by a PostgreSQL advisory lock (SQLite: write lock); the per-user active-run cap is serialised per user. **Proposed:** DB limits 16 total / 3 per user.
- **Reasoning:** PostgreSQL is the coordination layer; no Redis.
- **Consequences:** After a crash a process's standalone executions are recovered when its row expires (≈ one TTL), not at another process's startup; executions of dead processes do not occupy slots.
- **Implementation status:** **Implemented (M4-A2)**; not deployed.
- **Related:** D031, D032, D030, `agentplatform/processes.py`, `agentplatform/execution.py`.

### D040 — Code-defined, versioned, immutable tool registry with exact pinning
- **Status:** Accepted (2026-11-18, owner directive for M4-B) for the mechanism. **Proposed — awaiting owner approval:** emitting tool lifecycle events into `execution_events` (not implemented; the invocation rows are the record).
- **Context:** Agents could only write text. M4-B needs real, auditable tool use without letting a model or a user define what runs.
- **Decision:** Tools are trusted code registered at start-up as immutable `ToolSpec`s with an integer version; a version is never edited, a change is a new version. Agent definitions store `id@version` (pinned by the server when saved); resolution prefers the newest active version, never a disabled one. Ids are `namespace.name`; provider function names are derived (`ns__name__v1`).
- **Reasoning:** An approved agent must not change behaviour because a tool changed; pins make that true. Code-defined tools keep the attack surface to reviewed code.
- **Consequences:** Adding a tool is a code change and a release; a deprecated or disabled version is visible and, if disabled, not runnable; there is no user-defined or model-generated tool.
- **Implementation status:** **Implemented (M4-B)**; not deployed.
- **Related:** D004, D039, `agentplatform/tools/registry.py`, `flow.md` §25.21.

### D041 — Durable, fenced tool invocation: intent before effect, never silently re-run
- **Status:** Accepted (2026-11-18, owner directive for M4-B).
- **Context:** A tool call is a side-effect boundary; after a crash we must know what may have happened.
- **Decision:** One row per call in `tool_invocations`, written `running` before the handler runs (inside a lease-fence check for plan tasks), finished exactly once, protected by a database trigger. Unique `(execution_id, seq)` and idempotency key make a duplicate dispatch impossible. Crash recovery closes in-flight rows with their execution: `interrupted`, or `uncertain` for tools with side effects. A call is never re-run automatically after an uncertain/interrupted outcome; a retry attempt makes new rows. Refusals (not granted, unknown, unavailable, approval required, invalid arguments) are recorded as `rejected`. Stored inputs/outputs are redacted; validation errors carry field paths only.
- **Reasoning:** Same ownership/fencing model as plan runs (D031–D035); the audit record is the product of the layer.
- **Consequences:** Exactly-once external effects are **not** claimed (no tool has external effects yet). CPU-bound handler threads cannot be killed on timeout.
- **Implementation status:** **Implemented (M4-B)**; migration `0011_tools`; verified on SQLite and a disposable PostgreSQL; not deployed.
- **Related:** D031, D035, D038, `agentplatform/tools/invoker.py`.

### D042 — Policy: only read-only, side-effect-free tools are executable; approvals are M4-C
- **Status:** Accepted (2026-11-18, owner directive for M4-B) for the boundary. **Proposed — awaiting owner approval (for M4-C):** that an approval is bound to the exact tuple (tool id, exact version, validated canonical arguments / `args_hash`, execution and idempotency identity) already stored on each row, and that plan approval never counts as tool permission.
- **Context:** Tools that act on the world need a human gate that does not exist yet.
- **Decision:** `ToolPolicy` allows `risk=read` with `side_effect=none` only; anything else is `requires_approval` and not executable (the registry may list it, the picker disables it, the invoker refuses it with `approval_required`). The first set is five pure tools: `math.calculate`, `finance.unit_economics`, `text.analyze`, `data.summarize_csv`, `time.date_math`.
- **Consequences:** No approval gate, no side-effect tool and no network/file/shell tool exists in M4-B.
- **Implementation status:** Policy **implemented (M4-B)**; approval binding **not implemented**.
- **Related:** D040, D041, `flow.md` §25.21.

### D043 — Provider-neutral tool calling and a bounded agent loop
- **Status:** Accepted (2026-11-18, owner directive) for the design; the numeric limits are **Proposed — awaiting owner approval:** 4 rounds, 8 calls per execution, 4 per round.
- **Decision:** The provider layer carries `ToolDefinition`/`ToolCall`/tool messages in neutral types; the Groq adapter translates to its OpenAI-compatible format. The runtime offers only the agent's pinned tools, re-validates every requested name and argument set (model output is untrusted), tells the model results are data not instructions, and requests the final answer without tools.
- **Consequences:** **Live provider function calling has not been verified** (no credential); the adapter is covered with a fake SDK client and the loop with a scripted provider. Built-in agents get no tools.
- **Implementation status:** **Implemented (M4-B)**.
- **Related:** D040, D041, `agentplatform/providers.py`, `agentplatform/execution.py`.

### D044 — The Studio presents tools read-only, from real data
- **Status:** Accepted (2026-11-18, owner directive for M4-B); visual design choices are product decisions of this milestone and are open to owner review.
- **Decision:** Catalog, detail, activity and the run-page trail show only backend data; the agent editor picks tools from the real catalog (non-executable ones disabled, no free text); no screen or client method can run a tool; stored input/output is rendered as text.
- **Implementation status:** **Implemented (M4-B)**; verified in a real browser on an isolated stack with a scripted provider.
- **Related:** D040, D042, `frontend/app/studio/tools/`.

### D040–D044 review — addendum of 2026-11-18 (M4-B final verification pass)
No status below was changed to Accepted by this addendum; every item marked **Proposed** stays **Proposed — awaiting owner approval**.
- **D041 — added requirement (follows from the verified limitation):** the invoker stops *waiting* at a tool's time limit but cannot terminate a running Python thread. The five shipped tools are bounded by construction (worst-case input measured at ≤ 25 ms; one day-by-day loop in `time.date_math` was replaced by a constant-time formula); a future tool that can block (network, file, subprocess, unbounded work) must enforce its own deadline or run out of process before it is registered. A timed-out call is recorded `timed_out`, never as a success, and is never re-dispatched. Status unchanged: **Accepted**.
- **D043 — recommendation (not a decision):** the proposed limits of **4 tool rounds, 8 tool calls per execution and 4 calls per round** are a sound **initial** setting. Why: (1) per execution that is at most 5 model requests (4 tool rounds plus the final answer), at most 8 sequential tool executions and at most 8 invocation rows, so cost, time and storage per task are bounded; (2) with at most 8 concurrent executions per process (3 per user), at most 8 tool threads are in flight per process, and each shipped tool finishes in milliseconds, so no accumulation is possible; (3) the shipped tools are single-shot calculations, so typical tasks need 1–3 calls and 1–2 rounds (the browser scenarios used 1–2 calls and 1 round), leaving headroom for a compute-then-verify pattern; (4) the limits are module constants, enforced by the runtime and recorded as `limit_exceeded` rows, so they can be tightened or raised without a migration. Two caveats that the owner should weigh: the numbers are **not measured against a live model** (none was available), and each tool result may be up to 12 KB (the table summary), so a worst-case execution adds up to ≈96 KB of tool results to the model context. A cap on the total tool-result size per execution is **proposed as a possible follow-up, not implemented**. Recommended action: approve 4/8/4 as the initial release values with a review after live-provider measurements. This is a recommendation only; D043's limits remain **Proposed**.
- **D042 (M4-C approval binding):** unchanged and kept separate from M4-B acceptance. Each invocation row already stores the tool id, exact version, validated canonical arguments (`args_hash`), execution and idempotency identity; nothing of M4-C was implemented.
- **D040:** unchanged (event vocabulary remains Proposed). **D044:** unchanged; a light-theme contrast fix (three colour tokens) was made to satisfy the tool screens' accessibility; the brand ember colour on earlier screens (3.55–3.97:1 on the light theme) is an open owner decision.

### D043 — owner approval of the execution limits (2026-11-18)
- **Status change:** the limits **4 tool rounds per task, 8 tool calls per execution, 4 calls per round** are **Accepted** (owner decision at M4-B closure). This addendum supersedes only the "Proposed — awaiting owner approval" marker on those three numbers; the design, the rationale recorded in the D040–D044 review addendum above and the implementation are unchanged.
- **Future considerations (not approved, not implemented):** validate the limits against a live provider (none has been available); consider a cap on the total size of tool results per execution (each result may be up to 12 KB, so a worst-case execution adds ≈96 KB to the model context).
- **Implementation status:** unchanged (constants in `agentplatform/execution.py`).

### D045 — Brand ember is preserved; it is not used for small text where it fails WCAG AA
- **Status:** Accepted (2026-11-18, owner decision at M4-B closure).
- **Context:** In the light theme the brand ember `#d9560a` is 3.55:1 on the page background and 3.97:1 behind white button labels; both are below the 4.5:1 AA target for normal-size text.
- **Decision:** The brand ember is **not** changed. It stays for fills, borders, accents and large shapes. Small text that was coloured ember uses a text-only token `--st-ember-text` (light `#b43e00`, 5.2:1; dark identical to ember). Three shared light-theme tokens (`--st-ink-3`, `--st-good`, `--st-warn`) were darkened earlier in M4-B for the same reason (see `flow.md` §25.22).
- **Accepted exception:** the white label on the ember primary-button fill is **3.97:1** in the light theme (buttons "Generate plan", "New agent", "Create from scratch", "Go to plans", "Create agent"). It is below 4.5:1 for normal-size text and is kept to preserve the brand identity. The dark theme was not measured in this pass.
- **Implementation status:** **Implemented**; verified by a real-browser light-theme probe (`flow.md` §25.23). Not deployed.
- **Related:** D044, `frontend/app/studio/studio.css`.

### M4-B formal acceptance (2026-11-18)
- **Decision:** M4-B is **formally ACCEPTED** by the owner with the non-blocking caveats listed in `flow.md` §25.23 §5 (live-provider function calling unverified; multi-page pagination unverified in a browser; the `uncertain` outcome verified by fixture; primary-button label contrast exception; three historical, unexplained, resource-correlated B5 failures; master-context files not yet synchronised).
- **Statuses after acceptance:** D040, D041, D042 (boundary), D043 (design **and** limits), D044 and D045 are Accepted. Still **Proposed — awaiting owner approval:** the tool lifecycle event vocabulary (D040) and the M4-C approval-binding design (D042). Nothing of M4-C was started.
- **Not committed, not pushed, not deployed.**

### D046 — A human approval authorises ONE exact tool invocation and is consumed atomically with it
- **Status:** Accepted (2026-11-18, owner directive for the M4-C sprint) for the mechanism. The default approval window (900 s) is **Proposed — awaiting owner approval**.
- **Context:** M4-B made side-effecting tools unavailable. A product that delegates real work needs a human gate that cannot be bypassed, replayed or widened.
- **Decision:** A durable `tool_approvals` row binds the decision to the workspace and user, execution, run and task, agent and exact agent version, exact tool id and version, and the validated canonical arguments, through a deterministic SHA-256 of that envelope. The server recomputes the hash at decision and again, from the live parameters, at consumption; a browser-supplied hash is only a staleness check. Lifecycle `pending → approved | rejected | expired | invalidated`, `approved → consumed | expired | invalidated`, enforced by a database trigger; `consumed` is written in the same transaction as the invocation's intent row. One approval per step, one invocation per approval.
- **Reasoning:** The binding must survive a hostile model and a hostile client; the database must hold the invariant even if application code regresses.
- **Consequences:** Approving never grants anything beyond the one call; the next call asks again. A changed argument, version, step, agent, run or workspace invalidates the approval.
- **Implementation status:** **Implemented (M4-C)**; migration `0012_approvals`; verified on SQLite, PostgreSQL and in a browser; not deployed.
- **Related:** D041, D042, D045, `agentplatform/tools/approvals.py`, `flow.md` §25.24.

### D047 — Policy: anything not explicitly allowed needs approval; a grant is not an approval
- **Status:** Accepted (2026-11-18, owner directive) for the boundary. **Proposed — awaiting owner approval:** an explicit server-side allowlist of tool ids that may run without approval, so that mislabelled metadata cannot make a sensitive tool run unattended.
- **Decision:** `ToolRegistry.gated(spec)` is true for any non-disabled tool the policy does not explicitly `allow` (unknown verdicts fail closed). Gated tools may be **granted** to an agent only when the registry opts in (`allow_gated`; the default registry does, and ships no gated tool); every call still requires its own approval. Agent grant and human approval are separate checks; plan approval never substitutes for either. Without an approval service a gated call is refused.
- **Consequences:** Read-only tools are unchanged and need no approval. Today the policy still relies on a tool's declared risk and side-effect class being honest (code review is the control).
- **Implementation status:** **Implemented (M4-C)**; allowlist **not implemented**.
- **Related:** D040, D042, D046.

### D048 — Waiting for approval is in-process and fails closed on recovery
- **Status:** Accepted (2026-11-18, owner directive: "recovery must not silently execute an action whose approval state is uncertain"). A durable suspend/resume is **Proposed — awaiting owner approval** for a later milestone.
- **Decision:** A task that reaches a gated call stays a running task and waits (bounded by the approval window, which is added to that agent's execution timeout). If the process dies, recovery fails the execution and invalidates its open approvals (including approved-but-unconsumed ones); a retry asks again. Cancelling the task invalidates its request.
- **Reasoning:** The smallest design that keeps the M4-A ownership model intact and can never execute an action whose approval state is uncertain.
- **Consequences:** A restart costs the person a second decision; it never costs correctness. The approval does **not** survive a restart as a resumable decision.
- **Implementation status:** **Implemented (M4-C)**.
- **Related:** D031, D032, D035, D046.

### D049 — The approval fixture is a simulation and opt-in; approval events are proposed, not built
- **Status:** Accepted (2026-11-18, owner directive: an explicitly labelled, deterministic, disposable fixture) for the simulation rule. The event vocabulary is **Proposed — awaiting owner approval**.
- **Decision:** `demo.send_note` is a **simulated** `external`/`non_idempotent` tool that sends nothing; it is never in the default registry and a process must opt in explicitly. The approval lifecycle is recorded in `tool_approvals` (status, timestamps, decider, linked invocation) and shown in the Studio; adding `approval_requested/approved/rejected/expired/consumed` to the `execution_events` vocabulary needs a migration (the vocabulary is a database CHECK) and is proposed, not done. Any workspace member may decide (recorded as `decided_by`); a role-based approver rule is proposed, not done.
- **Implementation status:** Simulation rule **implemented**; events and approver roles **not implemented**.
- **Related:** D038, D046, `agentplatform/tools/fixtures.py`.

### D050 — Approver permissions: only a workspace owner or admin may decide
- **Status:** **Proposed — awaiting owner confirmation.** The sprint directive asked for the simplest explicit rule consistent with the existing workspace model; the rule below is **implemented as the initial rule** on that instruction, but the choice between "any member" and "owner/admin only" is the owner's to confirm. Nothing was silently expanded: the stricter option was implemented.
- **Context:** The first M4-C version let any workspace member decide. Workspace roles are `owner`, `admin`, `member` (no viewer), and the existing model is deny-by-default for role-sensitive actions (only an owner changes memberships).
- **Recommendation:** owner/admin only, because an approval authorises an action that may act outside AgentForge, and a plain member should not be able to authorise another person's agent. A member can still **see** requests (visibility is not authority) and is told who can decide.
- **Implementation:** `ApprovalStore.decide` reads the caller's role from `workspace_memberships` and refuses a non-approver with `ApprovalForbidden` (HTTP 403 `approver_role_required`) **before** examining the approval's state; `can_decide` is reported by the API and explained in the Studio. `decided_by` is recorded. No separation-of-duties rule (an owner/admin may approve their own run's action).
- **Consequences / limits:** The API currently issues personal-workspace scopes only (the caller is always the owner there), so the multi-member rule is tested against real team workspaces at the service level; a multi-member HTTP path does not exist yet.
- **Implementation status:** **Implemented (M4-C finalization)**; not deployed.
- **Related:** D046, `agentplatform/tools/approvals.py`.

### D051 — Trusted tool policy: an allowlist of exact tool versions, not declared metadata
- **Status:** Accepted (2026-11-19, owner directive for the M4-C finalization sprint). The specific list is the five shipped tools (as accepted in M4-B).
- **Decision:** `ToolPolicy.auto_run` lists the exact `id@version` refs that may run without human approval. A tool runs unattended only if it is on the list and its declared class is still read-only with no side effect; everything else (unlisted tools and versions, unknown classifications, a tool that merely claims to be safe) requires approval for every call. The list is code, released like code; it cannot be changed by a model, an agent definition or a tool's metadata. The list's fingerprint is recorded in `approval_requested` events. Agent grants stay separate from approvals. Supersedes the "Proposed: allowlist" part of D047.
- **Consequences:** A new tool version needs a deliberate code change to become auto-run; until then it asks. A mislabelled sensitive tool cannot run unattended (tested).
- **Implementation status:** **Implemented (M4-C finalization)**.
- **Related:** D040, D042, D047, `agentplatform/tools/policy.py`.

### D052 — Durable suspension of a task waiting for approval
- **Status:** Accepted (2026-11-19, owner directive). Supersedes D048's in-process wait **for plan tasks**; D048's fail-closed principle stands.
- **Decision:** A plan task that reaches a gated call is suspended in the database (approval + redacted conversation + position in one transaction), releases its capacity, and any process resumes the exact call after the decision, re-validating and consuming the approval atomically with the invocation (D046). A restart or crash never invalidates a valid pending approval; it closes only what was really in flight (never redispatched). An approval whose execution is really closed (cancelled, failed) is invalidated. Standalone executions keep the bounded in-process wait.
- **Consequences:** Migration `0013_suspension` (table `tool_suspensions`); parked runs have no owner; resume latency is seconds; see `flow.md` §25.25 §10 for the limits (capacity of a resumed execution, dependants skipped when next driven).
- **Implementation status:** **Implemented (M4-C finalization)**; verified with real killed and stopped worker processes on SQLite and PostgreSQL and in a browser.
- **Related:** D031, D032, D046, D048.

### D053 — Approval lifecycle in the append-only run history
- **Status:** Accepted (2026-11-19, owner directive). Resolves the "Proposed: events" part of D049 and the event-vocabulary proposal in D040.
- **Decision:** `approval_requested`, `approval_approved`, `approval_rejected`, `approval_expired`, `approval_invalidated`, `approval_consumed`, `task_suspended` and `task_resumed` are part of the closed event vocabulary, written in the same transaction as the transition they describe and only when it really happened; details are whitelisted (tool ref, step, approval id, policy fingerprint, short reason) and never contain values.
- **Implementation status:** **Implemented (M4-C finalization)**.
- **Related:** D038, D049, `agentplatform/run_events.py`.

### Addendum of 2026-11-19 to D046–D049 (M4-C finalization)
No status was changed by this addendum beyond what D050–D053 record. **D046:** the 900-second approval window is **still Proposed — awaiting owner approval**. **D047:** the allowlist part is now implemented as D051; the boundary itself is unchanged. **D048:** superseded for plan tasks by D052 (the fail-closed principle remains). **D049:** the simulation rule is unchanged; approval events are now D053; approver roles are D050 (Proposed). **D040 and D042 keep their recorded status**: the implemented binding follows the D042 proposal, but D042 is not marked accepted by this document.

### D054 — Demo mode: an explicit, labelled, production-refusing way to run the whole journey without a model credential
- **Status:** Accepted (2026-11-19, product-sprint directive: "if a scripted provider or simulated integration is required, label it clearly as demo mode").
- **Context:** The product cannot be demonstrated, or tested end to end, without a model key and a safe sensitive action.
- **Decision:** `AGENTFORGE_DEMO_MODE=1` replaces only the model (a script named `demo-script`) and registers the simulated `demo.send_note`; config refuses it when `APP_ENV=production`; every Studio screen shows a banner; `GET /demo` reports it. The plan validation, scheduler, tools that compute, approvals, durable suspension and history stay real. The scripted model plans one workflow and declines anything else.
- **Consequences:** A demonstration must disclose both stand-ins (`docs/demo.md`). Demo mode proves the product's control plane and the real tools, not the quality of a live model. The legacy built-in agents still need a placeholder `GROQ_API_KEY` in the environment.
- **Implementation status:** **Implemented (M5)**; not deployed.
- **Related:** D049, D051, `agentplatform/demo.py`, `docs/demo.md`.

### D055 — The launch-readiness workforce is the reference use case, and it makes judgments from real tool results
- **Status:** Accepted (2026-11-19, product-sprint directive to build one compelling end-to-end use case from existing capabilities).
- **Decision:** Three template agents (unit-economics-analyst, launch-planner, launch-comms) use the shipped read-only tools; the communications agent declines to send (and asks for no approval) when the computed unit economics are not healthy. The starter workforce is a product feature in every mode (`POST /starter-workforce`, idempotent, never changes an existing agent).
- **Consequences:** The three templates join the template gallery for every user. The use case is a demonstration vehicle, not a claim about customers.
- **Implementation status:** **Implemented (M5)**.

### D056 — The Studio home states one next step from real data and invents nothing
- **Status:** Accepted (2026-11-19, product-sprint directive: only real backend data, one clear next action).
- **Decision:** The overview computes a single next step in priority order (pending approval, assemble, review, follow, see result, delegate) from the backend's own facts (agents, plans, pending approvals, the real status of the latest run), explains control in three plain lines, and shows no metric that the backend does not record (no cost, token or activity figure).
- **Consequences:** Cost and token views are deliberately absent until they are recorded. The overview makes one extra request (the runs of the newest approved plan).
- **Implementation status:** **Implemented (M5)**; browser-verified.
- **Related:** D044, D045, `frontend/lib/studio/home.ts`.


## M6 decisions (appended 2026-11-19, productization sprint)

**Reconciliation of D046 and D050 against the repository's own records (nothing silently changed).** D046 stays *Accepted (mechanism); 900 s window proposed*: the owner has accepted that an approval authorises one exact invocation and is consumed atomically with it, but no record in this repository shows the owner confirming the 900-second default, so it remains a proposal that the code uses. D050 stays *Proposed (implemented as the initial rule)*: no record shows the owner confirming "owner or admin only". M6 builds on both exactly as they are; they are still the owner's to confirm. D025's row says *Not implemented* although workspaces, memberships and authorization shipped in M1-B/M1-E; its status text is historical and is left as written (see D057 for what changed).

### D057 — `X-Workspace-Id` is honoured only after server-side membership verification
- **Status:** Accepted (2026-11-19, sprint directive: multi-member workspace approvals with server-side membership resolution). This is the "later step" D025 reserved the header for.
- **Decision:** Without the header the workspace is the caller's personal one (unchanged). With it, the value must be a UUID (400 otherwise) and the caller's membership is read from the database on every request (403 for a non-member; an unknown workspace and a workspace of strangers look identical). The role always comes from the database. A shared workspace is created by any signed-in user (who becomes its sole owner); only the owner adds an EXISTING user (looked up by email as a lookup aid, never an identity) as `admin` or `member`; no owner transfer, never into a personal workspace. Resources stay owned by (user, workspace); the approval inbox is shared across the workspace and shows approvers a bounded `context` (objective, task title, "you / another member"; never an email) so they can judge another member's request without being handed that member's run. CORS now allows the `X-Workspace-Id` request header (found by the real-browser run).
- **Consequences / limits:** A member sees only their own plans, runs and agents in a shared workspace; only approvals and notifications are shared. There is no invitation flow for people who have never signed in, no removal or role-change UI (the service functions exist), and no separation-of-duties rule (an owner/admin may approve their own run's action, as D050).
- **Implementation status:** **Implemented (M6)**; HTTP tests (owner, admin, member, stranger, two concurrent approvers) and real-browser journeys.
- **Related:** D025, D050, `authz/dependencies.py`, `authz/workspaces.py`, `agentplatform/workspaces_api.py`.

### D058 — In-app notifications are durable rows written in the approval-request transaction
- **Status:** Accepted.
- **Decision:** `notifications` (migration 0014) has UNIQUE (user, kind, ref): a retry or recovery cannot notify twice. The workspace's current owners and admins get one row in the SAME transaction that creates the approval (so a notification exists iff the request does). Only `read_at` may change, once; rows are never deleted (database triggers). The badge is the number of the caller's notifications whose approval is STILL pending (due approvals are expired first), so it falls after approve, reject, expiry or invalidation; `unread` is separate. The Studio polls the count every 15 s (paused while hidden, one request in flight) and refreshes immediately after a decision. No email, SMS or push.
- **Implementation status:** **Implemented (M6)**; SQLite and PostgreSQL schema tests, HTTP tests, browser-verified.
- **Related:** D046, D053, `agentplatform/notifications.py`.

### D059 — Model usage and cost: recorded per request, calculated never estimated
- **Status:** Accepted for the mechanism. The PRICE TABLE is configured by AgentForge (USD per million tokens for `groq/openai/gpt-oss-120b` and `-20b`, version `2026-11-19`) and is **not verified** against the provider's current price list; the provider's billing is the authority.
- **Decision:** `model_usage` (migration 0014, append-only) holds one row per provider request: workspace, user, plan, run, task, execution, agent, purpose (`plan` | `agent`), provider, model, input/output/cached tokens exactly as reported (NULL when not reported), `usage_known`, `pricing_version`, `cost_micros` and `cost_basis` (`calculated` | `unknown_usage` | `unknown_pricing`; a cost exists iff it was calculated). Micro-dollars, exact decimal arithmetic, half-up once per request; an old row is never recalculated; a new price is a new version. Aggregates report `cost_complete` only when every request was calculated and otherwise show "at least" with the reason. Recording is best effort and logs only the exception type. Built-in (legacy orchestrator) agents return text only, so they record nothing; that gap is stated, not hidden.
- **Limits:** Cached-token discounts are NOT applied (cost uses the standard input rate for all prompt tokens, so it can slightly overstate); reasoning tokens are included in the provider's completion count.
- **Implementation status:** **Implemented (M6)**; unit and API tests; a live run reported 11,366 input / 3,076 output tokens, 8 requests, $0.004011 (calculated).
- **Related:** `agentplatform/pricing.py`, `agentplatform/usage.py`.

### D060 — A resumed task acquires capacity atomically and holds a slot until it ends
- **Status:** Accepted.
- **Context:** A suspended task holds no slot (D052), but the original resume path only checked the in-process limits, so many resumes could exceed the database-wide limits, and a resumed execution's owner row names the process that SUSPENDED it (possibly dead), hiding it from the count.
- **Decision:** Consuming an approval of a suspended task runs the same atomic admission as a start (`ExecutionStore.admit_resumed`, advisory lock on PostgreSQL / write lock on SQLite) inside the consumption transaction, BEFORE the action is dispatched; `Busy` rolls everything back (the approval stays approved, the task stays suspended, tried again). A running task whose suspension was closed counts as occupied until it ends, whichever process resumed it. Standalone (REST/WebSocket) executions are unchanged: they wait in memory holding their slot and fail closed on a restart (no side effect is re-dispatched); making them durable was reviewed and NOT done (they have no persisted conversation to resume from, and no need was demonstrated).
- **Limits:** No exactly-once claim: an approved action is dispatched at most once by this system; a tool that takes effect and loses its process before recording is closed `uncertain` and never re-run. No new infrastructure (no Redis).
- **Implementation status:** **Implemented (M6)**; real SQLite two-process-style test (full database: the resume waits, then proceeds exactly once) with a mutation check that the test fails without the admission. Not run with multiple OS processes against PostgreSQL this sprint.
- **Related:** D035, D039, D052.

### D061 — The first real integration: `calendar.public_holidays`
- **Status:** Accepted.
- **Decision:** A read-only lookup of the official public holidays of one country and year from Nager.Date (https://date.nager.at, no key). It sends only a validated two-letter code and a year, builds the URL from validated parts at ONE fixed https host, follows no redirect, has a 4 s fetch timeout and a hard byte cap, validates the response with a strict schema, caps the result and ends every failure as the fixed content-free `tool_error`. It is on the trusted auto-run allowlist as the exact version `calendar.public_holidays@1` and, like every tool, runs only for an agent that was granted it (the Launch Planner template now is). Results say `live: true` and name the source. The data is the third party's and is not verified.
- **Implementation status:** **Implemented (M6)**; unit tests with a local stand-in; a real call returned 17 US holidays for 2026; used in a live journey.
- **Related:** D047, D051, `agentplatform/tools/holidays.py`.

### D062 — The primary button uses a deliberate accessible fill
- **Status:** Accepted (follow-up to D045).
- **Decision:** Dark mode keeps the brand ember (#ff7a1a) with near-black text; light mode uses a deeper ember (#b43e00, white text, 5.8:1) for the FILL, keeping the brand ember on borders and shapes. The inbox count and "New" tags use the same fill. Verified with axe (WCAG 2.x A/AA rules) at 390, 820 and 1280 px in both themes on the main screens. This is automated plus real-browser evidence, **not** a full WCAG audit or a screen-reader test; no conformance is claimed.
- **Implementation status:** **Implemented (M6)**.


## M6b decisions (appended 2026-11-19, live demo reliability sprint)

**D046 and D050 were re-read against this file and are unchanged:** D046 stays *Accepted (mechanism); 900 s window proposed* and D050 stays *Proposed (implemented as the initial rule)*; neither status was changed, and no record shows the owner confirming the 900 s default or "owner or admin only". M6b builds on both as they are.

### D063 — Provider retry, backoff, cooldown and concurrency (provider-independent)
- **Status:** Accepted (sprint directive). The numbers are defaults chosen from live observation, overridable by environment (`PROVIDER_MAX_ATTEMPTS`, `PROVIDER_BASE_DELAY`, `PROVIDER_MAX_WAIT`, `PROVIDER_BUDGET`, `PROVIDER_CONCURRENCY`).
- **Decision:** `ResilientProvider` wraps any provider (only the REAL provider is wrapped; demo and test fakes are not). Transient failures (`rate_limited` HTTP 429, `unavailable`, `timeout`, and `malformed_tool_call`, a provider 400 `tool_use_failed`) are retried up to 5 attempts; the wait is the provider's own `Retry-After` when given (jitter only adds), else exponential from 1 s; at most 90 s per wait and 240 s in total per request. A 429 sets a provider-wide cooldown so concurrent callers do not pile on, and a per-provider, per-event-loop semaphore bounds simultaneous requests (default 2; the live runs used 1). Permanent failures (`auth`, `bad_request`, `unsupported_model`, `invalid_response`) are never retried. The original error is raised when the budget is spent; nothing is ever replaced by a script. Only provider REQUESTS are retried: a tool invocation never is (an uncertain one stays closed `uncertain`, D041). Every wait, retry, recovery and final failure is recorded in the append-only `provider_events` (migration 0015: kind, attempt, fixed error code, wait; no prompt, answer or credential) with the run and task, and shown on the run page. The task execution deadline was raised from 90 s to 600 s because waits now happen inside it, and the planner's outer deadline gained 130 s of room for the same reason.
- **Honest limits:** retry state is per process (the cooldown and semaphore are not shared across processes); a task that fails after its side effect already ran (observed once live) fails with its true error code, and an explicit run retry would re-run that task (a new approval would be required for the send, but the earlier send is not remembered across attempts: see D064).
- **Implementation status:** **Implemented (M6b)**; 16 unit tests with injected clocks, 2 real plan-run tests, live-observed (92 waits, 57 recoveries).
- **Related:** D036, D041, `agentplatform/resilience.py`.

### D064 — A required action is part of the task contract
- **Status:** Accepted.
- **Decision:** A plan task may carry `required_tool` (`id@version`, migration 0015 `plan_tasks.required_tool`). The planner is shown each agent's granted tools and may set it; a person may set it in a revision (the Studio editor carries it through unchanged). The SERVER resolves it through the trusted registry (unknown -> `required_tool_unknown`), and it must be granted to the assigned agent (`required_tool_not_granted`); a model cannot name a tool into existence or grant itself one. At run time a task whose agent cannot call the tool does not start. During execution, if the model answers in prose without calling the tool, the server makes exactly ONE correction (the request forces that tool via the provider's tool choice, where supported) and then fails the task with `required_action_missing`. The task completes only when the invocation records show the tool `succeeded` in that execution; a refused, rejected or failed call never counts, prose never counts. A sensitive tool still goes through the existing exact-action approval, suspension and resume. The run page shows each required action as Performed / Waiting for approval / Not performed / Not yet invoked from those records.
- **Limits:** the model still chooses the arguments (validated against the tool's schema and shown to the approver); whether the planner sets `required_tool` is the model's judgment unless a person edits it; a retried task starts from scratch (it does not know an earlier attempt already performed the action).
- **Implementation status:** **Implemented (M6b)**; 7 tests including both required cases (prose does not satisfy, a structured invocation does) and a mutation check; live: 9 approvals consumed, 2 rejections never dispatched.
- **Related:** D046, D047, D051, D052, `agentplatform/execution.py`.

### D065 — A usage limit is not slept through
- **Status:** Accepted, from live observation.
- **Decision:** When the provider's `Retry-After` exceeds the maximum single wait, the request ends at once as `exhausted` (code `rate_limited`) with the asked wait recorded, instead of holding a task slot for minutes. The Studio says so plainly ("asked us to wait about N minutes (a usage limit)") and never calls it a spent retry budget. The run can be retried later; finished tasks are reused.
- **Observed:** the free-tier key asked for 114–985 s after repeated live runs, so later journeys could not run: this is a provider quota, not a defect the retry layer can fix.
- **Implementation status:** **Implemented (M6b)**.


## P0 safety sprint decisions (appended 2026-11-19, durable side-effect execution)

**D046 and D050 are unchanged.** Re-read against this file: D046 stays *Accepted (mechanism); 900 s window proposed*, D050 stays *Proposed (implemented as the initial rule)*. The reconciliation permission (D068) is a SEPARATE constant (`RECONCILER_ROLES`) that happens to name the same roles today; it does not reinterpret or confirm D050.

### D066 — A durable dispatch ledger for side-effecting tool calls
- **Status:** Accepted (sprint directive). **Why:** a task could fail after an external action had been dispatched and a retry re-ran it from scratch with a fresh idempotency key; the coarse `running -> final` invocation row could not say whether anything was SENT, and a timeout or an exception of a side-effecting tool was recorded as a confirmed failure.
- **Decision:** `tool_dispatches` (migration 0016), one row per invocation of a tool whose side effect is not `none`. States: `authorized` (intent durable, approval consumed in the SAME transaction, nothing sent) -> `dispatching` (committed BEFORE the handler runs) -> `succeeded` | `failed` (CONFIRMED; `failed` only when the tool raises `DefinitiveFailure` or rejects its arguments before acting) | `unknown`; `authorized` -> `cancelled` (provably never sent); `unknown` -> `reconciled_succeeded` | `reconciled_failed` (only through a reconciliation record). Database triggers (SQLite and PostgreSQL) allow exactly these transitions, fix the identity columns and forbid deletes. A timeout, a cancellation, an unexpected exception, a crash, a lost lease or a failed local write after `dispatching` is `unknown`, never `failed`. Every transition appends a `tool_dispatch_*` run event in its own transaction. The invocation row and the dispatch row finish in ONE transaction; a stale owner's write is rejected by the lease fence and by compare-and-swap; recovery closes `authorized` as `cancelled` and `dispatching` as `unknown`. The existing `tool_invocations` rows and triggers are unchanged.
- **Limits:** only tools declared with a side effect take this path; a standalone (REST/WebSocket) execution has no cross-attempt identity (its operation key is per execution and step), so the cross-attempt rules below protect plan tasks.
- **Implementation status:** **Implemented (P0)**; SQLite and PostgreSQL tests; 13 mutations killed.
- **Related:** D041, D046, D052, `agentplatform/tools/dispatch.py`.

### D067 — Operation identity and the duplicate-prevention gate
- **Status:** Accepted.
- **Decision:** `operation_key` = sha256(workspace, plan, task, tool ref, canonical validated arguments) for a plan task: the SAME logical operation across attempts and executions (it deliberately excludes the execution id and the step number). `idempotency_key` (what a remote service receives) = `af-<operation key prefix>-g<generation>`. Before a human is asked, and again authoritatively inside the transaction that authorises the dispatch, a gate looks at every dispatch of that tool in the task lineage: any in flight -> refuse `operation_in_flight`; any `unknown` -> refuse `outcome_unknown` whatever the arguments; the same operation already confirmed (`succeeded`/`reconciled_succeeded`) -> REPLAY the recorded result (a `succeeded` invocation, event `tool_dispatch_replayed`, no dispatch, no approval); the same operation `failed` -> refuse `retry_not_permitted` unless the tool's `retry_policy` is `after_confirmed_failure`; `reconciled_failed` or `cancelled` -> a new generation (a new key, issued only after that evidence) may be authorised and a gated tool still needs a fresh exact-action approval. (operation_key, generation) is UNIQUE; on PostgreSQL a transaction advisory lock serialises authorisations of one lineage (defence in depth: within a run the lease-row lock already serialises them, and the mutation that removes the advisory lock is NOT killed by any test, which is stated rather than hidden).
- **Documented limit:** a retry whose model words the same announcement DIFFERENTLY asks for a DIFFERENT operation (different canonical arguments). It is not replayed; in a gated deployment it needs a fresh exact-action approval whose card shows the new text, and a person can see it. This is pinned by a test, not an accident. AgentForge cannot know that two different messages are "the same logical send".
- **Implementation status:** **Implemented (P0)**.

### D068 — Reconciliation of unknown outcomes
- **Status:** Accepted.
- **Decision:** `POST /tool-invocations/{id}/reconcile` (workspace owner or admin only, checked before the state; foreign workspaces see 404; unknown request fields refused). `mode: query` asks the remote service through the tool's own `lookup(idempotency_key)`: `succeeded` -> `reconciled_succeeded`; `failed` -> `reconciled_failed`; `not_found` -> `reconciled_failed` ONLY for a tool whose remote honours idempotency keys; anything else, or an unreachable remote, leaves it `unknown` and records the attempt. `mode: attest` records a person's decision with mandatory evidence and reason, stored as `operator_attestation` and shown as "a person's statement, not proof from the remote service". Every attempt (including `unresolved`) is an append-only `tool_reconciliations` row with actor, evidence, reason and time; the state change is a compare-and-swap in the same transaction (of concurrent attempts one wins, the others write nothing). A plan-run retry is refused with `reconciliation_required` while any task it would re-run has an unknown outcome; the legacy acknowledgement cannot override it. Reconciling never dispatches anything.
- **Implementation status:** **Implemented (P0)**; HTTP tests, concurrency tests on SQLite and PostgreSQL.

### D069 — Execution truth: the server decides whether a task succeeded
- **Status:** Directed by the sprint-2 brief; not separately confirmed by the owner. Builds on D064 (required action) and D066-D068 (dispatch ledger); changes none of them.
- **Task intent (derived, no new schema):** no `required_tool` = a reasoning/research/writing task, which never needs a tool; a `required_tool` whose spec has `side_effect = none` = tool-required; a `required_tool` with a side effect = side-effect-required (needs a recorded confirmed success; approval, exact arguments and version bindings are unchanged).
- **Contract, enforced in the tool loop before a task can complete (not in the prompt or the UI):** for EVERY task, required or not, if any side-effecting dispatch of the execution is `unknown`, still in flight, or confirmedly `failed` without a later success of the same tool in that execution, the task ends `failed` with `side_effect_outcome_unknown` (HTTP 409, reconcile first) or `side_effect_failed`. Prose cannot override this. A required action additionally needs a recorded success (`required_action_missing` otherwise). Rejected, refused or never-invoked calls never count; a pending approval keeps the task suspended (existing behaviour). A retry of an unknown outcome is refused by the existing reconciliation gate; a stale or recovered worker cannot complete the task (fenced).
- **Consequence for users:** a task whose external action could not be proven shows as failed with the reason, instead of "succeeded" next to a warning.
- **Limit:** a person-reconciled `reconciled_succeeded` / `reconciled_failed` is a recorded statement (D068); the task result is not retroactively re-evaluated, a retry of the task replays or re-dispatches per D067.
- **Evidence:** `tests/test_execution_truth.py` (10), the tightened `test_durable_dispatch` assertions, mutation check (guard disabled: 6 tests fail).
- **Sprint 3 amendment (status unchanged: Directed, owner confirmation pending):** found in a real-browser journey, the guard now counts the whole task LINEAGE (same plan and task, every attempt), not just the current execution: a retry whose send is refused by the tool policy (`retry_not_permitted`, no new dispatch) used to complete over an earlier confirmed failure. Test: `test_a_retry_whose_send_is_refused_by_policy_cannot_complete_the_task_over_an_earlier_confirmed_failure` (fails without the change). Known remaining limit: a task with no `required_tool` whose model never tried the action at all is a reasoning task by definition and is not failed.

### D070 — Per-user rate limits shared across processes
- **Status:** Directed by the sprint-2 brief; not separately confirmed by the owner.
- **Decision:** fixed-window counters in Redis (already a required dependency) keyed by (bucket, user), never by connection; `429` with a fixed body and `Retry-After`; no identity in the response or logs. Per minute per user: plan create 15, revise 40, approve 40, run start 15, run retry 15, approval decision 40, reconcile 20, agent run 30, improve 6; WebSocket: 30 connections and 30 job messages per user (shared by all that user's sockets, in every process). Health, readiness and metrics are not limited. Redis down: a bounded per-process counter takes over (a 15 s breaker avoids latency); the limiter never becomes "unlimited".
- **Limits:** a fixed window allows a 2x burst across a boundary; unauthenticated floods and token issuance (a Next.js route) are NOT limited here and belong to the edge proxy; limits are constants, not yet per-deployment configuration.
- **Sprint 3 amendment (status unchanged):** the limits are now validated configuration (`RATE_LIMIT_<NAME>`, 1-6000, out of range refuses to start; see docs/operations.md section 11); the Next.js token route has a per-person in-memory ceiling (`TOKEN_ISSUE_PER_MINUTE`, default 30; per Next.js process, no cross-stack dependency); unauthenticated floods remain an edge-proxy requirement, documented. A Redis outage is counted (`agentforge_redis_unavailable_total`).

### D071 — Shared provider coordination
- **Status:** Directed by the sprint-2 brief; not separately confirmed by the owner.
- **Decision:** `agentplatform/coordination.py`: a Redis cooldown key (extend-only) and a lease-scored sorted set of global slots, both atomic Lua using the Redis server clock. `ResilientProvider` waits out the shared cooldown, takes a global slot (cap `PROVIDER_GLOBAL_CONCURRENCY`, default 8) polling at most `PROVIDER_ADMISSION_WAIT` (30 s) and then FAILS as `rate_limited` (never queued forever, never retried into a storm), releases on every path, and publishes a 429's cooldown for all processes. A crashed holder frees its slot when its lease (180 s) expires. Redis down: per-process limits only (availability over sharing). Only model requests are coordinated; tool calls and side effects are never retried by this layer.
- **Evidence:** fake-coordinator tests (4) and 3 real OS processes against a disposable Redis (3).
- **Sprint 3 amendment (status unchanged):** `PROVIDER_REDIS_DOWN=degrade|closed` chooses what admission does while Redis is unreachable (default `degrade`, counted and alerted; `closed` grants no slot and fails the request after the admission wait). Behaviour drilled against a real Redis (stop/start, lease expiry, state loss); failover topologies untested. Provider tuning values are now range-validated (an invalid value refuses to start instead of being silently ignored). Tested under load with 2 API processes: the highest concurrency measured inside the provider stub across processes equalled the cap (4, and 2 in a tighter run).
- **Sprint 4.1 amendment (status unchanged: Directed, owner confirmation pending) — the Redis outage policy changed, deliberately.** Sprint 4 measured that the previous DEFAULT (`degrade`) granted 307/307 slots during an outage and let provider concurrency reach 6 against a cap of 4: the cap was silently dropped. Now: `PROVIDER_REDIS_DOWN` defaults to `closed` (admission is refused whenever the shared accounting cannot be trusted); `degrade` is an explicit opt-in that is disclosed (start-up warning, `provider_admission_total{decision="degraded"}`, gauge `provider_global_cap_enforced` = 0, alert `AgentForgeProviderCapNotEnforced`). Accounting is also untrusted after a Redis server change: the admission script compares the server `run_id` with `provider:origin` and quarantines admission cluster-wide for one lease term (`PROVIDER_LEASE_SECONDS`, new, default 90 s, was a fixed 180 s); with Sentinel a grant must be acknowledged by a replica (`WAIT`) unless none is connected. Run leases and fencing are in PostgreSQL and are unaffected. The cap invariant is held at the price of availability during an outage (measured: peak concurrency 4, 6 requests failed `rate_limited`, p95 47 s). Residual risk: loss of the origin key together with the slots is indistinguishable from a first start. Evidence and limits: docs/operations.md section 22. The previous sentence about the load measurement ("equalled the cap") remains true for the healthy case.

### D072 — Observability baseline
- **Status:** Directed by the sprint-2 brief; not separately confirmed by the owner.
- **Decision:** `observability.py`: counters/histograms with a fixed label vocabulary (never user, workspace or run ids), rendered at `GET /metrics` only when `METRICS_TOKEN` is set (bearer-protected; 404 otherwise); gauges for running runs, unresolved dispatches, pending approvals and registered workers are read from the database at scrape time. Logs: `LOG_FORMAT=json`, correlation ids (workspace, run, task, execution) in a context variable, secrets redacted from messages, exception type only. `GET /ready` (database reachable, no detail) is separate from `GET /health` (liveness).
- **Limits:** metrics are per process (scrape every process); no distributed tracing: correlation is by ids in logs; invocation and provider-request ids are not yet bound into every log line; no alert rules are shipped.

### D073 — The first real side-effecting integration: `email.send@1` (Resend)
- **Status:** Directed by the sprint-4 brief; not separately confirmed by the owner. Builds on D066-D068 (dispatch ledger, gate, reconciliation) and D069 (execution truth); changes none of them.
- **Choice and why:** Resend because it DOCUMENTS an `Idempotency-Key` header (1-256 characters, 24 hours, same key + same payload = the original response, different payload = 409), has documented sandbox recipients (`delivered@resend.dev` ...), a small typed API (`from`, `to`, `subject`, `text`; response `{"id"}`) and `GET /emails/{id}`. What it does NOT document (and is therefore not relied on): lookup by idempotency key, the full `last_event` list on the page read, the missing-id response.
- **Decision:** the tool exists only when `RESEND_API_KEY` is configured (secret held in one module, only in the Authorization header to one fixed https host); sender fixed by `EMAIL_FROM`; recipients limited to `EMAIL_ALLOWED_DOMAINS` (default `resend.dev`: SANDBOX by default, real recipients need an explicit operator setting); every call needs exact-action approval; `retry_policy=never`; AgentForge sends the operation's stable key; 400/401/403/404/422/429 = definitive failure, 409/5xx/timeouts/unreadable 2xx = UNKNOWN; no `lookup` is registered, so an unknown outcome is reconciled by a person's attestation (never proof). Replaying the key to "ask" is deliberately not reconciliation (it would dispatch).
- **Guarantee, exactly:** at most one automatic dispatch per logical operation (AgentForge) and, per the provider's documentation (not independently verified live), no second send for the same key and payload within 24 hours. NOT exactly-once delivery.
- **Evidence:** 19 contract tests against a local mock of the documented semantics; runtime tests through the durable-dispatch protocol; load run through 2 API processes and 2 workers with 0 duplicate real sends to the mock; a worker hard-kill and 20% lost responses created no duplicates. **Real execution unverified** (no sandbox key was available; none used). Credential checklist: docs/operations.md section 16.

### D074 — Live alerting without a new service, and application-level edge protections
- **Status:** Directed by the sprint-4 brief; not separately confirmed by the owner.
- **Alerting decision:** the repository has no monitoring backend and adding Prometheus/Alertmanager would be a new service to operate. `alerting.py` evaluates 11 rules inside each API process (severity, `for` period, one firing and one resolved notification, 4 h reminder, Redis de-duplication across processes, bounded delivery with failure accounting) and posts to ONE webhook (`ALERT_WEBHOOK_URL`, Slack-compatible). It complements, and does not replace, the fleet-wide rule file `ops/alerts/agentforge-alerts.yml` for teams that run Prometheus. Backup/restore signals come from `ops/backup_job.py` through `BACKUP_STATUS_FILE`.
- **Edge decision:** per-IP flood protection for unauthenticated traffic belongs to the reverse proxy (`ops/nginx/agentforge.conf`, exercised in a real nginx by `ops/edge_drill.sh`); the application adds `MAX_REQUEST_BYTES` (413) and `ALLOWED_HOSTS` (400) as defence in depth and keeps per-user limits (D070) and the token-issuance ceiling.
- **Limits, stated:** metric-based signals are per process; evaluator timers are in memory (a restart re-enters `pending`); no silencing, escalation or on-call schedule; delivery to a production receiver and a production edge are UNVERIFIED.
