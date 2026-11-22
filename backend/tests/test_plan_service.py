"""M3-A: persisted plans, immutable revisions, ownership, concurrency and the planning service (migrated disposable SQLite,
fake provider, no network)."""

import asyncio
import copy
import json
import threading
import uuid

import pytest

import agentplatform.plans as plans_module
from agentplatform.execution import Busy, CursorError, Limits
from agentplatform.planning import Planner, PlannerConfig, PlanningError, PlanRejected, ProposedTask, Unplannable
from agentplatform.plans import (
    PlanCancelled,
    PlanConflict,
    PlanInvalid,
    PlanLimitReached,
    PlanningService,
    PlanNotFound,
    RevisionNotFound,
)
from agentplatform.providers import ProviderError, ProviderRegistry
from agentplatform.schema import AgentDefinition
from agentplatform.service import AgentService
from authz.errors import ScopeError
from authz.workspaces import authorize_memory_scope, authorize_personal_scope
from tests.api_support import FakeProvider
from tests.auth_support import auth_db  # noqa: F401  (fixture)
from tests.test_authz_memory import make_user, team_workspace

BASE = {
    "name": "Release Notes Writer", "description": "Turns a changelog into friendly release notes for customers.", "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.", "instructions": "You write clear release notes from the changelog the user provides.",
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"},
}


def definition(**overrides):
    data = copy.deepcopy(BASE)
    data.update(overrides)
    return AgentDefinition.model_validate(data)


def task(id, agent, depends_on=(), **overrides):
    data = {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short written result.",
            "agent": agent, "rationale": "This agent fits the work.", "depends_on": list(depends_on)}
    data.update(overrides)
    return data


def reply(*tasks, feasible=True, unsupported=()):
    return json.dumps({"feasible": feasible, "interpretation": "The user wants a launch plan for the product.", "tasks": list(tasks), "unsupported": list(unsupported)})


OBJECTIVE = "Launch our new product to early customers within a month."


@pytest.fixture
def world(auth_db):  # noqa: F811
    a, _ = make_user(auth_db, "a@example.com")
    b, _ = make_user(auth_db, "b@example.com")
    sf = auth_db.session_factory
    provider = FakeProvider()
    provider.reply = lambda request: reply(task("research", "research"), task("draft", "sales", ["research"]), task("review", "ceo", ["draft"]))
    limits = Limits()
    registry = ProviderRegistry({"groq": lambda: provider})

    def service(**kw):
        return PlanningService(sf, providers=registry, limits=kw.pop("limits", limits), **kw)

    return dict(db=auth_db, sf=sf, provider=provider, service=service, svc=service(), agents=AgentService(sf), a=a, b=b,
                sa=authorize_personal_scope(sf, a), sb=authorize_personal_scope(sf, b), limits=limits)


def run(coro):
    return asyncio.run(coro)


def create(world, scope=None, objective=OBJECTIVE):
    return run(world["svc"].create_plan(scope or world["sa"], objective))


def counts(db):
    return {t: db.scalar(f"SELECT count(*) FROM {t}") for t in ("plans", "plan_revisions", "plan_tasks", "plan_task_dependencies", "agent_executions")}


def revised(*tasks):
    return [ProposedTask.model_validate(t) for t in tasks]


# ------------------------------------------------------------------------------------------------ creation

def test_a_plan_is_created_validated_and_persisted_without_executing_anything(world):
    plan = create(world)
    assert (plan.status, plan.latest_revision, plan.approved_revision, plan.objective) == ("draft", 1, None, OBJECTIVE)
    rev = plan.current
    assert (rev.revision, rev.origin, rev.provider, rev.model) == (1, "model", "groq", "openai/gpt-oss-120b")
    assert rev.interpretation and rev.levels == (("research",), ("draft",), ("review",))
    assert [(t.key, t.level, t.position, t.agent_key, t.agent_source, t.depends_on) for t in rev.tasks] == [
        ("research", 0, 0, "research", "builtin", ()), ("draft", 1, 1, "sales", "builtin", ("research",)), ("review", 2, 2, "ceo", "builtin", ("draft",))]
    assert counts(world["db"]) == {"plans": 1, "plan_revisions": 1, "plan_tasks": 3, "plan_task_dependencies": 2, "agent_executions": 0}  # nothing executed
    assert len(world["provider"].requests) == 1
    assert world["db"].rows("SELECT user_id, workspace_id FROM plans") == [(world["sa"].user_id.hex, world["sa"].workspace_id.hex)]  # from the scope


