"""M4-A2 Part 2: explicit, auditable retry attempts that preserve successful work.

A retry is a NEW attempt of the same approved plan revision: tasks that already succeeded are carried over (never dispatched again; their
persisted results feed their dependants), every other task runs again pinned to the same agent versions, the attempt links to the one it
retries, uncertain outcomes need an explicit per-task acknowledgement, and at most one retry of a run can exist."""

import asyncio
import threading
import uuid

import pytest

from agentplatform.plan_runs import (
    AlreadyExecuted,
    PlanCancelledForRun,
    PlanRunStore,
    RetryNotAllowed,
    RunInProgress,
    RunNotFound,
    RunRegistry,
    UncertainOutcome,
)
from agentplatform.providers import ProviderError
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, definition, go, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, expire

DIAMOND = [task("a", "research"), task("b", "sales", ["a"]), task("c", "marketing", ["a"]), task("d", "ceo", ["b", "c"])]


def failing(world, **errors):
    """Built-in agents named in ``errors`` raise that exception; the others answer ``result of <name>``."""

    async def behaviour(name, text, scope):
        if name in errors:
            raise errors[name]
        return f"result of {name}"

    world["orch"].behaviour = behaviour


def retry(world, run_id, acknowledge=(), svc=None, scope=None, timeout=20):
    svc, scope = svc or world["svc"], scope or world["sa"]

    async def scenario():
        started = await svc.retry(scope, run_id, tuple(acknowledge))
        await svc.registry.wait(started.id, timeout)
        return await asyncio.to_thread(svc.store.get, scope, started.id)

    return asyncio.run(scenario())


def calls(world):
    return [name for name, _ in world["orch"].calls]


# ------------------------------------------------------------------------------------------------ the core guarantees

def test_a_retry_never_executes_a_succeeded_task_again_and_dependants_reuse_its_persisted_output(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))  # the provider rejected the request: nothing took effect
    first = go(world, plan_id)
    assert (first.status, statuses(first)) == ("partial", {"a": "succeeded", "b": "failed", "c": "skipped"})
    assert calls(world) == ["research", "sales"]
    failing(world)  # the provider recovered
    second = retry(world, first.id)  # a safe failure needs no acknowledgement
    assert (second.attempt, second.status, statuses(second)) == (2, "succeeded", {"a": "succeeded", "b": "succeeded", "c": "succeeded"})
    assert calls(world) == ["research", "sales", "sales", "ceo"]  # research ran exactly once in total
    by = {t.key: t for t in second.tasks}
    assert by["a"].reused and by["a"].reused_attempt == 1 and by["a"].reused_run_id == first.id and not by["b"].reused
    assert by["a"].execution_id == {t.key: t for t in first.tasks}["a"].execution_id  # traceable to the execution that really ran it
    assert "result of research" in world["orch"].calls[2][1]  # b (retried) received a's PERSISTED output from attempt 1
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 4  # a, b(failed), b(retry), c: no execution for the reused task
    assert second.counts["succeeded"] == 3 and second.counts["total"] == 3
    assert second.retry_of == {"id": first.id, "attempt": 1}
    assert world["svc"].store.get(world["sa"], first.id).retried_by == {"id": second.id, "attempt": 2}


def test_the_outputs_of_every_reused_prerequisite_survive_into_a_new_attempt(world):
    plan_id = make_plan(world, DIAMOND)
    failing(world, sales=ProviderError("unavailable"))
    first = go(world, plan_id)
    assert statuses(first) == {"a": "succeeded", "b": "failed", "c": "succeeded", "d": "skipped"}
    failing(world)
    second = retry(world, first.id)
    assert second.status == "succeeded" and sorted(calls(world)) == sorted(["research", "sales", "marketing", "sales", "ceo"])
    d_input = [text for name, text in world["orch"].calls if name == "ceo"][0]
    assert "result of sales" in d_input and "result of marketing" in d_input  # b is new, c was carried over from attempt 1
    assert {t.key for t in second.tasks if t.reused} == {"a", "c"}
    detail = asyncio.run(world["svc"].detail(world["sa"], second.id))
    assert [o["task"] for o in detail[1]] == ["d"] and detail[1][0]["result"] == "result of ceo"  # final outputs of the leaf, from its execution


def test_an_uncertain_outcome_is_never_retried_silently(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=RuntimeError("the connection dropped after the request left"))  # outcome unknown
    first = go(world, plan_id)
    assert {t.key: t.retry_class for t in first.tasks} == {"a": None, "b": "uncertain", "c": "safe"}
    assert first.retry == {"allowed": True, "reason": None, "uncertain": ["b"], "reconcile": []}
    failing(world)
    runs_before = world["db"].scalar("SELECT count(*) FROM plan_runs")
    with pytest.raises(UncertainOutcome) as info:
        retry(world, first.id)
    assert info.value.tasks == ["b"]
    with pytest.raises(UncertainOutcome):
        retry(world, first.id, acknowledge=["c"])  # acknowledging something else does not cover b
    assert world["db"].scalar("SELECT count(*) FROM plan_runs") == runs_before and calls(world) == ["research", "sales"]  # nothing was created or run
    second = retry(world, first.id, acknowledge=["b"])
    assert second.status == "succeeded" and calls(world) == ["research", "sales", "sales", "ceo"]
    row = world["db"].rows("SELECT acknowledged, carried_count FROM plan_run_retries")
    assert row == [('["b"]', 1)]  # the decision is recorded with the attempt


