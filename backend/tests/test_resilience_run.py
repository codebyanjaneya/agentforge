"""M6b: the retry layer inside a REAL plan run: events are persisted with the run and task, a burst of 429s recovers without rerunning anything, an exhausted budget fails only that task
honestly (rate_limited, never a script), and the explicit run retry then reuses the finished task and completes the failed one."""

import asyncio

from agentplatform.execution import ExecutionService, Limits
from agentplatform.plan_runs import PlanRunService, RunRegistry
from agentplatform.providers import ProviderError, ProviderRegistry, ProviderResponse, Usage
from agentplatform.usage import UsageRecorder
from agentplatform.resilience import ResilientProvider, RetryPolicy
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.api_support import FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)


class Burst:
    """Answers every request, except that requests for ``victim`` fail with a 429 while ``failures`` lasts (None = forever)."""

    name = "groq"

    def __init__(self, victim, failures):
        self.victim, self.failures, self.calls = victim, failures, {}

    async def complete(self, request):
        who = "bravo" if "Task bravo" in request.messages[-1].content else "alpha"
        self.calls[who] = self.calls.get(who, 0) + 1
        if who == self.victim and (self.failures is None or self.failures > 0):
            if self.failures is not None:
                self.failures -= 1
            raise ProviderError("rate_limited", retry_after=0.01)
        return ProviderResponse(text=f"{who} done", model="openai/gpt-oss-120b", usage=Usage(100, 20))


def stack(world, burst):
    fast = RetryPolicy(max_attempts=4, base_delay=0.01, max_wait=0.05, budget=1.0, concurrency=2)
    registry = ProviderRegistry({"groq": lambda: ResilientProvider(burst, fast)})
    executions = ExecutionService(world["sf"], orchestrator=world["orch"], providers=registry, short_term=ShortTermMemory(client=FakeRedis()),
                                  long_term=MemoryRepository(world["sf"]), limits=Limits(total=8, per_user=3), usage=UsageRecorder(world["sf"]))
    return PlanRunService(world["sf"], executions=executions, registry=RunRegistry(), busy_poll=0.01)


def plan(world):
    for slug in ("alpha", "bravo"):
        world["agents"].create(world["sa"], slug, definition())
    return make_plan(world, [task("alpha", "alpha"), task("bravo", "bravo")])


def events(world):
    return world["db"].rows("SELECT task_key, kind, attempt, error_code FROM provider_events ORDER BY created_at, id")


def test_a_burst_of_429s_recovers_inside_the_task_and_is_recorded_with_the_run_and_task(world):
    burst = Burst("bravo", failures=2)
    svc = stack(world, burst)
    plan_id = plan(world)

    async def go():
        started = await svc.start(world["sa"], plan_id)
        await svc.registry.wait(started.id, 30)
        return await asyncio.to_thread(svc.store.get, world["sa"], started.id)

    view = asyncio.run(go())
    assert view.status == "succeeded" and burst.calls == {"alpha": 1, "bravo": 3}  # nothing ran twice except the failed provider requests themselves
    assert events(world) == [("bravo", "waiting", 1, "rate_limited"), ("bravo", "retrying", 2, "rate_limited"), ("bravo", "waiting", 2, "rate_limited"),
                             ("bravo", "retrying", 3, "rate_limited"), ("bravo", "recovered", 3, None)]
    assert world["db"].scalar("SELECT count(*) FROM provider_events WHERE run_id IS NOT NULL AND execution_id IS NOT NULL") == 5
    assert world["db"].scalar("SELECT count(*) FROM model_usage WHERE run_id IS NOT NULL") == 2  # only requests that returned are accounted for (usage is reported by the provider)


def test_an_exhausted_budget_fails_only_that_task_honestly_and_the_run_retry_reuses_the_finished_one(world):
    burst = Burst("bravo", failures=None)
    svc = stack(world, burst)
    plan_id = plan(world)

    async def first():
        started = await svc.start(world["sa"], plan_id)
        await svc.registry.wait(started.id, 30)
        return started.id, await asyncio.to_thread(svc.store.get, world["sa"], started.id)

    run_id, view = asyncio.run(first())
    assert view.status == "partial"
    assert world["db"].rows("SELECT task_key, status, error_code FROM plan_task_runs ORDER BY task_key") == [("alpha", "succeeded", None), ("bravo", "failed", "rate_limited")]
    kinds = [e[1] for e in events(world)]
    assert kinds.count("waiting") == 3 and kinds[-1] == "exhausted" and burst.calls["bravo"] == 4  # exactly the attempt budget, no more: no storm
    assert world["db"].scalar("SELECT count(*) FROM agent_executions WHERE status = 'succeeded'") == 1
    # the provider recovers; the explicit retry reuses alpha (never dispatched again) and completes bravo
    burst.failures = 0

    async def second():
        retried = await svc.retry(world["sa"], run_id)
        await svc.registry.wait(retried.id, 30)
        return await asyncio.to_thread(svc.store.get, world["sa"], retried.id)

    again = asyncio.run(second())
    assert again.status == "succeeded" and burst.calls["alpha"] == 1  # alpha's result was reused
    assert world["db"].scalar("SELECT count(*) FROM agent_executions WHERE agent_key = 'alpha'") == 1
    assert ("bravo", "exhausted", 4, "rate_limited") in events(world)  # the earlier record is kept as history
