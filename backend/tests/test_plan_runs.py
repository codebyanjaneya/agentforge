"""M3-B: execution of approved plans (service layer: migrated disposable SQLite, the real M2-B ExecutionService, fake provider and
fake built-in orchestrator, no network)."""

import asyncio
import copy
import json
import threading

import pytest

import agentplatform.plan_runs as plan_runs_module
from agentplatform.execution import ExecutionService, ExecutionStore, Limits
from agentplatform.plan_runs import (
    MAX_ACTIVE_RUNS_PER_USER,
    AlreadyExecuted,
    PlanCancelledForRun,
    PlanNotApproved,
    PlanRunService,
    PlanRunStore,
    RunInProgress,
    RunInvalid,
    RunLimit,
    RunNotFound,
    RunRegistry,
    TaskRunNotFound,
)
from agentplatform.planning import ProposedTask
from agentplatform.plans import PlanNotFound, PlanningService
from agentplatform.providers import ProviderError, ProviderRegistry
from agentplatform.schema import AgentDefinition
from agentplatform.service import AgentService, ResolvedAgent
from authz.errors import ScopeError
from authz.workspaces import authorize_memory_scope, authorize_personal_scope
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.api_support import FakeProvider, FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture)
from tests.test_authz_memory import make_user, team_workspace

BASE = {
    "name": "Release Notes Writer", "description": "Turns a changelog into friendly release notes for customers.", "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.", "instructions": "You write clear release notes from the changelog the user provides.",
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"},
}
OBJECTIVE = "Launch our new product to early customers within a month."


def definition(**overrides):
    data = copy.deepcopy(BASE)
    data.update(overrides)
    return AgentDefinition.model_validate(data)


def task(id, agent, depends_on=(), **extra):
    return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short written result.",
            "agent": agent, "rationale": "This agent fits the work.", "depends_on": list(depends_on), **extra}


class Orchestrator:
    """Stands in for the unchanged built-in orchestrator; ``behaviour`` can delay, fail or record."""

    agent_names = ("ceo", "sales", "research", "marketing", "finance", "hiring", "support")

    def __init__(self):
        self.calls, self.behaviour, self.active, self.max_active = [], None, 0, 0

    async def run_agent(self, name, task, scope):
        self.calls.append((name, task))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.behaviour:
                return await self.behaviour(name, task, scope)
            return f"result of {name}"
        finally:
            self.active -= 1

    run_with_delegation = run_agent


@pytest.fixture
def world(auth_db):  # noqa: F811
    a, _ = make_user(auth_db, "a@example.com")
    b, _ = make_user(auth_db, "b@example.com")
    sf = auth_db.session_factory
    provider, orch, redis, limits = FakeProvider(), Orchestrator(), FakeRedis(), Limits(total=8, per_user=3)
    registry = ProviderRegistry({"groq": lambda: provider})
    executions = ExecutionService(sf, orchestrator=orch, providers=registry, short_term=ShortTermMemory(client=redis), long_term=MemoryRepository(sf), limits=limits)

    def service(**kw):
        return PlanRunService(sf, executions=executions, registry=kw.pop("registry", RunRegistry()), busy_poll=0.01, busy_budget=kw.pop("busy_budget", 0.6), **kw)

    return dict(db=auth_db, sf=sf, provider=provider, orch=orch, limits=limits, svc=service(), service=service, agents=AgentService(sf), a=a, b=b,
                planning=PlanningService(sf, providers=registry, limits=limits), sa=authorize_personal_scope(sf, a), sb=authorize_personal_scope(sf, b),
                executions=executions)


def make_plan(world, tasks, scope=None, approve=True, objective=OBJECTIVE):
    scope = scope or world["sa"]
    world["provider"].reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "tasks": list(tasks), "unsupported": []})
    plan = asyncio.run(world["planning"].create_plan(scope, objective))
    world["provider"].reply = lambda request: f"custom reply: {request.messages[-1].content[-60:]}"
    if approve:
        world["planning"].approve(scope, plan.id, 1)
    return plan.id