def test_the_stored_objective_is_a_redacted_copy_while_the_planner_saw_the_original(world):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    plan = create(world, objective=f"Plan our launch; our test key is {secret} and must not be shared.")
    assert secret not in plan.objective and "[redacted]" in plan.objective
    assert world["provider"].requests[0].messages[0].content.count(secret) == 1
    assert secret not in str(world["db"].rows("SELECT * FROM plans"))


def test_custom_agent_tasks_pin_the_exact_immutable_version(world):
    agent = world["agents"].create(world["sa"], "release-notes", definition())
    world["provider"].reply = lambda request: reply(task("t1", "research"), task("t2", "release-notes", ["t1"]))
    plan = create(world)
    t2 = [t for t in plan.current.tasks if t.key == "t2"][0]
    assert (t2.agent_source, t2.agent_version, t2.agent_name) == ("custom", 1, "Release Notes Writer") and t2.agent_version_id == agent.version.version_id
    assert world["db"].scalar("SELECT agent_version_id FROM plan_tasks WHERE task_key = 't2'") == agent.version.version_id.hex
    assert world["db"].scalar("SELECT agent_version_id FROM plan_tasks WHERE task_key = 't1'") is None  # a built-in task has no fake version
    system = world["provider"].requests[0].system
    assert 'key "release-notes" (custom)' in system and "Release Notes Writer" in system  # the planner was offered the real custom agent


def test_later_agent_edits_never_change_an_existing_plan(world):
    agent = world["agents"].create(world["sa"], "release-notes", definition())
    world["provider"].reply = lambda request: reply(task("t1", "release-notes"))
    plan = create(world)
    world["agents"].update(world["sa"], agent.id, 1, definition(role="A completely different role"))
    again = world["svc"].store.get(world["sa"], plan.id)
    assert again.current.tasks[0].agent_version == 1 and again.current.tasks[0].agent_version_id == agent.version.version_id


def test_unavailable_agents_reject_the_plan_and_store_nothing(world):
    archived = world["agents"].create(world["sa"], "old-agent", definition(name="Old Agent"))
    world["agents"].archive(world["sa"], archived.id)
    world["agents"].create(world["sb"], "theirs", definition(name="Theirs"))
    for ghost in ("invented", "old-agent", "theirs"):  # unknown, archived and someone else's agent
        world["provider"].reply = lambda request, ghost=ghost: reply(task("t1", "research"), task("t2", ghost, ["t1"]), task("t3", "sales", ["t2"]))
        with pytest.raises(PlanRejected) as info:
            create(world)
        assert {(e["code"], e["task"]) for e in info.value.errors} == {("agent_unavailable", "t2"), ("dependency_unsatisfiable", "t3")}
    assert "old-agent" not in world["provider"].requests[1].system  # archived agents are not even offered
    assert counts(world["db"]) == {"plans": 0, "plan_revisions": 0, "plan_tasks": 0, "plan_task_dependencies": 0, "agent_executions": 0}


def test_graph_errors_from_the_model_are_rejected(world):
    for tasks, code in (([task("a", "research", ["b"]), task("b", "sales", ["a"])], "cycle"), ([task("a", "research", ["a"])], "self_dependency"),
                        ([task("a", "research", ["zzz"])], "missing_dependency"), ([task("a", "research"), task("a", "sales")], "duplicate_task_id")):
        world["provider"].reply = lambda request, tasks=tasks: reply(*tasks)
        with pytest.raises(PlanRejected) as info:
            create(world)
        assert code in {e["code"] for e in info.value.errors}
    assert counts(world["db"])["plans"] == 0


def test_an_objective_that_cannot_be_planned_is_explained_not_faked(world):
    world["provider"].reply = lambda request: json.dumps({"feasible": False, "interpretation": "The user wants the company's accounts to be charged.",
                                                          "tasks": [], "unsupported": [{"requirement": "Charge customer cards", "reason": "No agent can take payments."}]})
    with pytest.raises(Unplannable) as info:
        create(world, objective="Charge every customer's card for the annual plan today.")
    assert info.value.unsupported == [{"requirement": "Charge customer cards", "reason": "No agent can take payments."}] and info.value.interpretation
    world["provider"].reply = lambda request: reply(feasible=True)  # "feasible" but empty is also not a plan
    with pytest.raises(Unplannable):
        create(world)
    assert counts(world["db"])["plans"] == 0


