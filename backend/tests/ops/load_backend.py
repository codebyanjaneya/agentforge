"""Load-test API process (sprint 3, extended in sprint 4). Runs the REAL FastAPI application (real JWT verification, real authorisation, real plan/run/tool code, real provider-resilience and Redis coordination
layers) against a DISPOSABLE PostgreSQL and Redis, with ONE substitution: the model provider is a STUB with a controlled latency and a controlled rate of 429s (tests/ops/load_stub.py). With
``LOAD_SIDE_EFFECTS=1`` the REAL email tool is registered against the LOCAL mock (tests/ops/mock_resend.py). No credential, no external network, no real side effect.

    python tests/ops/load_backend.py <port>      environment: LOAD_DATABASE_URL, LOAD_REDIS_URL, LOAD_PUBLIC_PEM_FILE (+ LOAD_*, RATE_LIMIT_*, PROVIDER_*, RUN_EXECUTION, METRICS_TOKEN ...)
Refuses to start unless the database and Redis are on loopback and the port is not a default one (5432 / 6379)."""

import sys

import load_stub

load_stub.prepare_environment()

import uvicorn  # noqa: E402

import main  # noqa: E402

load_stub.install(main)


@main.app.get("/_stub_stats", include_in_schema=False)
async def stub_stats():  # load-test only: how hard THIS process's provider stub was driven
    return load_stub.STATS


uvicorn.run(main.app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