@pytest.mark.parametrize("code", ["timeout", "invalid_response"])
def test_timeouts_and_unusable_responses_are_uncertain_but_provider_rejections_are_safe(world, code):
    plan_id = make_plan(world, [task("a", "research")])
    failing(world, research=ProviderError(code))
    first = go(world, plan_id)
    assert first.tasks[0].retry_class == "uncertain"
    with pytest.raises(UncertainOutcome):
        retry(world, first.id)


def test_the_classification_table():
    from agentplatform.plan_runs import retry_class

    for status in ("succeeded", "pending", "running"):
        assert retry_class(status, None, None) is None
    for status, code in (("skipped", "dependency_failed"), ("failed", "busy"), ("failed", "agent_unavailable"), ("failed", "task_invalid"), ("cancelled", "cancelled")):
        assert retry_class(status, code, None) == "safe"  # never dispatched: nothing reached a provider
    eid = uuid.uuid4()
    for code in ("rate_limited", "unavailable", "auth", "unsupported_model", "unsupported_provider", "bad_request"):
        assert retry_class("failed", code, eid) == "safe"  # the provider rejected the request
    for status, code in (("failed", "timeout"), ("failed", "interrupted"), ("failed", "internal_error"), ("failed", "invalid_response"), ("cancelled", "cancelled")):
        assert retry_class(status, code, eid) == "uncertain"  # a dispatched call whose outcome is unknown