def test_unsupported_requirements_are_kept_alongside_a_feasible_plan(world):
    world["provider"].reply = lambda request: reply(task("t1", "research"), unsupported=[{"requirement": "Post on LinkedIn", "reason": "No agent can post."}])
    plan = create(world)
    assert plan.current.unsupported == ({"requirement": "Post on LinkedIn", "reason": "No agent can post."},)


def test_actions_no_agent_can_take_become_warnings(world):
    world["provider"].reply = lambda request: reply(task("t1", "sales", description="Send the email to every lead on the list, then report."))
    plan = create(world)
    assert [(w["code"], w["task"]) for w in plan.current.warnings] == [("possible_external_action", "t1")]


@pytest.mark.parametrize("error,code", [(ProviderError("timeout"), "timeout"), (ProviderError("rate_limited"), "rate_limited"), (ProviderError("unavailable"), "unavailable"),
                                        (ProviderError("auth"), "auth"), (ProviderError("unsupported_model"), "unsupported_model"), (ProviderError("invalid_response"), "invalid_response")])
def test_provider_failures_store_nothing_and_release_the_slot(world, error, code):
    world["provider"].error = error
    with pytest.raises(PlanningError) as info:
        create(world)
    assert info.value.code == code and len(world["provider"].requests) == 1  # one call, never retried
    assert counts(world["db"])["plans"] == 0 and world["limits"].acquire(world["sa"].user_id)


def test_malformed_model_output_stores_nothing(world):
    for text in ("I think you should hire someone.", '{"feasible": true}', "{" * 20, "```json\n[1,2]\n```"):
        world["provider"].reply = lambda request, text=text: text
        with pytest.raises(PlanningError) as info:
            create(world)
        assert info.value.code == "invalid_output"
    assert counts(world["db"])["plans"] == 0


def test_a_slow_planner_times_out(world):
    world["provider"].delay = 5
    svc = world["service"](planner=Planner(
        ProviderRegistry({"groq": lambda: world["provider"]}), PlannerConfig(timeout_seconds=0.05, deadline_grace_seconds=0.05)))
    with pytest.raises(PlanningError) as info:
        run(svc.create_plan(world["sa"], OBJECTIVE))
    assert info.value.code == "timeout" and counts(world["db"])["plans"] == 0


def test_invalid_objectives_never_reach_the_provider(world):
    for bad in ("short", "", None, "x" * 2001, "bad\x00objective here"):
        with pytest.raises(ValueError):
            create(world, objective=bad)
    assert world["provider"].requests == []


def test_the_shared_concurrency_limits_refuse_planning_too(world):
    limits = Limits(total=1, per_user=1)
    assert limits.acquire(world["sa"].user_id)
    with pytest.raises(Busy):
        run(world["service"](limits=limits).create_plan(world["sa"], OBJECTIVE))
    assert world["provider"].requests == [] and counts(world["db"])["plans"] == 0


def test_every_operation_requires_a_genuine_scope(world):
    plan = create(world)
    for bad in (None, "a@example.com", {"user_id": 1}, object()):
        for call in (lambda: run(world["svc"].create_plan(bad, OBJECTIVE)), lambda: world["svc"].store.get(bad, plan.id), lambda: world["svc"].store.list_plans(bad),
                     lambda: world["svc"].store.list_revisions(bad, plan.id), lambda: world["svc"].store.get_revision(bad, plan.id, 1),
                     lambda: world["svc"].validate(bad, plan.id), lambda: world["svc"].approve(bad, plan.id, 1), lambda: world["svc"].store.cancel(bad, plan.id),
                     lambda: world["svc"].revise(bad, plan.id, 1, None, revised(task("a", "research")))):
            with pytest.raises(ScopeError):
                call()


# ------------------------------------------------------------------------------------------------ ownership