async def execute(world, plan_id, scope=None, timeout=20, svc=None):
    scope, svc = scope or world["sa"], svc or world["svc"]
    started = await svc.start(scope, plan_id)
    await svc.registry.wait(started.id, timeout)
    return await asyncio.to_thread(svc.store.get, scope, started.id)


def go(world, plan_id, **kw):
    return asyncio.run(execute(world, plan_id, **kw))


def statuses(view):
    return {t.key: t.status for t in view.tasks}


def counts(db):
    return {t: db.scalar(f"SELECT count(*) FROM {t}") for t in ("plan_runs", "plan_task_runs", "agent_executions")}


CHAIN = [task("a", "research"), task("b", "sales", ["a"]), task("c", "ceo", ["b"])]


# ------------------------------------------------------------------------------------------------ the approval gate

def test_only_an_approved_plan_can_be_executed_and_a_refusal_changes_nothing(world):
    draft = make_plan(world, CHAIN, approve=False)
    cancelled = make_plan(world, CHAIN)
    world["planning"].store.cancel(world["sa"], cancelled)
    stale = make_plan(world, CHAIN)
    world["planning"].revise(world["sa"], stale, 1, None, [ProposedTask.model_validate(task("z", "research"))])  # revising withdraws the approval
    for plan_id, error in ((draft, PlanNotApproved), (cancelled, PlanCancelledForRun), (stale, PlanNotApproved)):
        with pytest.raises(error):
            asyncio.run(world["svc"].start(world["sa"], plan_id))
    assert counts(world["db"]) == {"plan_runs": 0, "plan_task_runs": 0, "agent_executions": 0} and world["orch"].calls == []


def test_foreign_unknown_and_forged_plans_are_indistinguishable(world):
    plan_id = make_plan(world, CHAIN)
    import uuid

    for scope, pid in ((world["sb"], plan_id), (world["sa"], uuid.uuid4())):
        with pytest.raises(PlanNotFound):
            asyncio.run(world["svc"].start(scope, pid))
    for bad in (None, "a@example.com", object()):
        with pytest.raises(ScopeError):
            asyncio.run(world["svc"].start(bad, plan_id))
    assert counts(world["db"])["plan_runs"] == 0


def test_the_exact_approved_revision_is_executed(world):
    plan_id = make_plan(world, CHAIN)
    first = go(world, plan_id)
    assert (first.revision, first.attempt, first.status) == (1, 1, "succeeded") and set(statuses(first)) == {"a", "b", "c"}
    world["planning"].revise(world["sa"], plan_id, 1, None, [ProposedTask.model_validate(task("x", "marketing")), ProposedTask.model_validate(task("y", "finance", ["x"]))])
    with pytest.raises(PlanNotApproved):
        asyncio.run(world["svc"].start(world["sa"], plan_id))  # revision 2 is a draft
    world["planning"].approve(world["sa"], plan_id, 2)
    second = go(world, plan_id)
    assert (second.revision, second.attempt, second.status) == (2, 2, "succeeded") and set(statuses(second)) == {"x", "y"}
    assert statuses(world["svc"].store.get(world["sa"], first.id)) == {"a": "succeeded", "b": "succeeded", "c": "succeeded"}  # the earlier run is untouched
    assert [r[0] for r in world["db"].rows("SELECT attempt FROM plan_runs ORDER BY attempt")] == [1, 2]


def test_agents_unavailable_at_start_refuse_the_run_before_anything_is_recorded(world):
    agent = world["agents"].create(world["sa"], "writer", definition())
    plan_id = make_plan(world, [task("a", "research"), task("b", "writer", ["a"])])
    world["agents"].archive(world["sa"], agent.id)
    with pytest.raises(RunInvalid) as info:
        asyncio.run(world["svc"].start(world["sa"], plan_id))
    assert [(e["code"], e["task"]) for e in info.value.errors] == [("agent_unavailable", "b")]
    assert counts(world["db"]) == {"plan_runs": 0, "plan_task_runs": 0, "agent_executions": 0} and world["orch"].calls == []
    world["agents"].unarchive(world["sa"], agent.id)
    assert go(world, plan_id).status == "succeeded"  # the approved version is available again


