# AgentForge 2.0

A multi-user **AI workforce platform**: you describe an objective, a planner turns it into a reviewable plan of tasks for specialist agents, you approve it, and then you start the run as a separate, explicit step. Agents use registered tools; actions with real-world effects need an approval of the exact action, are dispatched at most once automatically, and an unknown outcome is reconciled by a person. Everything is recorded in an append-only history with usage and cost.

> **Status: release candidate for staging verification. Not production-ready.** What has and has not been verified is listed under [Verification status](#verification-status). Nothing here claims customer traction or a verified live deployment.

## What it does

- **Workspaces and teams.** Every person has a personal workspace; shared workspaces have one owner plus admins and members. Members can read the workspace's plans, runs, usage and approvals; only the person who created a plan or started a run can change it; owners/admins decide approvals; membership and invitations are owner-only. Invitations work for people who have never signed in (hashed, single-use, expiring token bound to the invited verified email; the link is copied by the inviter: **no invitation email is sent**).
- **Agents with immutable versions.** Custom agents are versioned; a plan pins the exact version it was written against. A starter workforce and templates are included. The seven legacy agents (CEO, Sales, Marketing, Support, Research, Finance, Hiring) still power the original dashboard and Telegram bot.
- **Objective -> plan -> approve -> run.** The planner validates its own output (agents, dependencies, tools). Plans are editable and versioned; approving never starts anything.
- **Durable execution.** Runs are owned by a worker through database leases with fencing; a crashed worker's runs are recovered; side-effecting tool calls go through a dispatch ledger (`authorized -> dispatching -> succeeded | failed | unknown -> reconciled`). The server, not the model, decides whether a task succeeded.
- **Tools.** A versioned registry with schemas, grants per agent version, timeouts and delivery guarantees shown in the Studio. Built-ins compute (unit economics, date arithmetic, text analysis) or look up public holidays. `email.send@1` (Resend) is the first real side-effecting integration: sandbox recipients by default, exact-action approval, idempotency key, no exactly-once claim. A simulated `demo.send_note` exists for demos and is labelled as simulated.
- **Resilience and operations.** Provider retry/cooldown with a Redis-shared concurrency cap (`PROVIDER_REDIS_DOWN=closed` by default), per-user rate limits, Prometheus metrics, JSON logs, alert rules, request-size and Host protections, backup job and drills. See [docs/operations.md](docs/operations.md).
- **Studio UI** (`/studio`): onboarding, agents and versions, plan review/edit, approvals inbox, run timeline, tool-invocation details, usage and cost, team and invitations, light/dark, responsive. Write actions are hidden for people who may not perform them (the backend remains authoritative).

## Architecture

```
backend/                 FastAPI modular monolith (Python 3.11)
  main.py, app_factory.py  API, WebSocket, readiness, metrics, middleware
  agentplatform/           plans, runs, tools, approvals, dispatch ledger, usage, coordination
  auth/, authz/            ES256 backend tokens, principals, workspace roles, invitations
  memory/                  SQLAlchemy models/repositories, legacy Redis short-term memory
  migrations/              Alembic (head: 0017_invitations)
  worker.py                durable run worker (python worker.py)
  tests/                   pytest suite (+ tests/ops load and failure drills)
frontend/                Next.js 16 + NextAuth v4 (Studio in app/studio)
ops/                     nginx example, alert rules, backup/PITR/Sentinel/edge drills, staging env template
docs/                    operations, demo, decisions summary, per-sprint context
flow.md, decision.md     engineering log and decision register
```

Data: PostgreSQL 16 (SQLite only with `APP_ENV=test`), Redis 7 (rate limits, provider coordination, legacy chat memory; Sentinel supported, Cluster is not).

## Run it locally

Prerequisites: Python 3.11, Node 20+, Docker (for PostgreSQL/Redis), a Groq-compatible API key for a live model (the default model is `openai/gpt-oss-120b`).

```bash
# infrastructure (disposable)
docker run -d --name agentforge-pg -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=agentforge -p 127.0.0.1:5433:5432 postgres:16
docker run -d --name agentforge-redis -p 127.0.0.1:6379:6379 redis:7-alpine

# backend
cd backend
python -m venv venv && source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp ../ops/staging.env.example .env                        # then edit: placeholders only in the template
python ../ops/check_config.py --profile staging           # optional: validates the environment shape
python -m alembic upgrade head
uvicorn main:app --port 8000
python worker.py                                          # separate process when RUN_EXECUTION=api-only

# frontend
cd ../frontend && npm install && npm run dev
```

Frontend environment (`frontend/.env.local`, never committed): `NEXTAUTH_URL`, `NEXTAUTH_SECRET`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `NEXT_PUBLIC_API_BASE`, `BACKEND_TOKEN_PRIVATE_KEY` (P-256 PEM), `BACKEND_TOKEN_KID`. The backend verifies tokens with `AUTH_JWT_PUBLIC_KEYS` (kid -> public key). Required backend settings and every limit are validated at start-up (`backend/config.py`); `backend/.env` is git-ignored.

**Demo without a model key:** set `APP_ENV=test`, `AGENTFORGE_DEMO_MODE=1` (refused in production) and follow [docs/demo.md](docs/demo.md); the model is a script and the UI says so.

## Tests

```bash
cd backend
# one process per file (the suite's module-purging fixtures need isolated files)
for f in tests/test_*.py; do python -m pytest "$f" -q -p no:cacheprovider; done
# PostgreSQL integration (disposable server only; see the guard in the test file)
AGENTFORGE_TEST_PG_ADMIN_URL=... AGENTFORGE_TEST_PG_TOKEN=... python -m pytest tests/test_postgres_integration.py -q
# real-Redis coordination tests
AGENTFORGE_TEST_REDIS_URL=redis://127.0.0.1:6379/0 python -m pytest tests/test_provider_coordination.py -q
cd ../frontend && npm test && npx tsc --noEmit && npx eslint .
```

CI: `.github/workflows/ci.yml` (backend per-file, PostgreSQL + Redis + load/crash smoke, release gates, frontend).

## Verification status

Verified locally on the commit that carries this README: backend per-file regression, the PostgreSQL integration file on a disposable PostgreSQL 16, real-Redis coordination tests and the Redis failure drill, the frontend suite, TypeScript and ESLint, and an isolated real-browser journey (scripted model, simulated tool) with automated accessibility checks. Exact numbers are in [flow.md](flow.md) section 25.36 and [docs/operations.md](docs/operations.md) section 23.

**Not verified:** any successful live model run (the one authorised run was refused by the provider: the model configured in the deployment's environment is no longer served; `ops/live_model_check.py` is the bounded harness for a retry), a real Resend send, any staging or production deployment, a production alert receiver and on-call, a real edge/CDN, screen-reader behaviour, Redis Cluster, multi-host load, and measured RTO/RPO. Known limits: legacy short-term chat memory is not Sentinel-aware; losing Redis's origin key together with all slots is undetectable; no owner transfer; invitation lifetime is a constant (7 days).

See [docs/release-assessment.md](docs/release-assessment.md) for the verdict (staging-ready, not production-ready) with verified-locally, verified-in-staging and verified-externally kept apart, [docs/recovery-and-staging.md](docs/recovery-and-staging.md) for the measured local recovery drills and the staging runbook, and [docs/accessibility.md](docs/accessibility.md) for the accessibility record and the manual screen-reader script.

## Legacy Telegram bot and classic dashboard

`backend/` still serves the original single-user surfaces (agent chat over `/ws`, autonomous runs, scheduled tasks, self-improvement, the Telegram bot with voice) and the classic dashboard at `/` in the frontend; they share the same authentication.

## License

MIT