def test_plans_are_isolated_between_users_and_workspaces(world):
    mine = create(world)
    theirs = create(world, scope=world["sb"], objective="Hire our first two engineers by the end of March.")
    assert [p.id for p, _ in world["svc"].store.list_plans(world["sa"])["items"]] == [mine.id]
    assert [p.id for p, _ in world["svc"].store.list_plans(world["sb"])["items"]] == [theirs.id]
    for call in (lambda: world["svc"].store.get(world["sa"], theirs.id), lambda: world["svc"].store.list_revisions(world["sa"], theirs.id),
                 lambda: world["svc"].store.get_revision(world["sa"], theirs.id, 1), lambda: world["svc"].validate(world["sa"], theirs.id),
                 lambda: world["svc"].approve(world["sa"], theirs.id, 1), lambda: world["svc"].store.cancel(world["sa"], theirs.id),
                 lambda: world["svc"].revise(world["sa"], theirs.id, 1, None, revised(task("a", "research"))), lambda: world["svc"].store.get(world["sa"], uuid.uuid4())):
        with pytest.raises(PlanNotFound):  # foreign and unknown plans are indistinguishable
            call()
    assert world["svc"].store.get(world["sb"], theirs.id).status == "draft" and world["svc"].store.get(world["sb"], theirs.id).objective.startswith("Hire")


def test_plans_are_shared_for_reading_inside_a_workspace_writable_only_by_their_creator_and_invisible_across_workspaces(world):
    """Sprint 5 (replaces the earlier "private inside a shared workspace" rule): every member of a workspace can READ the workspace's plans and sees who created them; only the creator can change, approve or cancel
    a plan; a different workspace (including the same person's personal one) sees nothing."""
    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    owner = authorize_memory_scope(world["sf"], world["a"], team)
    member = authorize_memory_scope(world["sf"], world["b"], team)
    personal = create(world)
    team_plan = create(world, scope=owner, objective="Plan the quarterly planning offsite for the team.")
    assert [p.id for p, _ in world["svc"].store.list_plans(owner)["items"]] == [team_plan.id]
    assert [p.id for p, _ in world["svc"].store.list_plans(world["sa"])["items"]] == [personal.id]  # the personal workspace is a different workspace
    assert [p.id for p, _ in world["svc"].store.list_plans(member)["items"]] == [team_plan.id]  # the teammate READS it ...
    seen = world["svc"].store.get(member, team_plan.id)
    assert seen.id == team_plan.id and seen.created_by  # ... and sees who created it (an e-mail, never an id)
    assert set(world["svc"].store.list_plans(member)["creators"].values()) == {seen.created_by}
    with pytest.raises(PlanNotFound):  # ... but cannot change, approve or cancel it
        world["svc"].store.approve(member, team_plan.id, 1)
    with pytest.raises(PlanNotFound):
        world["svc"].store.cancel(member, team_plan.id)
    with pytest.raises(PlanNotFound):
        world["svc"].store.get(world["sa"], team_plan.id)  # the same person's PERSONAL workspace sees nothing of the team's plans


def test_a_non_creator_is_told_not_found_before_the_plan_is_validated_for_them(world, monkeypatch):
    """Found by the browser acceptance run: approve() validated the plan under the CALLER's agent catalogue first, so a teammate (or the owner) approving somebody else's plan got a
    409 plan_invalid about agents they do not have, instead of the 404 every other write gets. Ownership is now checked first; the creator still gets the validation answer."""
    from agentplatform.plans import PlanInvalid

    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    owner = authorize_memory_scope(world["sf"], world["a"], team)
    member = authorize_memory_scope(world["sf"], world["b"], team)
    plan = create(world, scope=owner, objective="Plan the quarterly planning offsite for the team.")

    def failing_validation(scope, plan_id):
        return {"revision": 1, "errors": [{"code": "agent_unavailable", "task": "a", "message": "not yours"}], "warnings": [], "status": "draft"}

    monkeypatch.setattr(world["svc"], "validate", failing_validation)
    with pytest.raises(PlanNotFound):
        world["svc"].approve(member, plan.id, 1)  # not the creator: not found, and nothing about validation leaks
    with pytest.raises(PlanInvalid):
        world["svc"].approve(owner, plan.id, 1)  # the creator still hears why it cannot be approved


# ------------------------------------------------------------------------------------------------ retrieval, listing