def test_an_interrupted_task_after_a_crash_needs_an_acknowledgement_and_the_rest_is_reused(world):
    from agentplatform.execution import ExecutionStore
    from agentplatform.service import ResolvedAgent
    from agentplatform.run_leases import Fence

    plan_id = make_plan(world, CHAIN)
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    fence = Fence(run_id, store.owner_id, 1)
    _, tasks = store.load_for_driver(run_id)
    by = {t.key: t for t in tasks}
    resolved = ResolvedAgent("builtin", "research", None, None, None)
    done = store.start_task(by["a"].id, world["sa"], resolved, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    store.finish_task_execution(by["a"].id, done, "succeeded", "succeeded", None, fence=fence, result="the persisted result of a")
    store.start_task(by["b"].id, world["sa"], resolved, "Sales", "t", "groq", None, executions=world["executions"].store, fence=fence)  # in flight when the process died
    expire(world)
    assert store.recover_expired() == [run_id]
    crashed = store.get(world["sa"], run_id)
    assert (crashed.status, statuses(crashed), crashed.tasks[1].retry_class) == ("partial", {"a": "succeeded", "b": "failed", "c": "skipped"}, "uncertain")
    with pytest.raises(UncertainOutcome):
        retry(world, run_id)
    second = retry(world, run_id, acknowledge=["b"])
    assert second.status == "succeeded" and calls(world) == ["sales", "ceo"]  # research never ran again
    assert "the persisted result of a" in world["orch"].calls[0][1]  # b received a's output read back from the database


def test_cancelled_runs_can_be_retried_with_the_in_flight_task_acknowledged(world):
    plan_id = make_plan(world, CHAIN)

    async def scenario():
        started = asyncio.Event()

        async def behaviour(name, text, scope):
            if name == "sales":
                started.set()
                await asyncio.sleep(3600)
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await world["svc"].start(world["sa"], plan_id)
        await asyncio.wait_for(started.wait(), 10)
        return await world["svc"].cancel(world["sa"], run.id)

    cancelled = asyncio.run(scenario())
    assert (cancelled.status, statuses(cancelled)) == ("cancelled", {"a": "succeeded", "b": "cancelled", "c": "cancelled"})
    assert {t.key: t.retry_class for t in cancelled.tasks} == {"a": None, "b": "uncertain", "c": "safe"}  # b was in flight; c never started
    failing(world)
    with pytest.raises(UncertainOutcome) as info:
        retry(world, cancelled.id)
    assert info.value.tasks == ["b"]
    assert retry(world, cancelled.id, acknowledge=["b"]).status == "succeeded"


# ------------------------------------------------------------------------------------------------ duplicates and refusals

def test_concurrent_retries_create_exactly_one_attempt(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    failing(world)
    reg_a, reg_b = RunRegistry(config=FAST), RunRegistry(config=FAST)
    svc_a, svc_b = world["service"](registry=reg_a), world["service"](registry=reg_b)

    async def scenario():
        results = await asyncio.gather(svc_a.retry(world["sa"], first.id), svc_b.retry(world["sa"], first.id), return_exceptions=True)
        ok = [r for r in results if not isinstance(r, BaseException)]
        refused = [r for r in results if isinstance(r, BaseException)]
        await (reg_a if reg_a.active() else reg_b).wait(ok[0].id, 20)
        return ok, refused

    ok, refused = asyncio.run(scenario())
    assert len(ok) == 1 and len(refused) == 1 and isinstance(refused[0], (RetryNotAllowed, RunInProgress, AlreadyExecuted))
    assert world["db"].scalar("SELECT count(*) FROM plan_runs") == 2 and world["db"].scalar("SELECT count(*) FROM plan_run_retries") == 1
    assert calls(world).count("research") == 1 and calls(world).count("ceo") == 1  # each remaining task dispatched exactly once


def test_many_threads_retrying_one_run_leave_one_attempt_and_one_lineage_row(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    stores = [PlanRunStore(world["sf"], config=FAST) for _ in range(6)]
    barrier, results = threading.Barrier(6), []

    def attempt(store):
        barrier.wait()
        try:
            results.append(store.create(world["sa"], plan_id, retry_of=first.id, claim=False))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=attempt, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join() for t in threads]
    winners = [r for r in results if isinstance(r, uuid.UUID)]
    assert len(winners) == 1 and all(isinstance(r, (RetryNotAllowed, RunInProgress, AlreadyExecuted)) for r in results if r not in winners)
    assert world["db"].scalar("SELECT count(*) FROM plan_run_retries") == 1 and world["db"].scalar("SELECT count(*) FROM plan_runs") == 2


def test_only_a_latest_unsucceeded_attempt_of_an_approved_plan_can_be_retried(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    second = retry(world, first.id)  # still failing: attempt 2 also ends partial
    assert second.status == "partial"
    with pytest.raises(RetryNotAllowed) as info:
        retry(world, first.id)  # attempt 1 is no longer the latest
    assert info.value.code == "not_latest_attempt"
    failing(world)
    done = retry(world, second.id)
    assert done.status == "succeeded"
    with pytest.raises(RetryNotAllowed) as info:
        retry(world, done.id)
    assert info.value.code == "run_not_retryable"  # a succeeded run needs no retry
    # a chain of attempts reuses the work of the FIRST execution of each task, never a copy
    original = world["db"].rows("SELECT id FROM plan_task_runs WHERE task_key = 'a' AND status = 'succeeded'")
    assert [r[0] for r in world["db"].rows("SELECT source_task_run_id FROM plan_task_carryovers WHERE task_key = 'a' ORDER BY created_at")] == [original[0][0]] * 2
    assert world["svc"].store.get(world["sa"], done.id).retry_of == {"id": second.id, "attempt": 2}


def test_a_running_attempt_blocks_a_second_one_and_a_cancelled_plan_cannot_be_retried(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)

    async def scenario():
        gate = asyncio.Event()

        async def behaviour(name, text, scope):
            await gate.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        running = await world["svc"].retry(world["sa"], first.id)
        with pytest.raises((RunInProgress, RetryNotAllowed)):
            await world["svc"].retry(world["sa"], first.id)  # no duplicate concurrent attempt
        with pytest.raises(RunInProgress):
            await world["svc"].start(world["sa"], plan_id)
        gate.set()
        await world["svc"].registry.wait(running.id, 20)

    asyncio.run(scenario())
    # a cancelled plan cannot be retried
    plan2 = make_plan(world, CHAIN, objective="Launch our second product to early customers within a month.")
    failing(world, sales=ProviderError("rate_limited"))
    other = go(world, plan2)
    world["svc"].plans.cancel(world["sa"], plan2)
    with pytest.raises(PlanCancelledForRun):
        retry(world, other.id)


def test_retry_is_scoped_foreign_and_unknown_runs_are_not_found_and_leave_nothing(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    failing(world)
    before = world["db"].scalar("SELECT count(*) FROM plan_runs")
    for scope, rid in ((world["sb"], first.id), (world["sa"], uuid.uuid4())):
        with pytest.raises(RunNotFound):
            retry(world, rid, scope=scope)
    with pytest.raises(RetryNotAllowed) as info:
        retry(world, first.id, acknowledge=["no-such-task"])
    assert info.value.code == "unknown_task"
    assert world["db"].scalar("SELECT count(*) FROM plan_runs") == before == 1 and world["db"].scalar("SELECT count(*) FROM plan_run_retries") == 0


def test_a_retry_keeps_the_pinned_agent_versions_of_the_approved_plan(world):
    created = world["agents"].create(world["sa"], "writer", definition())
    plan_id = make_plan(world, [task("a", "writer"), task("b", "sales", ["a"])])
    world["agents"].update(world["sa"], created.id, 1, definition(role="Second role"))  # edited AFTER approval: version 2 now exists
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    assert statuses(first) == {"a": "succeeded", "b": "failed"}
    failing(world)
    second = retry(world, first.id)
    assert second.status == "succeeded" and {t.key: t.agent_version for t in second.tasks if t.agent_source == "custom"} == {"a": 1}
    assert world["db"].rows("SELECT agent_version FROM agent_executions WHERE agent_source = 'custom'") == [(1,)]  # the custom task ran once, at its pinned version
