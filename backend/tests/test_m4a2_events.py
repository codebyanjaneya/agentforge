"""M4-A2 Part 4: the append-only execution event history.

Events are written in the same transaction as the state change they describe, are scoped like the run, are totally ordered by id, cannot
be updated or deleted, and can only contain the whitelisted codes, keys, ids and numbers (never task text, results or secrets)."""

import asyncio
import json
import threading
import time

import pytest
from sqlalchemy.exc import DatabaseError

from agentplatform import run_events
from agentplatform.plan_runs import PlanRunStore, RunNotFound, RunRegistry
from agentplatform.providers import ProviderError
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_m4a2_consistency import inject_once
from tests.test_m4a2_retry import calls, failing, retry
from tests.test_plan_runs import CHAIN, go, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, RUNNING_TASKS, expire, stuck_agent, until


def history(world, run_id):
    """[(type, task, actor, code)] of a run in event order."""
    page = world["svc"].store.events(world["sa"], run_id, 0, 200)
    return [(e.event_type, e.task_key, e.actor_type, e.detail.get("code")) for e in page["items"]]


def types(world, run_id):
    return [t for t, _k, _a, _c in history(world, run_id)]


# ------------------------------------------------------------------------------------------------ lifecycle

def test_a_normal_run_leaves_a_complete_ordered_history(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    events = world["svc"].store.events(world["sa"], run.id)["items"]
    assert [(e.event_type, e.task_key) for e in events] == [
        ("run_created", None), ("run_claimed", None), ("task_started", "a"), ("task_succeeded", "a"), ("task_started", "b"), ("task_succeeded", "b"),
        ("task_started", "c"), ("task_succeeded", "c"), ("run_completed", None)]
    assert [e.id for e in events] == sorted(e.id for e in events) and len({e.id for e in events}) == len(events)  # a strict total order
    assert [e.actor_type for e in events][:2] == ["user", "api"] and all(e.actor_type == "api" for e in events[2:])
    assert events[0].detail == {"attempt": 1, "count": 3} and events[-1].detail == {"status": "succeeded"} and events[1].detail == {"generation": 1}
    started = {e.task_key: e.execution_id for e in events if e.event_type == "task_started"}
    assert started == {t.key: t.execution_id for t in run.tasks}  # every task event names its execution record
    rows = world["db"].rows("SELECT DISTINCT user_id, workspace_id FROM execution_events")
    assert rows == [(world["sa"].user_id.hex, world["sa"].workspace_id.hex)]  # scoped exactly like the run


def test_failures_skips_and_the_final_status_are_recorded_with_codes(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    run = go(world, plan_id)
    assert history(world, run.id)[2:] == [("task_started", "a", "api", None), ("task_succeeded", "a", "api", None), ("task_started", "b", "api", None),
                                           ("task_failed", "b", "api", "rate_limited"), ("task_skipped", "c", "api", "dependency_failed"), ("run_completed", None, "api", None)]
    assert world["svc"].store.events(world["sa"], run.id)["items"][-1].detail == {"status": "partial"}


def test_cancellation_requested_and_acknowledged_are_recorded_for_a_cross_process_cancel(world):
    plan_id = make_plan(world, CHAIN)
    reg_a, reg_b = RunRegistry(config=FAST), RunRegistry(config=FAST)
    svc_a, svc_b = world["service"](registry=reg_a), world["service"](registry=reg_b)

    async def scenario():
        stuck_agent(world, [])
        run = await svc_a.start(world["sa"], plan_id)
        await until(world, RUNNING_TASKS, 1)
        await svc_b.cancel(world["sa"], run.id)
        await reg_a.wait(run.id, 20)
        return run.id

    run_id = asyncio.run(scenario())
    h = history(world, run_id)
    assert h[0][0] == "run_created" and ("cancellation_requested", None, "user", None) in h  # the requester is the user
    tail = [t for t, *_ in h][h.index(("cancellation_requested", None, "user", None)):]
    assert tail == ["cancellation_requested", "task_cancelled", "task_cancelled", "task_cancelled", "cancellation_acknowledged", "run_completed"] or \
        tail[0] == "cancellation_requested" and tail[-3:] == ["task_cancelled", "cancellation_acknowledged", "run_completed"] and "cancellation_acknowledged" in tail
    assert dict((t, a) for t, _k, a, _c in h)["cancellation_acknowledged"] == "api"  # acknowledged by the owner process
    assert world["db"].scalar("SELECT count(*) FROM execution_events WHERE event_type = 'cancellation_requested'") == 1  # idempotent: one request, one event


def test_recovery_is_recorded_with_the_recovery_actor_and_reconciliation_is_visible(world):
    from agentplatform.execution import ExecutionStore
    from agentplatform.run_leases import Fence
    from agentplatform.service import ResolvedAgent

    plan_id = make_plan(world, CHAIN)
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    fence = Fence(run_id, store.owner_id, 1)
    _, tasks = store.load_for_driver(run_id)
    by = {t.key: t for t in tasks}
    resolved = ResolvedAgent("builtin", "research", None, None, None)
    done = store.start_task(by["a"].id, world["sa"], resolved, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    # the old crash window, simulated: the execution ended but the task row never followed (these two writes are one transaction in M4-A2)
    ExecutionStore(world["sf"]).finish(done, "succeeded", result="kept")
    store.start_task(by["b"].id, world["sa"], resolved, "Sales", "t", "groq", None, executions=world["executions"].store, fence=fence)  # in flight
    expire(world)
    assert store.recover_expired() == [run_id]
    h = history(world, run_id)
    assert ("task_succeeded", "a", "recovery", None) in h  # reconciled, and recorded as such
    assert ("task_interrupted", "b", "recovery", "interrupted") in h and ("task_skipped", "c", "recovery", "interrupted") in h
    kinds = [t for t, *_ in h]
    assert kinds[-2:] == ["recovery_performed", "run_completed"]
    detail = [e for e in store.events(world["sa"], run_id)["items"] if e.event_type == "recovery_performed"][0].detail
    assert detail == {"code": "interrupted", "reason": "lease_expired", "reconciled": 1, "status": "partial"}


def test_a_retry_records_who_authorised_what_and_the_reused_tasks(world):
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=RuntimeError("unknown outcome"))
    first = go(world, plan_id)
    failing(world)
    second = retry(world, first.id, acknowledge=["b"])
    events = world["svc"].store.events(world["sa"], second.id)["items"]
    assert [e.event_type for e in events][:4] == ["retry_authorized", "task_reused", "run_created", "run_claimed"]
    assert events[0].actor_type == "user" and events[0].detail == {"acknowledged": ["b"], "attempt": 2, "carried": ["a"], "retried": ["b", "c"], "retry_of_attempt": 1}
    assert events[1].task_key == "a" and events[1].actor_type == "user"
    assert [t for t in types(world, second.id) if t == "task_started"] == ["task_started", "task_started"]  # only b and c were dispatched again


# ------------------------------------------------------------------------------------------------ the contract

@pytest.mark.parametrize("table, column", [("execution_events", "task_key"), ("plan_run_retries", "carried_count"), ("plan_task_carryovers", "task_key"), ("execution_owners", "owner_id")])
def test_the_history_tables_refuse_updates_and_deletes(world, table, column):
    world["executions"].store.owner_id = "owner-under-test"  # executions of a registered process carry an owner row
    plan_id = make_plan(world, CHAIN)
    failing(world, sales=ProviderError("rate_limited"))
    first = go(world, plan_id)
    retry(world, first.id)  # populates retries and carry-overs too
    assert world["db"].scalar(f"SELECT count(*) FROM {table}") > 0
    for statement in (f"UPDATE {table} SET {column} = {column}", f"DELETE FROM {table}"):
        with pytest.raises(DatabaseError) as info:
            world["db"].run(statement)
        assert "append-only" in str(info.value)
    before = world["db"].scalar(f"SELECT count(*) FROM {table}")
    assert before > 0 and world["db"].scalar(f"SELECT count(*) FROM {table}") == before


def test_an_event_exists_if_and_only_if_its_transition_committed(world, monkeypatch):
    plan_id = make_plan(world, [task("a", "research")])
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    events_before = world["db"].scalar("SELECT count(*) FROM execution_events")
    fired = inject_once(monkeypatch, "run_completed")  # the event write fails after the run row was updated
    with pytest.raises(RuntimeError):
        store.finish_run(run_id, "failed", "internal_error")
    assert fired == ["run_completed"]
    assert world["db"].rows("SELECT status FROM plan_runs") == [("running",)] and world["db"].scalar("SELECT count(*) FROM execution_events") == events_before  # both rolled back
    assert store.finish_run(run_id, "failed", "internal_error") is True  # and the transition works once the failure is gone
    assert world["db"].scalar("SELECT count(*) FROM execution_events WHERE event_type = 'run_completed'") == 1


def test_every_finished_task_has_exactly_one_terminal_event_and_every_started_one_a_start(world):
    plan_id = make_plan(world, [task("a", "research"), task("b", "sales", ["a"]), task("c", "marketing", ["a"]), task("d", "ceo", ["b", "c"])])
    failing(world, sales=RuntimeError("boom"))
    run = go(world, plan_id)
    events = world["svc"].store.events(world["sa"], run.id)["items"]
    for t in run.tasks:
        terminal = [e for e in events if e.task_key == t.key and e.event_type in ("task_succeeded", "task_failed", "task_skipped", "task_interrupted", "task_cancelled")]
        started = [e for e in events if e.task_key == t.key and e.event_type == "task_started"]
        assert len(terminal) == 1, t.key
        assert len(started) == (1 if t.execution_id else 0), t.key
    assert sum(e.event_type == "run_completed" for e in events) == 1


def test_event_details_accept_only_whitelisted_codes_keys_and_numbers():
    ok = run_events.build_detail({"status": "partial", "code": "timeout", "carried": ["b", "a"], "attempt": 2, "resumed": True})
    assert json.loads(ok) == {"attempt": 2, "carried": ["a", "b"], "code": "timeout", "resumed": True, "status": "partial"} and ok == run_events.build_detail(
        {"resumed": True, "attempt": 2, "code": "timeout", "carried": ["a", "b"], "status": "partial"})  # deterministic JSON
    for bad in ({"task_text": "write to alice@example.com"}, {"result": "sk-abcdefghijklmnopqrstuvwxyz123456"}, {"code": "x" * 65}, {"code": {"nested": "no"}},
                {"attempt": -1}, {"attempt": "2"}, {"carried": ["ok", "x" * 40]}, {"carried": [1]}, {"resumed": "yes"}, {"reason": "line\nbreak"}, {"password": "p"}):
        with pytest.raises(run_events.EventError):
            run_events.build_detail(bad)
    assert run_events.build_detail({}) is None and run_events.build_detail({"code": None}) is None
    with pytest.raises(run_events.EventError):
        run_events.add(None, None, "free_text", actor_type="user")
    with pytest.raises(run_events.EventError):
        run_events.add(None, None, "run_created", actor_type="admin")


def test_no_secret_or_task_content_ever_reaches_the_history(world):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    plan_id = make_plan(world, [task("a", "research")], objective=f"Launch the product to our first customers, credential {secret}, within a month.")

    async def behaviour(name, text, scope):
        return f"the answer mentions {secret} and alice@example.com"

    world["orch"].behaviour = behaviour
    run = go(world, plan_id)
    dump = json.dumps(world["db"].rows("SELECT * FROM execution_events"), default=str)
    assert run.status == "succeeded" and secret not in dump and "alice@example.com" not in dump and "Launch the product" not in dump and "the answer" not in dump


# ------------------------------------------------------------------------------------------------ scope and ordering

def test_the_history_is_scoped_unknown_and_foreign_runs_are_not_found_and_paging_is_stable(world):
    plan_id = make_plan(world, CHAIN)
    run = go(world, plan_id)
    store = world["svc"].store
    for scope in (world["sb"],):
        with pytest.raises(RunNotFound):
            store.events(scope, run.id)
    first = store.events(world["sa"], run.id, 0, 4)
    assert len(first["items"]) == 4 and first["next_after"] == first["items"][-1].id
    rest = store.events(world["sa"], run.id, first["next_after"], 100)
    assert rest["next_after"] is None and [e.id for e in first["items"] + rest["items"]] == [e.id for e in store.events(world["sa"], run.id, 0, 100)["items"]]
    for bad in ({"after": -1}, {"limit": 0}, {"limit": 201}):
        with pytest.raises(ValueError):
            store.events(world["sa"], run.id, **bad)
    other = go(world, make_plan(world, CHAIN, scope=world["sb"]), scope=world["sb"])
    assert {e.run_id for e in store.events(world["sa"], run.id, 0, 100)["items"]} == {run.id} and other.id != run.id  # never another run's events


def test_concurrent_writers_still_produce_one_strictly_increasing_order(world):
    plan_id = make_plan(world, CHAIN)
    stores = [PlanRunStore(world["sf"], config=FAST) for _ in range(4)]
    run_id = world["svc"].store.create(world["sa"], plan_id)
    barrier, errors = threading.Barrier(4), []

    def cancel(store):
        try:
            barrier.wait()
            store.leases.request_cancel(run_id, world["sa"].user_id)  # idempotent request from several processes at once
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=cancel, args=(s,)) for s in stores]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ids = [r[0] for r in world["db"].rows("SELECT id FROM execution_events WHERE run_id = :r ORDER BY id".replace(":r", f"'{run_id.hex}'"))]
    assert errors == [] and ids == sorted(set(ids)) and types(world, run_id).count("cancellation_requested") == 1