def test_retrieval_returns_tasks_dependencies_and_levels_in_a_stable_order(world):
    world["provider"].reply = lambda request: reply(task("a", "research"), task("b", "sales"), task("c", "ceo", ["a", "b"]), task("d", "finance", ["c"]))
    plan = create(world)
    got = world["svc"].store.get(world["sa"], plan.id)
    assert got == plan and got.current.levels == (("a", "b"), ("c",), ("d",))
    assert [(t.key, t.depends_on) for t in got.current.tasks] == [("a", ()), ("b", ()), ("c", ("a", "b")), ("d", ("c",))]
    assert world["svc"].store.get_revision(world["sa"], plan.id, 1) == got.current
    with pytest.raises(RevisionNotFound):
        world["svc"].store.get_revision(world["sa"], plan.id, 2)


def test_listing_is_paged_newest_first_filtered_and_validated(world):
    ids = [create(world, objective=f"Launch campaign number {i} for the product.").id for i in range(7)]
    seen, cursor = [], None
    while True:
        page = world["svc"].store.list_plans(world["sa"], limit=3, cursor=cursor)
        seen += [p.id for p, _ in page["items"]]
        assert all(count == 3 for _, count in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1] and len(set(seen)) == 7
    world["svc"].store.cancel(world["sa"], ids[0])
    assert len(world["svc"].store.list_plans(world["sa"])["items"]) == 6  # "active" hides cancelled plans
    assert [p.id for p, _ in world["svc"].store.list_plans(world["sa"], status="cancelled")["items"]] == [ids[0]]
    assert len(world["svc"].store.list_plans(world["sa"], status="all", limit=100)["items"]) == 7
    for bad_limit in (0, 101, "5", True):
        with pytest.raises(ValueError):
            world["svc"].store.list_plans(world["sa"], limit=bad_limit)
    with pytest.raises(ValueError):
        world["svc"].store.list_plans(world["sa"], status="deleted")
    for bad_cursor in ("garbage", "v1.x.y", ""):
        with pytest.raises(CursorError):
            world["svc"].store.list_plans(world["sa"], cursor=bad_cursor)


# ------------------------------------------------------------------------------------------------ revisions

def snapshot(db):
    return {t: db.rows(f"SELECT * FROM {t} ORDER BY 1, 2") for t in ("plan_revisions", "plan_tasks", "plan_task_dependencies")}


def test_a_revision_is_a_new_immutable_revision_and_never_touches_the_old_one(world):
    plan = create(world)
    before = snapshot(world["db"])
    new = world["svc"].revise(world["sa"], plan.id, 1, "A narrower interpretation of the objective.",
                              revised(task("research", "research"), task("write", "marketing", ["research"], title="Write the announcement")))
    assert (new.latest_revision, new.status, new.current.revision, new.current.origin) == (2, "draft", 2, "revised")
    assert [t.key for t in new.current.tasks] == ["research", "write"] and new.current.interpretation.startswith("A narrower")
    after = snapshot(world["db"])
    for table, rows in before.items():
        assert after[table][:len(rows)] == rows or all(r in after[table] for r in rows)  # every old row is still there, unchanged
    old = world["svc"].store.get_revision(world["sa"], plan.id, 1)
    assert [t.key for t in old.tasks] == ["research", "draft", "review"] and old.origin == "model"
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [2, 1]
    assert counts(world["db"])["agent_executions"] == 0 and len(world["provider"].requests) == 1  # revising calls no model, runs nothing


def test_a_revision_is_validated_exactly_like_a_model_proposal(world):
    plan = create(world)
    archived = world["agents"].create(world["sa"], "old-agent", definition(name="Old Agent"))
    world["agents"].archive(world["sa"], archived.id)
    for tasks, code in (([task("a", "research", ["b"]), task("b", "sales", ["a"])], "cycle"), ([task("a", "research"), task("a", "sales")], "duplicate_task_id"),
                        ([task("a", "research", ["a"])], "self_dependency"), ([task("a", "research", ["zzz"])], "missing_dependency"),
                        ([task("a", "old-agent")], "agent_unavailable"), ([task("a", "nobody")], "agent_unavailable")):
        with pytest.raises(PlanRejected) as info:
            world["svc"].revise(world["sa"], plan.id, 1, None, revised(*tasks))
        assert code in {e["code"] for e in info.value.errors}, tasks
    assert world["svc"].store.get(world["sa"], plan.id).latest_revision == 1 and counts(world["db"])["plan_revisions"] == 1


