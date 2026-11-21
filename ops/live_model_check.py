"""ONE bounded live-model validation run (owner-authorised, billable). Run from backend/:   python ../ops/live_model_check.py [--yes-spend-one-bounded-run]

What it does (and only this):
  * reads the provider key and model name from backend/.env INSIDE this process (``dotenv_values``); the key is never printed, logged, copied or written anywhere,
    and backend/.env is never modified;
  * builds a throw-away SQLite database in a temp directory (it does NOT touch the database named in backend/.env), one user, one custom agent that is granted ONLY
    the read-only tool ``math.calculate``, and ONE one-task plan whose planner output is scripted (no model call for planning);
  * runs that plan ONCE through the real durable run path (PlanRunService -> ExecutionService -> tool loop -> tool ledger -> usage recorder) with the REAL provider;
  * enforces hard limits BEFORE the first request: at most ``MAX_REQUESTS`` provider requests in total (a wrapper refuses the next one), ``max_tokens`` per request,
    a per-request timeout, one attempt per request (the SDK retries are off and no retry layer is installed), no parallel calls (the scheduler runs one task), and an
    estimated-cost ceiling that stops further requests;
  * no email, no other tool, no network call other than the provider's, no Redis, no staging or production.
It prints one JSON evidence document and exits 0 only if the run completed and the evidence is consistent.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from decimal import Decimal
from pathlib import Path

BACKEND = Path.cwd()
assert (BACKEND / "alembic.ini").exists(), "run from backend/"
sys.path.insert(0, str(BACKEND))

MAX_REQUESTS = 3  # the expected shape is 2 (tool call, then the final answer); a third is the margin, never more
MAX_TOKENS = 400  # per request, completion side
REQUEST_TIMEOUT = 30.0
RUN_TIMEOUT = 90.0
COST_CEILING_USD = Decimal("0.01")
ASSUMED_PRICE = (Decimal("0.59"), Decimal("0.79"))  # USD per 1M tokens (input, output): an ESTIMATE for the cost ceiling only, not verified against the provider's price list
OBJECTIVE = "Use the math.calculate tool to compute 17*23, then reply with the number and one short sentence."
EXPECTED = "391"


def main() -> int:
    dry = "--dry-run" in sys.argv  # a scripted stand-in provider: proves the harness, limits and evidence WITHOUT any provider call or key
    if not dry and "--yes-spend-one-bounded-run" not in sys.argv:
        print("refusing: this makes a real, billable provider call; pass --yes-spend-one-bounded-run (the owner authorised exactly one run), or --dry-run for the free harness check")
        return 2
    from dotenv import dotenv_values

    values = {} if dry else dotenv_values(BACKEND / ".env")
    key, model = ("dry-run", "dry-run-model") if dry else (values.get("GROQ_API_KEY") or "", values.get("MODEL_NAME") or "")
    if not key or not model:
        print(json.dumps({"blocker": "GROQ_API_KEY or MODEL_NAME is not set in backend/.env"}))
        return 3
    tmp = Path(tempfile.mkdtemp(prefix="af_live_"))
    db_url = f"sqlite:///{(tmp / 'live.db').as_posix()}"
    # Pin the environment BEFORE the application modules are imported: some of them call load_dotenv(override=False), which must not pull backend/.env's database,
    # Redis or key into this process (an already-set variable is never overridden).
    os.environ.update({"APP_ENV": "test", "DATABASE_URL": db_url, "GROQ_API_KEY": "unused-placeholder", "REDIS_URL": "redis://127.0.0.1:1/0"})
    env = {**os.environ, "APP_ENV": "test", "DATABASE_URL": db_url, "GROQ_API_KEY": "unused-in-subprocess", "PYTHON_DOTENV_DISABLED": "1"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env, check=True, capture_output=True)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from agentplatform.execution import ExecutionService, Limits
    from agentplatform.plan_runs import PlanRunService, RunRegistry
    from agentplatform.plans import PlanningService
    from agentplatform.pricing import cost_micros
    from agentplatform.providers import GroqProvider, ProviderError, ProviderRegistry, ProviderResponse, Usage
    from agentplatform.schema import AgentDefinition
    from agentplatform.service import AgentService
    from agentplatform.tools import default_registry
    from agentplatform.tools.invoker import ToolInvoker, ToolStore
    from agentplatform.usage import UsageRecorder
    from auth.principal import Principal
    from authz.workspaces import authorize_personal_scope
    from memory.models import User, Workspace, WorkspaceMembership
    from memory.repository import MemoryRepository
    from memory.short_term import ShortTermMemory

    engine = create_engine(db_url)
    sf = sessionmaker(bind=engine, expire_on_commit=False)
    uid, wid = uuid.uuid4(), uuid.uuid4()
    with sf() as s, s.begin():
        s.add(User(id=uid, email="live-check@example.invalid", email_normalized="live-check@example.invalid", is_active=True))
        s.flush()
        s.add(Workspace(id=wid, name="Personal", is_personal=True, personal_for_user_id=uid))
        s.flush()
        s.add(WorkspaceMembership(workspace_id=wid, user_id=uid, role="owner"))
    scope = authorize_personal_scope(sf, Principal(user_id=uid, provider="google", provider_subject="live-check", email="live-check@example.invalid", is_active=True))

    evidence = {"model_configured_in_env": model, "limits": {"max_provider_requests": MAX_REQUESTS, "max_tokens_per_request": MAX_TOKENS, "request_timeout_s": REQUEST_TIMEOUT,
                                                              "run_timeout_s": RUN_TIMEOUT, "cost_ceiling_usd": str(COST_CEILING_USD), "attempts_per_request": 1, "parallel_calls": 1},
                "requests": []}

    class FakeRedis:  # the legacy short-term memory is not part of this check: an in-memory stand-in, no Redis is contacted
        def __init__(self):
            self.d = {}

        def get(self, k):
            return self.d.get(k)

        def set(self, k, v):
            self.d[k] = v

        def delete(self, k):
            self.d.pop(k, None)

    class Guard:
        """The real provider behind hard limits: counts, times and records every request; refuses requests beyond the cap or once the estimated cost passes the ceiling."""

        name = "groq"

        def __init__(self, real):
            self.real, self.spent = real, Decimal(0)

        async def complete(self, request):
            if len(evidence["requests"]) >= MAX_REQUESTS:
                raise ProviderError("unavailable")
            if self.spent >= COST_CEILING_USD:
                raise ProviderError("unavailable")
            started = time.perf_counter()
            entry = {"n": len(evidence["requests"]) + 1, "model_requested": request.model, "tools_offered": [t.name for t in request.tools], "max_tokens": request.max_tokens}
            evidence["requests"].append(entry)
            try:
                response: ProviderResponse = await self.real.complete(request)
            except ProviderError as exc:
                entry.update(outcome="provider_error", error_code=exc.code, latency_s=round(time.perf_counter() - started, 3))
                raise
            usage: Usage = response.usage
            entry.update(outcome="ok", latency_s=round(time.perf_counter() - started, 3), model_reported=response.model, finish_reason=response.finish_reason,
                         tool_calls=[c.name for c in response.tool_calls], prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens, text_chars=len(response.text or ""))
            if isinstance(usage.prompt_tokens, int) and isinstance(usage.completion_tokens, int):
                self.spent += (Decimal(usage.prompt_tokens) * ASSUMED_PRICE[0] + Decimal(usage.completion_tokens) * ASSUMED_PRICE[1]) / Decimal(1_000_000)
            return response

    class Planner:
        name = "groq"

        async def complete(self, request):  # scripted: planning is NOT a model call in this run
            plan = {"feasible": True, "interpretation": "Compute one product with the calculator tool.", "unsupported": [], "tasks": [
                {"id": "calc", "title": "Compute 17*23 with the calculator", "description": OBJECTIVE, "expected_output": "The number 391 and one sentence.",
                 "agent": "calc-check", "rationale": "The calculator agent is granted the tool.", "depends_on": [], "complexity": "low", "priority": "normal"}]}
            return ProviderResponse(text=json.dumps(plan), model="scripted-planner", usage=Usage(prompt_tokens=0, completion_tokens=0))

    class Orchestrator:
        agent_names = ()

    class DryProvider:
        """--dry-run only: first request asks for the calculator, the second answers from the tool result (no network, no key)."""

        name = "groq"

        async def complete(self, request):
            from agentplatform.providers import ToolCall

            if not any(m.role == "tool" for m in request.messages):
                return ProviderResponse(text="", model=request.model, finish_reason="tool_calls", tool_calls=(ToolCall("c1", "math__calculate__v1", json.dumps({"expression": "17*23"})),), usage=Usage(prompt_tokens=300, completion_tokens=20))
            return ProviderResponse(text="17*23 = 391.", model=request.model, finish_reason="stop", usage=Usage(prompt_tokens=360, completion_tokens=12))

    guard = Guard(DryProvider() if dry else GroqProvider(key))
    del key  # the only copy is inside the provider's client from here on; it is never printed
    limits = Limits(total=1, per_user=1)
    registry = default_registry()
    agents = AgentService(sf)
    agents.create(scope, "calc-check", AgentDefinition.model_validate({
        "name": "Calculator checker", "description": "Computes one arithmetic result with the calculator tool.", "role": "Calculator", "purpose": "Compute exactly what is asked with the tool.",
        "instructions": "You must call the math.calculate tool for any arithmetic and then state its result. Be brief.", "model": {"provider": "groq", "name": model, "temperature": 0, "max_tokens": MAX_TOKENS},
        "allowed_tools": ["math.calculate"]}))
    executions = ExecutionService(sf, orchestrator=Orchestrator(), providers=ProviderRegistry({"groq": lambda: guard}), short_term=ShortTermMemory(client=FakeRedis()), long_term=MemoryRepository(sf),
                                  limits=limits, tools=ToolInvoker(ToolStore(sf), registry, None), usage=UsageRecorder(sf), provider_timeout=REQUEST_TIMEOUT, custom_timeout=RUN_TIMEOUT)
    planning = PlanningService(sf, providers=ProviderRegistry({"groq": lambda: Planner()}), limits=limits)
    plan = asyncio.run(planning.create_plan(scope, OBJECTIVE))
    planning.approve(scope, plan.id, 1)
    service = PlanRunService(sf, executions=executions, registry=RunRegistry(), busy_poll=0.01, busy_budget=0.6)

    async def go():
        started = await service.start(scope, plan.id)
        await service.registry.wait(started.id, RUN_TIMEOUT + 30)
        return started.id

    t0 = time.perf_counter()
    run_id = asyncio.run(go())
    wall = round(time.perf_counter() - t0, 3)

    with engine.connect() as c:
        run = c.execute(text("SELECT status, error_code FROM plan_runs WHERE id = :i"), {"i": run_id.hex}).first()
        task = c.execute(text("SELECT status, error_code, execution_id FROM plan_task_runs WHERE run_id = :i"), {"i": run_id.hex}).first()
        invocations = [tuple(r) for r in c.execute(text("SELECT tool_id, status, side_effect, output FROM tool_invocations ORDER BY seq")).fetchall()]
        dispatches = c.execute(text("SELECT count(*) FROM tool_dispatches")).scalar()
        usage_rows = [tuple(r) for r in c.execute(text("SELECT purpose, model, input_tokens, output_tokens, cost_micros, cost_basis FROM model_usage ORDER BY created_at")).fetchall()]
        exec_row = c.execute(text("SELECT id, status, provider, model, result FROM agent_executions")).first()
    prompt_t = sum(r.get("prompt_tokens") or 0 for r in evidence["requests"])
    completion_t = sum(r.get("completion_tokens") or 0 for r in evidence["requests"])
    est = (Decimal(prompt_t) * ASSUMED_PRICE[0] + Decimal(completion_t) * ASSUMED_PRICE[1]) / Decimal(1_000_000)
    registry_cost, basis = cost_micros("groq", model, prompt_t, completion_t)
    result = (exec_row[4] or "") if exec_row else ""
    ok_tool = any(i[0] == "math.calculate" and i[1] == "succeeded" and EXPECTED in (i[3] or "") for i in invocations)
    evidence.update({
        "plan_run_id": str(run_id), "execution_id": str(uuid.UUID(exec_row[0])) if exec_row else None, "run_status": run[0] if run else None, "run_error": run[1] if run else None,
        "task_status": task[0] if task else None, "task_error_code": task[1] if task else None, "task_result": result[:400],
        "wall_clock_s": wall, "provider_requests_made": len(evidence["requests"]), "prompt_tokens": prompt_t, "completion_tokens": completion_t,
        "estimated_cost_usd_assumed_price": str(est.quantize(Decimal("0.000001"))), "assumed_price_per_1m_tokens_usd": [str(p) for p in ASSUMED_PRICE],
        "platform_cost_accounting": {"cost_micros": registry_cost, "basis": basis}, "model_usage_rows": usage_rows,
        "tool_invocations": [{"tool": i[0], "status": i[1], "side_effect": i[2], "output": (i[3] or "")[:120]} for i in invocations], "side_effecting_dispatches": dispatches,
        "checks": {"run_succeeded": bool(run and run[0] == "succeeded"), "task_succeeded": bool(task and task[0] == "succeeded"), "calculator_ran_and_returned_391": ok_tool,
                   "model_text_contains_391": EXPECTED in result, "no_side_effecting_dispatch": dispatches == 0, "requests_within_cap": len(evidence["requests"]) <= MAX_REQUESTS,
                   "cost_within_ceiling": est <= COST_CEILING_USD}})
    print(json.dumps(evidence, indent=1, default=str))
    engine.dispose()
    return 0 if all(evidence["checks"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
