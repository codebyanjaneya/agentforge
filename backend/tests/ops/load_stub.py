"""Shared by the load-test API and worker launchers (sprint 4): the STUB model provider, the optional email tool wired to a local mock, and the common start-up checks. Nothing here contacts a real
provider; the model is a stub with a controlled latency and 429 rate, the email tool talks to ``tests/ops/mock_resend.py`` on loopback."""

import asyncio
import json
import os
import random
import time
import sys
from urllib.parse import urlparse

BACKEND = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
STATS = {"calls": 0, "active": 0, "max_active": 0, "r429": 0}


def prepare_environment() -> None:
    """Validate that only disposable loopback servers are used and put the application's settings in the environment BEFORE ``main`` is imported."""
    os.chdir(BACKEND)
    sys.path.insert(0, BACKEND)
    db, redis_url = os.environ["LOAD_DATABASE_URL"], os.environ["LOAD_REDIS_URL"]
    for url, default_port in ((db, 5432), (redis_url, 6379)):
        parsed = urlparse(url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "::1") or (parsed.port or default_port) == default_port:
            raise SystemExit("refusing to run: the load test only talks to a disposable loopback database/redis on a non-default port")
    public = open(os.environ["LOAD_PUBLIC_PEM_FILE"], encoding="utf-8").read()
    os.environ.update({"APP_ENV": "development", "GROQ_API_KEY": "stub-not-a-credential", "DATABASE_URL": db, "REDIS_URL": redis_url, "PYTHON_DOTENV_DISABLED": "1",
                       "AUTH_JWT_PUBLIC_KEYS": json.dumps({"k1": public}), "CORS_ALLOWED_ORIGINS": "http://localhost:3999"})


def install(main) -> None:
    """Replace the model provider with the stub (wrapped in the REAL resilience + Redis coordination layers) and register the email tool against the local mock when asked."""
    import ratelimit
    from agentplatform.coordination import Coordinator
    from agentplatform.providers import ProviderError, ProviderRegistry, ProviderResponse, ToolCall
    from agentplatform.resilience import RetryPolicy, wrap_registry
    from agentplatform.tools import default_registry, email_resend

    latency, rate_429 = float(os.environ.get("LOAD_STUB_LATENCY", "0.08")), float(os.environ.get("LOAD_STUB_429_RATE", "0.05"))
    side_effects = os.environ.get("LOAD_SIDE_EFFECTS") == "1"
    agent = os.environ.get("LOAD_AGENT", "load-analyst")
    client = ratelimit.redis_client()
    enter = client.register_script("local a = redis.call('INCR', KEYS[1]); local m = tonumber(redis.call('GET', KEYS[2]) or '0'); if a > m then redis.call('SET', KEYS[2], a) end; return a")  # TRUE cross-process concurrency

    if side_effects:
        email_resend.register(default_registry(), email_resend.EmailConfig(api_key="load-test-key-not-real", sender="AgentForge <sandbox@resend.dev>", allowed_domains=("resend.dev",),
                                                                           url=os.environ["LOAD_MOCK_RESEND_URL"], allow_insecure_test_url=True))

    log_path = os.environ.get("LOAD_STUB_CALLLOG")
    token_counter = iter(range(10**9))

    def calllog(kind, call_id=0):
        """An INDEPENDENT record of provider concurrency: one line per call start/end with a wall-clock time, appended by every process (API and worker) to ONE local file; the load test computes the true maximum
        overlap from it, so the measurement does not depend on Redis (which may be down on purpose)."""
        if log_path:
            with open(f"{log_path}.{os.getpid()}", "a") as f:  # ONE FILE PER PROCESS: concurrent appends from several processes to one file lose lines on Windows
                f.write(f"{kind} {time.perf_counter():.7f} {os.getpid()} {call_id}" + chr(10))  # high-resolution counter, one system-wide origin on Windows (time.time() has ~15 ms granularity there)

    def _safe(fn):
        try:
            result = fn()
            return 0 if result is None else result
        except Exception:  # noqa: BLE001
            return None

    class Stub:
        name = "groq"

        async def complete(self, request):
            STATS["calls"] += 1
            STATS["active"] += 1
            STATS["max_active"] = max(STATS["max_active"], STATS["active"])
            entered = await asyncio.to_thread(_safe, lambda: enter(keys=["load:active", "load:maxactive"]))  # measurement only: Redis being down must not fail the stub
            try:
                call_id = next(token_counter)
                calllog("S", call_id)  # the provider call starts HERE: only the simulated provider latency is bracketed (the stub's own Redis measurement calls must not stretch the interval)
                try:
                    await asyncio.sleep(latency * (0.5 + random.random()))
                finally:
                    calllog("E", call_id)  # unconditional: a CANCELLED call (deadline, shutdown) must still close its interval, or the measured concurrency drifts upward
                if random.random() < rate_429:
                    STATS["r429"] += 1
                    raise ProviderError("rate_limited", retry_after=0.3)
                if not request.system.startswith("You are an AI agent running inside AgentForge"):  # the planner
                    base = {"description": "Work out the monthly figure carefully.", "expected_output": "A number.", "agent": agent, "rationale": "The analyst computes it.", "depends_on": [],
                            "complexity": "low", "priority": "normal"}
                    tasks = [{**base, "id": "calc", "title": "Compute the figure"}]
                    if side_effects:
                        tasks.append({**base, "id": "notify", "title": "Email the figure", "description": "Email the figure to delivered@resend.dev with the email tool.", "depends_on": ["calc"],
                                      "dependency_reason": "It reports the computed figure.", "required_tool": "email.send@1"})
                    else:
                        tasks.append({**base, "id": "check", "title": "Check the figure", "depends_on": ["calc"], "dependency_reason": "It checks the first result."})
                    return ProviderResponse(text=json.dumps({"feasible": True, "interpretation": "Compute and report one figure.", "tasks": tasks, "unsupported": []}), model=request.model)
                if request.tools and not any(m.role == "tool" for m in request.messages):
                    if os.environ.get("LOAD_STUB_DEBUG"):
                        open(os.environ["LOAD_STUB_DEBUG"], "a").write(repr([(m.role, (m.content or "")[:160]) for m in request.messages]) + chr(10))
                    last_user = [m.content or "" for m in request.messages if m.role == "user"][-1]  # the agent's short-term memory carries earlier tasks' prompts: only the LATEST one is this task
                    wanted = "email" if side_effects and "Your task: Email the figure" in last_user else "math"
                    name = next((t.name for t in request.tools if wanted in t.name), request.tools[0].name)
                    args = {"to": "delivered@resend.dev", "subject": "Monthly figure", "text": "The monthly figure is 60."} if wanted == "email" else {"expression": "(1200 * 0.85 - 300) / 12"}
                    return ProviderResponse(text="I will do it.", model=request.model, tool_calls=(ToolCall("c1", name, json.dumps(args)),))
                return ProviderResponse(text="Done: the monthly figure is 60.", model=request.model)
            finally:
                STATS["active"] -= 1
                if entered is not None:
                    await asyncio.to_thread(_safe, lambda: client.decr("load:active"))

    policy = RetryPolicy.from_env(main.settings.provider_tuning)
    main.providers = wrap_registry(ProviderRegistry({"groq": lambda: Stub()}), policy, Coordinator(client, fail_closed=main.settings.provider_redis_down == "closed", replica_ack_ms=100 if main.settings.redis_sentinels else 0))