def test_stale_revisions_are_refused_not_merged(world):
    plan = create(world)
    world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))
    with pytest.raises(PlanConflict) as info:
        world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("b", "sales")))
    assert info.value.latest_revision == 2
    with pytest.raises(PlanConflict):
        world["svc"].revise(world["sa"], plan.id, 7, None, revised(task("b", "sales")))
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [2, 1]


def test_revisions_keep_an_unchanged_tasks_agent_version_and_warn_about_newer_ones(world):
    agent = world["agents"].create(world["sa"], "release-notes", definition())
    world["provider"].reply = lambda request: reply(task("t1", "research"), task("t2", "release-notes", ["t1"]))
    plan = create(world)
    world["agents"].update(world["sa"], agent.id, 1, definition(role="Second role of the agent"))
    new = world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("t1", "research"), task("t2", "release-notes", ["t1"]), task("t3", "release-notes", ["t2"])))
    versions = {t.key: t.agent_version for t in new.current.tasks}
    assert versions == {"t1": None, "t2": 1, "t3": 2}  # t2 keeps what was selected; the new t3 pins the current version
    assert [(w["code"], w["task"]) for w in new.current.warnings] == [("newer_version_available", "t2")]


def test_concurrent_revisions_from_one_base_have_exactly_one_winner(world):
    plan = create(world)
    outcomes, barrier = [], threading.Barrier(8)

    def revise(i):
        barrier.wait()
        try:
            world["svc"].revise(world["sa"], plan.id, 1, None, revised(task(f"t{i}", "research")))
            outcomes.append("won")
        except PlanConflict as exc:
            outcomes.append(("conflict", exc.latest_revision))

    threads = [threading.Thread(target=revise, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("won") == 1 and len(outcomes) == 8 and all(o[1] >= 2 for o in outcomes if o != "won")
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [2, 1]  # no gap, no duplicate, no orphan rows
    assert counts(world["db"])["plan_revisions"] == 2 and world["db"].scalar("SELECT latest_revision FROM plans") == 2
    assert counts(world["db"])["plan_tasks"] == 3 + 1  # the three original tasks plus the winner's one; losers left nothing


# ------------------------------------------------------------------------------------------------ approval and lifecycle

def test_approval_pins_a_specific_revision_and_never_executes(world):
    plan = create(world)
    approved = world["svc"].approve(world["sa"], plan.id, 1)
    assert (approved.status, approved.approved_revision, approved.latest_revision) == ("approved", 1, 1) and approved.approved_at
    assert world["svc"].approve(world["sa"], plan.id, 1).approved_at == approved.approved_at  # idempotent
    with pytest.raises(PlanConflict) as info:
        world["svc"].approve(world["sa"], plan.id, 5)
    assert info.value.latest_revision == 1
    assert counts(world["db"])["agent_executions"] == 0 and len(world["provider"].requests) == 1


def test_revising_an_approved_plan_withdraws_the_approval_but_keeps_the_history(world):
    plan = create(world)
    world["svc"].approve(world["sa"], plan.id, 1)
    new = world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))
    assert (new.status, new.approved_revision, new.approved_at, new.latest_revision) == ("draft", None, None, 2)
    with pytest.raises(PlanConflict):
        world["svc"].approve(world["sa"], plan.id, 1)  # the old revision is no longer the latest
    assert world["svc"].approve(world["sa"], plan.id, 2).approved_revision == 2
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [2, 1]  # the earlier approved revision is still readable


def test_approval_revalidates_against_the_agents_available_now(world):
    agent = world["agents"].create(world["sa"], "release-notes", definition())
    world["provider"].reply = lambda request: reply(task("t1", "release-notes"), task("t2", "sales", ["t1"]), task("t3", "research"))
    plan = create(world)
    report = world["svc"].validate(world["sa"], plan.id)
    assert report["valid"] and report["errors"] == [] and report["levels"] == [["t1", "t3"], ["t2"]]
    world["agents"].archive(world["sa"], agent.id)
    report = world["svc"].validate(world["sa"], plan.id)
    assert not report["valid"] and {(e["code"], e["task"]) for e in report["errors"]} == {("agent_unavailable", "t1"), ("dependency_unsatisfiable", "t2")}
    with pytest.raises(PlanInvalid) as info:
        world["svc"].approve(world["sa"], plan.id, 1)
    assert {e["code"] for e in info.value.errors} == {"agent_unavailable", "dependency_unsatisfiable"}
    assert world["svc"].store.get(world["sa"], plan.id).status == "draft"  # nothing changed
    world["agents"].unarchive(world["sa"], agent.id)
    assert world["svc"].approve(world["sa"], plan.id, 1).status == "approved"