# ------------------------------------------------------------------------------------------------ ordering, inputs, records

def test_tasks_run_in_dependency_order_and_receive_their_prerequisites_results(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    assert run.status == "succeeded" and [t.status for t in run.tasks] == ["succeeded"] * 3
    assert [name for name, _ in world["orch"].calls] == ["research", "sales", "ceo"]
    inputs = dict(world["orch"].calls)
    assert OBJECTIVE in inputs["research"] and "Results of the tasks you depend on" not in inputs["research"]
    assert "result of research" in inputs["sales"] and "result of sales" in inputs["ceo"] and "result of research" not in inputs["ceo"]  # only direct prerequisites
    executions = world["db"].rows("SELECT agent_key, status FROM agent_executions ORDER BY created_at")
    assert executions == [("research", "succeeded"), ("sales", "succeeded"), ("ceo", "succeeded")]
    for t in run.tasks:  # every task run links its own M2-B execution; nothing is duplicated
        assert t.execution_id is not None and t.started_at and t.completed_at
    assert len({t.execution_id for t in run.tasks}) == 3 and counts(world["db"])["agent_executions"] == 3
    detail, outputs = asyncio.run(world["svc"].detail(world["sa"], run.id))
    assert [(o["task"], o["result"]) for o in outputs] == [("c", "result of ceo")]  # the final output is the leaf's, traceable to its execution
    assert outputs[0]["execution_id"] == [t for t in run.tasks if t.key == "c"][0].execution_id


def test_a_dependent_task_never_starts_before_its_prerequisite_has_succeeded(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    log = []

    async def behaviour(name, text, scope):
        log.append(("start", name))
        await asyncio.sleep(0.05)
        log.append(("end", name))
        return f"result of {name}"

    world["orch"].behaviour = behaviour
    assert go(world, plan_id).status == "succeeded"
    assert log == [("start", "research"), ("end", "research"), ("start", "sales"), ("end", "sales")]


def test_independent_tasks_run_in_parallel_and_never_exceed_the_shared_budget(world):
    plan_id = make_plan(world, [task(f"t{i}", "research") for i in range(6)] + [task("join", "ceo", [f"t{i}" for i in range(6)])])

    async def behaviour(name, text, scope):
        assert world["limits"]._active <= world["limits"].total and world["limits"]._by_user[world["sa"].user_id] <= 3
        await asyncio.sleep(0.4)
        return "ok"

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    assert run.status == "succeeded" and world["orch"].max_active == 3  # parallel, capped by max_parallel / per-user limit
    ordered = [name for name, _ in world["orch"].calls]
    assert ordered[-1] == "ceo" and len(ordered) == 7  # the join runs last, once
    world["limits"].total = 1  # a tighter shared budget is respected: tasks wait for a slot instead of failing
    world["orch"].max_active = 0
    world["planning"].revise(world["sa"], plan_id, 1, None, [ProposedTask.model_validate(task(f"u{i}", "research")) for i in range(3)])
    world["planning"].approve(world["sa"], plan_id, 2)
    world["limits"]._active = 0
    run = go(world, plan_id, svc=world["service"](busy_budget=20))
    assert run.status == "succeeded" and world["orch"].max_active == 1


def test_a_task_that_waits_too_long_for_a_slot_fails_honestly(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    world["limits"].total = 1
    assert world["limits"].acquire(world["sb"].user_id)  # another user holds the only slot
    run = go(world, plan_id, svc=world["service"](busy_budget=0.1))
    assert (run.status, statuses(run)) == ("failed", {"a": "failed", "b": "skipped"})
    assert run.tasks[0].error_code == "busy" and run.tasks[1].error_code == "dependency_failed" and counts(world["db"])["agent_executions"] == 0


# ------------------------------------------------------------------------------------------------ failure behaviour

def test_a_failed_prerequisite_skips_its_dependants_and_the_run_is_partial(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"]), task("c", "ceo", ["b"]), task("d", "marketing", ["a"]), task("e", "finance", ["c", "d"])])

    async def behaviour(name, text, scope):
        if name == "sales":
            raise RuntimeError("SECRET-INTERNAL gsk_abcdefghijklmnopqrstuvwxyz123456")
        return f"result of {name}"

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    assert run.status == "partial" and statuses(run) == {"a": "succeeded", "b": "failed", "c": "skipped", "d": "succeeded", "e": "skipped"}
    by = {t.key: t for t in run.tasks}
    assert (by["b"].error_code, by["c"].error_code, by["e"].error_code) == ("internal_error", "dependency_failed", "dependency_failed")
    assert by["c"].execution_id is None and by["b"].execution_id is not None
    assert counts(world["db"])["agent_executions"] == 3  # skipped tasks create no execution record
    assert run.counts["succeeded"] == 2 and run.counts["failed"] == 1 and run.counts["skipped"] == 2
    assert "SECRET" not in str(world["db"].rows("SELECT * FROM plan_task_runs")) + str(world["db"].rows("SELECT * FROM agent_executions")) + str(world["db"].rows("SELECT * FROM plan_runs"))
    assert world["db"].scalar(f"SELECT status FROM agent_executions WHERE id = '{by['b'].execution_id.hex}'") == "failed"


def test_when_nothing_succeeds_the_run_failed(world):
    plan_id = make_plan(world, CHAIN)

    async def behaviour(name, text, scope):
        raise RuntimeError("boom")

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    assert (run.status, statuses(run)) == ("failed", {"a": "failed", "b": "skipped", "c": "skipped"})


def test_a_failed_run_is_never_retried_automatically_and_a_new_run_is_an_explicit_new_attempt(world):
    plan_id = make_plan(world, CHAIN)

    async def boom(name, text, scope):
        raise RuntimeError("boom")

    world["orch"].behaviour = boom
    first = go(world, plan_id)
    assert first.status == "failed" and len(world["orch"].calls) == 1  # exactly one dispatch, no silent retry
    world["orch"].behaviour = None
    second = go(world, plan_id)
    assert (second.attempt, second.status) == (2, "succeeded")
    kept = world["svc"].store.get(world["sa"], first.id)
    assert (kept.status, statuses(kept)) == ("failed", {"a": "failed", "b": "skipped", "c": "skipped"})  # history is never overwritten
    with pytest.raises(AlreadyExecuted):
        asyncio.run(world["svc"].start(world["sa"], plan_id))  # a revision that already succeeded is not executed again


def test_provider_failures_and_timeouts_of_custom_agents_are_normalised(world):
    world["agents"].create(world["sa"], "writer", definition())
    for code in ("timeout", "rate_limited", "unavailable", "unsupported_model"):
        world["provider"].error = None
        plan_id = make_plan(world, [task("a", "writer"), task("b", "research", ["a"])])
        world["provider"].error = ProviderError(code)
        run = go(world, plan_id)
        assert (run.status, run.tasks[0].error_code, run.tasks[1].status) == ("failed", code, "skipped"), code
        assert run.tasks[0].error_message and "SECRET" not in run.tasks[0].error_message
    assert world["orch"].calls == []  # the dependent built-in task never ran


def test_a_scheduler_bug_never_leaves_the_run_running(world, monkeypatch):
    plan_id = make_plan(world, CHAIN)

    def explode(*args, **kwargs):
        raise ValueError("SECRET-BUG")

    monkeypatch.setattr(plan_runs_module, "compose_input", explode)
    run = go(world, plan_id)
    assert run.status == "failed" and all(t.status in ("failed", "skipped") for t in run.tasks)
    assert "SECRET" not in str(world["db"].rows("SELECT * FROM plan_task_runs"))
    assert world["db"].scalar("SELECT count(*) FROM plan_runs WHERE status = 'running'") == 0 and world["db"].scalar("SELECT count(*) FROM plan_task_runs WHERE status IN ('running', 'pending')") == 0


# ------------------------------------------------------------------------------------------------ pinning

def test_custom_tasks_run_the_exact_approved_version_even_after_the_agent_was_edited(world):
    agent = world["agents"].create(world["sa"], "writer", definition())
    plan_id = make_plan(world, [task("a", "writer")])
    world["agents"].update(world["sa"], agent.id, 1, definition(role="A COMPLETELY DIFFERENT ROLE", instructions="You now behave in a new way entirely."))
    run = go(world, plan_id)
    assert run.status == "succeeded" and (run.tasks[0].agent_version, run.tasks[0].agent_version_id) == (1, agent.version.version_id)
    system = world["provider"].requests[-1].system
    assert "Product marketer" in system and "COMPLETELY DIFFERENT" not in system  # version 1 was executed, not the current version 2
    row = world["db"].rows("SELECT agent_version, agent_version_id FROM agent_executions")[0]
    assert row == (1, agent.version.version_id.hex)
    # a new plan picks up the current version
    world["agents"].update(world["sa"], agent.id, 2, definition(role="A COMPLETELY DIFFERENT ROLE"))
    plan2 = make_plan(world, [task("a", "writer")])
    assert go(world, plan2).tasks[0].agent_version == 3


def test_a_pin_that_no_longer_resolves_is_refused_and_never_falls_back_to_another_version(world):
    agent = world["agents"].create(world["sa"], "writer", definition())
    other = world["agents"].create(world["sa"], "second", definition(name="Second Agent"))
    plan_id = make_plan(world, [task("a", "writer")])
    resolver = world["executions"].agents
    assert resolver.resolve_pinned(world["sa"], "writer", agent.version.version_id) is not None
    assert resolver.resolve_pinned(world["sa"], "writer", other.version.version_id) is None  # a version of ANOTHER agent
    assert resolver.resolve_pinned(world["sb"], "writer", agent.version.version_id) is None  # a foreign scope
    assert resolver.resolve_pinned(world["sa"], "writer", None) is None and resolver.resolve_pinned(world["sa"], "research", agent.version.version_id) is None
    assert resolver.resolve_pinned(world["sa"], "research", None).source == "builtin"
    world["agents"].archive(world["sa"], agent.id)
    assert resolver.resolve_pinned(world["sa"], "writer", agent.version.version_id) is None
    with pytest.raises(RunInvalid):
        asyncio.run(world["svc"].start(world["sa"], plan_id))


def test_an_agent_archived_after_the_run_started_fails_that_task_without_running_another_version(world):
    agent = world["agents"].create(world["sa"], "writer", definition())
    plan_id = make_plan(world, [task("a", "research"), task("b", "writer", ["a"])])

    async def behaviour(name, text, scope):
        world["agents"].archive(world["sa"], agent.id)  # archived while the first task runs
        return "ok"

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    assert (run.status, statuses(run), run.tasks[1].error_code) == ("partial", {"a": "succeeded", "b": "failed"}, "agent_unavailable")
    assert len(world["provider"].requests) == 1 and run.tasks[1].execution_id is None  # only the planner's own call: the custom agent never ran


# ------------------------------------------------------------------------------------------------ duplicate and concurrent starts

def test_concurrent_starts_of_the_same_plan_create_exactly_one_run(world):
    plan_id = make_plan(world, CHAIN)
    gate = asyncio.Event

    async def scenario():
        release = gate()

        async def behaviour(name, text, scope):
            await release.wait()
            return "ok"

        world["orch"].behaviour = behaviour
        results = await asyncio.gather(*[world["svc"].start(world["sa"], plan_id) for _ in range(6)], return_exceptions=True)
        winners = [r for r in results if not isinstance(r, Exception)]
        assert len(winners) == 1 and all(isinstance(r, RunInProgress) for r in results if isinstance(r, Exception)), results
        assert world["db"].scalar("SELECT count(*) FROM plan_runs") == 1 and world["db"].scalar("SELECT count(*) FROM plan_task_runs") == 3
        with pytest.raises(RunInProgress):
            await world["svc"].start(world["sa"], plan_id)
        release.set()
        await world["svc"].registry.wait(winners[0].id, 20)
        return world["svc"].store.get(world["sa"], winners[0].id)

    run = asyncio.run(scenario())
    assert run.status == "succeeded" and len(world["orch"].calls) == 3 and counts(world["db"])["agent_executions"] == 3  # each task dispatched once


def test_the_active_run_limit_per_user(world):
    plans = [make_plan(world, [task("a", "research")]) for _ in range(MAX_ACTIVE_RUNS_PER_USER + 1)]

    async def scenario():
        release = asyncio.Event()

        async def behaviour(name, text, scope):
            await release.wait()
            return "ok"

        world["orch"].behaviour = behaviour
        started = [await world["svc"].start(world["sa"], p) for p in plans[:MAX_ACTIVE_RUNS_PER_USER]]
        with pytest.raises(RunLimit) as info:
            await world["svc"].start(world["sa"], plans[-1])
        assert info.value.what == "active_runs"
        release.set()
        for r in started:
            await world["svc"].registry.wait(r.id, 20)
        assert (await world["svc"].start(world["sa"], plans[-1])).status == "running"
        await world["svc"].registry.wait(world["svc"].registry._tasks and next(iter(world["svc"].registry._tasks)), 20)

    asyncio.run(scenario())


# ------------------------------------------------------------------------------------------------ cancellation

def test_cancelling_a_running_plan_stops_work_records_everything_and_leaves_nothing_running(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"]), task("c", "marketing"), task("d", "finance", ["c"])])

    async def scenario():
        async def behaviour(name, text, scope):
            await asyncio.sleep(30)

        world["orch"].behaviour = behaviour
        run = await world["svc"].start(world["sa"], plan_id)
        for _ in range(200):
            if world["svc"].store.get(world["sa"], run.id).counts["running"] == 2:
                break
            await asyncio.sleep(0.01)
        cancelled = await world["svc"].cancel(world["sa"], run.id)
        again = await world["svc"].cancel(world["sa"], run.id)  # idempotent
        return run, cancelled, again

    run, cancelled, again = asyncio.run(scenario())
    assert cancelled.status == "cancelled" and again.status == "cancelled" and cancelled.error_code == "cancelled"
    assert statuses(cancelled) == {"a": "cancelled", "b": "cancelled", "c": "cancelled", "d": "cancelled"}
    assert world["db"].rows("SELECT status, error_code FROM agent_executions ORDER BY agent_key") == [("cancelled", "cancelled"), ("cancelled", "cancelled")]
    assert world["db"].scalar("SELECT count(*) FROM plan_runs WHERE status = 'running'") == 0
    assert world["db"].scalar("SELECT count(*) FROM plan_task_runs WHERE status IN ('running', 'pending')") == 0
    assert world["limits"].acquire(world["sa"].user_id) and world["svc"].registry.active() == 0  # budget released, driver gone
    assert world["db"].scalar("SELECT count(*) FROM agent_executions") == 2  # tasks b and d never started
    assert go(world, plan_id).attempt == 2  # a cancelled run can be started again explicitly


def test_cancelling_a_finished_run_changes_nothing(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    assert asyncio.run(world["svc"].cancel(world["sa"], run.id)) == run


# ------------------------------------------------------------------------------------------------ restart recovery

def test_orphaned_runs_are_closed_honestly_once_their_lease_has_expired(world):
    """M4-A1: the old test closed every running run at any restart. Now a run is closed only when its OWNER is gone (lease
    expired or missing); the outcomes of the closed run are exactly the ones M3-B produced."""
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"]), task("c", "marketing"), task("d", "finance", ["c"])])
    store, executions = world["svc"].store, ExecutionStore(world["sf"])
    run_id = store.create(world["sa"], plan_id)
    _, tasks = store.load_for_driver(run_id)
    by = {t.key: t for t in tasks}
    resolved = ResolvedAgent("builtin", "research", None, None, None)
    done = executions.start(world["sa"], resolved, "Research", "t", provider="groq", model=None)
    store.mark_running(by["a"].id, done)
    executions.finish(done, "succeeded", result="kept")
    store.finish_task(by["a"].id, "succeeded")
    live = executions.start(world["sa"], resolved, "Marketing", "t", provider="groq", model=None)
    store.mark_running(by["c"].id, live)  # in flight when the process "died"
    assert executions.fail_orphaned() == 0 and store.recover_expired() == []  # the owner's lease is still live: nothing is touched
    assert store.get(world["sa"], run_id).status == "running"
    world["db"].run("UPDATE run_leases SET expires_at = '2000-01-01 00:00:00.000000'")  # the owner stopped renewing
    assert store.recover_expired() == [run_id] and store.recover_expired() == [] and executions.fail_orphaned() == 0
    assert world["db"].scalar(f"SELECT status || '/' || error_code FROM agent_executions WHERE id = '{live.hex}'") == "failed/interrupted"  # closed with its run
    run = store.get(world["sa"], run_id)
    assert (run.status, run.error_code) == ("partial", "interrupted")
    assert statuses(run) == {"a": "succeeded", "b": "skipped", "c": "failed", "d": "skipped"}
    assert {t.key: t.error_code for t in run.tasks if t.error_code} == {"b": "interrupted", "c": "interrupted", "d": "interrupted"}
    assert world["db"].scalar(f"SELECT result FROM agent_executions WHERE id = '{done.hex}'") == "kept"  # completed work is kept
    assert asyncio.run(world["svc"].start(world["sa"], plan_id)).attempt == 2  # a new run may be started explicitly


# ------------------------------------------------------------------------------------------------ isolation

def test_runs_are_isolated_between_users_and_workspaces(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    theirs = make_plan(world, CHAIN, scope=world["sb"])
    other = go(world, theirs, scope=world["sb"])
    for call in (lambda: world["svc"].store.get(world["sb"], run.id), lambda: asyncio.run(world["svc"].detail(world["sb"], run.id)),
                 lambda: asyncio.run(world["svc"].task_detail(world["sb"], run.id, "a")), lambda: asyncio.run(world["svc"].cancel(world["sb"], run.id)),
                 lambda: world["svc"].store.list_for_plan(world["sb"], plan_id)):
        with pytest.raises((RunNotFound, PlanNotFound)):
            call()
    assert [r.id for r in world["svc"].store.list_for_plan(world["sa"], plan_id)] == [run.id]
    assert world["svc"].store.get(world["sb"], other.id).status == "succeeded"
    with pytest.raises(TaskRunNotFound):
        asyncio.run(world["svc"].task_detail(world["sa"], run.id, "nope"))
    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    member = authorize_memory_scope(world["sf"], world["b"], team)
    with pytest.raises((RunNotFound, PlanNotFound)):
        world["svc"].store.get(member, run.id)  # same user id, different workspace: not visible
    assert world["db"].rows("SELECT DISTINCT user_id FROM plan_runs ORDER BY 1") == sorted([(world["sa"].user_id.hex,), (world["sb"].user_id.hex,)])


def test_task_detail_returns_the_linked_execution_record(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    view, task_view, record = asyncio.run(world["svc"].task_detail(world["sa"], run.id, "b"))
    assert task_view.status == "succeeded" and record["result"] == "result of sales" and record["status"] == "succeeded" and "result of research" in record["input"]
    assert record["execution_id"] == task_view.execution_id


def test_secrets_in_results_are_redacted_in_the_stored_inputs_of_later_tasks(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"])])
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

    async def behaviour(name, text, scope):
        return f"the key is {secret}" if name == "research" else "ok"

    world["orch"].behaviour = behaviour
    assert go(world, plan_id).status == "succeeded"
    assert secret not in str(world["db"].rows("SELECT task FROM agent_executions"))


def test_the_scheduler_does_not_run_more_than_the_ready_tasks_and_listing_is_newest_first(world):
    plan_id = make_plan(world, CHAIN)
    first = go(world, plan_id)
    world["db"].run("DELETE FROM plan_runs WHERE 1 = 0")
    runs = world["svc"].store.list_for_plan(world["sa"], plan_id)
    assert [(r.attempt, r.status, r.revision) for r in runs] == [(1, "succeeded", 1)] and runs[0].counts["succeeded"] == 3


def test_an_approval_withdrawn_between_the_gate_and_the_insert_still_refuses_the_run(world, monkeypatch):
    """The service pre-check passes, then a revision commits (withdrawing the approval) before the run is created: the
    transaction in ``PlanRunStore.create`` re-checks the approval itself, so nothing is executed."""
    plan_id = make_plan(world, CHAIN)
    original = world["svc"]._validate_gate

    def gate_then_revise(scope, pid):
        original(scope, pid)
        world["planning"].revise(world["sa"], plan_id, 1, None, [ProposedTask.model_validate(task("z", "research"))])

    monkeypatch.setattr(world["svc"], "_validate_gate", gate_then_revise)
    with pytest.raises(PlanNotApproved):
        asyncio.run(world["svc"].start(world["sa"], plan_id))
    assert counts(world["db"]) == {"plan_runs": 0, "plan_task_runs": 0, "agent_executions": 0} and world["orch"].calls == []


def test_a_started_run_keeps_executing_its_own_revision_when_the_plan_is_revised_meanwhile(world):
    plan_id = make_plan(world, CHAIN)

    async def scenario():
        release = asyncio.Event()

        async def behaviour(name, text, scope):
            await release.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await world["svc"].start(world["sa"], plan_id)
        world["planning"].revise(world["sa"], plan_id, 1, None, [ProposedTask.model_validate(task("z", "finance"))])  # the plan moves on
        release.set()
        await world["svc"].registry.wait(run.id, 20)
        return world["svc"].store.get(world["sa"], run.id)

    run = asyncio.run(scenario())
    assert (run.revision, run.status, set(statuses(run))) == (1, "succeeded", {"a", "b", "c"}) and [n for n, _ in world["orch"].calls] == ["research", "sales", "ceo"]


# ------------------------------------------------------------------------------------------------ cancellation contract

def plan_header(world):
    return world["db"].rows("SELECT status, latest_revision, approved_revision, approved_at, updated_at FROM plans"), world["db"].rows("SELECT revision FROM plan_revisions ORDER BY 1")


def test_cancelling_the_plan_prevents_future_runs_but_a_running_run_continues_until_cancelled_itself(world):
    plan_id = make_plan(world, CHAIN)

    async def scenario():
        release = asyncio.Event()

        async def behaviour(name, text, scope):
            await release.wait()
            return f"result of {name}"

        world["orch"].behaviour = behaviour
        run = await world["svc"].start(world["sa"], plan_id)
        assert world["planning"].store.cancel(world["sa"], plan_id).status == "cancelled"  # the plan is cancelled mid-run
        await asyncio.sleep(0.05)
        assert world["svc"].store.get(world["sa"], run.id).status == "running"  # the run is NOT cancelled by it
        with pytest.raises(PlanCancelledForRun):
            await world["svc"].start(world["sa"], plan_id)  # no future runs
        release.set()
        await world["svc"].registry.wait(run.id, 20)
        return world["svc"].store.get(world["sa"], run.id)

    run = asyncio.run(scenario())
    assert (run.status, set(statuses(run).values())) == ("succeeded", {"succeeded"})  # it ran to completion
    assert world["planning"].store.get(world["sa"], plan_id).status == "cancelled"  # and the plan history was not rewritten by it
    # ... and a run that is cancelled explicitly through its own endpoint stops (covered by test_cancelling_a_running_plan_stops_work...)


def test_cancelling_a_run_never_cancels_or_rewrites_the_plan(world):
    plan_id = make_plan(world, CHAIN)
    before = plan_header(world)

    async def scenario():
        async def behaviour(name, text, scope):
            await asyncio.sleep(30)

        world["orch"].behaviour = behaviour
        run = await world["svc"].start(world["sa"], plan_id)
        for _ in range(200):
            if world["svc"].store.get(world["sa"], run.id).counts["running"] == 1:
                break
            await asyncio.sleep(0.01)
        return await world["svc"].cancel(world["sa"], run.id)

    assert asyncio.run(scenario()).status == "cancelled"
    assert plan_header(world) == before  # status, approval, revisions and updated_at: untouched
    view = world["planning"].store.get(world["sa"], plan_id)
    assert (view.status, view.approved_revision, view.latest_revision) == ("approved", 1, 1)
    assert world["planning"].store.latest_run(world["sa"], plan_id)["status"] == "cancelled"