def test_a_newer_agent_version_does_not_block_approval(world):
    agent = world["agents"].create(world["sa"], "release-notes", definition())
    world["provider"].reply = lambda request: reply(task("t1", "release-notes"))
    plan = create(world)
    world["agents"].update(world["sa"], agent.id, 1, definition(role="Another role entirely"))
    assert world["svc"].approve(world["sa"], plan.id, 1).current.tasks[0].agent_version == 1


def test_validation_is_read_only(world):
    plan = create(world)
    before = (snapshot(world["db"]), world["db"].rows("SELECT * FROM plans"))
    for _ in range(3):
        world["svc"].validate(world["sa"], plan.id)
    assert (snapshot(world["db"]), world["db"].rows("SELECT * FROM plans")) == before


def test_cancelling_a_plan(world):
    plan = create(world)
    world["svc"].approve(world["sa"], plan.id, 1)
    cancelled = world["svc"].store.cancel(world["sa"], plan.id)
    assert (cancelled.status, cancelled.approved_revision, cancelled.approved_at) == ("cancelled", None, None)
    assert world["svc"].store.cancel(world["sa"], plan.id).status == "cancelled"  # idempotent
    with pytest.raises(PlanCancelled):
        world["svc"].approve(world["sa"], plan.id, 1)
    with pytest.raises(PlanCancelled):
        world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))
    assert world["svc"].validate(world["sa"], plan.id)["errors"][0]["code"] == "plan_cancelled"
    assert world["svc"].store.get(world["sa"], plan.id).current.tasks  # the content is still readable


# ------------------------------------------------------------------------------------------------ guards and limits

def test_revisions_tasks_and_dependencies_are_insert_only_in_the_database_itself(world):
    plan = create(world)
    for statement in ("UPDATE plan_revisions SET interpretation = 'tampered'", "DELETE FROM plan_revisions", "UPDATE plan_tasks SET title = 'tampered'",
                      "UPDATE plan_tasks SET agent_key = 'sales'", "DELETE FROM plan_tasks", "UPDATE plan_task_dependencies SET depends_on_key = 'draft'",
                      "DELETE FROM plan_task_dependencies"):
        with pytest.raises(Exception) as info:
            world["db"].run(statement)
        assert "insert-only" in str(info.value), statement
    assert world["svc"].store.get(world["sa"], plan.id) == plan
    world["db"].run("UPDATE plans SET updated_at = '2026-02-02 00:00:00.000000'")  # only the header is mutable


def test_safety_limits(world, monkeypatch):
    monkeypatch.setattr(plans_module, "MAX_PLANS_PER_WORKSPACE", 2)
    create(world)
    create(world)
    with pytest.raises(PlanLimitReached) as info:
        create(world)
    assert info.value.what == "plans"
    create(world, scope=world["sb"])  # per workspace
    monkeypatch.setattr(plans_module, "MAX_REVISIONS_PER_PLAN", 2)
    plan = world["svc"].store.list_plans(world["sa"])["items"][0][0]
    world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))
    with pytest.raises(PlanLimitReached) as info:
        world["svc"].revise(world["sa"], plan.id, 2, None, revised(task("b", "research")))
    assert info.value.what == "revisions"


def test_approval_racing_a_revision_never_leaves_a_stale_approval(world):
    for round_ in range(6):
        plan = create(world)
        barrier, errors = threading.Barrier(2), []

        def approve():
            barrier.wait()
            try:
                world["svc"].approve(world["sa"], plan.id, 1)
            except (PlanConflict, PlanCancelled) as exc:
                errors.append(exc)

        def revise():
            barrier.wait()
            world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))

        threads = [threading.Thread(target=approve), threading.Thread(target=revise)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        final = world["svc"].store.get(world["sa"], plan.id)
        assert final.latest_revision == 2 and final.status == "draft" and final.approved_revision is None, round_  # approval of revision 1 is always withdrawn


# ------------------------------------------------------------------------------------------------ deterministic interleavings (CAS review)

def interleave(world, monkeypatch, method, action):
    """Run ``action`` (another committed operation) inside store.<method>, right AFTER it has read the plan row and BEFORE it
    writes: the exact window in which a concurrent writer can invalidate that read. The service's own earlier reads are untouched."""
    store = type(world["svc"].store)
    state, read, wrapped = {"armed": False, "fired": False}, store._plan, getattr(store, method)

    def patched_plan(self, session, scope, plan_id, write=False):
        plan = read(self, session, scope, plan_id, write)
        if state["armed"] and not state["fired"]:
            state["fired"] = True
            action()
        return plan

    def patched_method(self, *args, **kwargs):
        state["armed"] = True
        try:
            return wrapped(self, *args, **kwargs)
        finally:
            state["armed"] = False
            assert state["fired"], "the interleaving never happened"

    monkeypatch.setattr(store, "_plan", patched_plan)
    monkeypatch.setattr(store, method, patched_method)


def header(db):
    return db.rows("SELECT status, latest_revision, approved_revision, approved_at IS NULL FROM plans")


def test_a_revision_that_lost_the_race_leaves_no_trace_and_cannot_move_the_pointer(world, monkeypatch):
    plan = create(world)
    interleave(world, monkeypatch, "add_revision", lambda: world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("winner", "research"))))
    with pytest.raises(PlanConflict) as info:
        world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("loser", "sales")))
    assert info.value.latest_revision == 2
    assert header(world["db"]) == [("draft", 2, None, 1)]
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [2, 1]
    keys = [r[0] for r in world["db"].rows("SELECT task_key FROM plan_tasks")]
    assert "loser" not in keys and keys.count("winner") == 1 and len(keys) == 3 + 1  # no orphan task/dependency rows from the loser
    assert world["db"].scalar("SELECT count(*) FROM plan_task_dependencies") == 2


def test_a_revision_racing_a_cancel_cannot_resurrect_the_plan(world, monkeypatch):
    plan = create(world)
    interleave(world, monkeypatch, "add_revision", lambda: world["svc"].store.cancel(world["sa"], plan.id))
    with pytest.raises(PlanConflict):
        world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research")))
    assert header(world["db"]) == [("cancelled", 1, None, 1)]
    assert [r[0] for r in world["svc"].store.list_revisions(world["sa"], plan.id)] == [1]  # the half-written revision rolled back


def test_an_approval_that_lost_to_a_committed_revision_is_refused_not_stale(world, monkeypatch):
    plan = create(world)
    interleave(world, monkeypatch, "approve", lambda: world["svc"].revise(world["sa"], plan.id, 1, None, revised(task("a", "research"))))
    with pytest.raises(PlanConflict):
        world["svc"].approve(world["sa"], plan.id, 1)
    # the plan must NOT be 'approved' with approved_revision=1 while revision 2 is the latest
    assert header(world["db"]) == [("draft", 2, None, 1)]
    assert world["svc"].approve(world["sa"], plan.id, 2).approved_revision == 2


def test_an_approval_racing_a_cancel_cannot_approve_a_cancelled_plan(world, monkeypatch):
    plan = create(world)
    interleave(world, monkeypatch, "approve", lambda: world["svc"].store.cancel(world["sa"], plan.id))
    with pytest.raises(PlanConflict):
        world["svc"].approve(world["sa"], plan.id, 1)
    assert header(world["db"]) == [("cancelled", 1, None, 1)]


def test_approved_revision_always_names_the_latest_complete_revision(world):
    plan = create(world)
    world["svc"].approve(world["sa"], plan.id, 1)
    for n in (2, 3):
        world["svc"].revise(world["sa"], plan.id, n - 1, None, revised(task(f"a{n}", "research")))
        world["svc"].approve(world["sa"], plan.id, n)
        status, latest, approved = world["db"].rows("SELECT status, latest_revision, approved_revision FROM plans")[0]
        assert (status, latest, approved) == ("approved", n, n)
        tasks = world["db"].scalar(f"SELECT count(*) FROM plan_tasks t JOIN plan_revisions r ON r.id = t.revision_id WHERE r.revision = {approved}")
        assert tasks == 1  # the approved revision is complete: its tasks exist
