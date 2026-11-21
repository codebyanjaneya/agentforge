"""M6b / Priority 2: a task that requires a specific action declares it STRUCTURALLY (``required_tool``). The server resolves and validates it, forces one correction when the model only
talks, pauses the sensitive call for the exact-action approval, and refuses to complete the task unless the tool was REALLY invoked. Prose promising an action never counts."""

import asyncio
import time

import pytest

from agentplatform.planning import PlanRejected
from agentplatform.tools.fixtures import OUTBOX
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_approvals import ARGS, plan_stack, reg  # noqa: F401  (reg is a fixture)
from tests.test_plan_runs import definition, make_plan, task, world  # noqa: F401  (world is a fixture)
from tests.test_tools import Scripted, asks, call, final

WIRE = "demo__send_note__v1"
REF = "demo.send_note@1"


def setup(world, provider, reg, **task_extra):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    new = plan_stack(world, reg, provider)
    plan_id = make_plan(world, [task("a", "notifier", required_tool=REF, **task_extra)])
    return new, plan_id


def drive(world, reg, provider, decision=None):
    """Start the plan; if the task pauses for approval, decide as given and let a SECOND independent process resume it. Returns the final run view."""
    new, plan_id = setup(world, provider, reg)
    first_store, first = new()

    async def phase_one():
        started = await first.start(world["sa"], plan_id)
        deadline = time.time() + 20
        while time.time() < deadline:
            if first_store.list(world["sa"], status="pending") or world["db"].scalar("SELECT status FROM plan_runs") != "running":
                break
            await asyncio.sleep(0.05)
        await first.registry.wait(started.id, 30)
        return started.id

    run_id = asyncio.run(phase_one())
    if decision is None:
        return first.store.get(world["sa"], run_id)
    second_store, second = new()
    pending = second_store.list(world["sa"], status="pending")[0]
    second_store.decide(world["sa"], pending.id, decision, submitted_hash=pending.payload_hash)

    async def phase_two():
        await second.claim_work(resume=True)
        await second.registry.wait(run_id, 30)

    asyncio.run(phase_two())
    return second.store.get(world["sa"], run_id)


def codes(world):
    return world["db"].rows("SELECT task_key, status, error_code FROM plan_task_runs")


# ------------------------------------------------------------------------------------------------ the contract is resolved and validated server-side

def test_the_required_tool_is_resolved_pinned_and_checked_against_the_agents_grants(world, reg):
    world["agents"].create(world["sa"], "notifier", definition(allowed_tools=["demo.send_note"]))
    world["agents"].create(world["sa"], "writer", definition())  # no tools granted
    plan_id = make_plan(world, [task("a", "notifier", required_tool="demo.send_note")], approve=False)  # an unversioned reference is pinned by the SERVER
    assert world["db"].rows("SELECT task_key, required_tool FROM plan_tasks") == [("a", REF)]
    for ref, agent, code in (("demo.send_note@1", "writer", "required_tool_not_granted"), ("nope.nothing@1", "notifier", "required_tool_unknown"),
                             ("demo.send_note@9", "notifier", "required_tool_unknown"), ("math.calculate@1", "notifier", "required_tool_not_granted")):
        with pytest.raises(PlanRejected) as rejected:
            make_plan(world, [task("a", agent, required_tool=ref)], approve=False)
        assert [e["code"] for e in rejected.value.errors] == [code], (ref, agent)
    assert world["db"].scalar("SELECT count(*) FROM plans") == 1  # a rejected plan is never stored


# ------------------------------------------------------------------------------------------------ prose is not an action

def test_a_model_that_only_describes_the_action_does_not_satisfy_the_task_even_after_a_forced_correction(world, reg):
    provider = Scripted(final("I have sent the note to the customer."), final("Yes, the note was sent. Nothing more to do."))
    view = drive(world, reg, provider)
    assert view.status == "failed" and codes(world) == [("a", "failed", "required_action_missing")]
    assert OUTBOX == [] and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0 and world["db"].scalar("SELECT count(*) FROM tool_approvals") == 0
    assert len(provider.requests) == 2 and provider.requests[0].tool_choice is None and provider.requests[1].tool_choice == WIRE  # exactly ONE correction, forcing the tool
    assert "required action has not been performed" in provider.requests[1].messages[-1].content
    assert world["db"].scalar("SELECT error_message FROM plan_task_runs").startswith("The agent did not perform the action")


def test_prose_first_then_a_structured_invocation_after_the_correction_pauses_and_completes_exactly_once(world, reg):
    provider = Scripted(final("I will send it now."), asks(call(WIRE, ARGS)), final("The note was sent (simulated)."))
    view = drive(world, reg, provider, "approve")
    assert view.status == "succeeded" and codes(world) == [("a", "succeeded", None)]
    assert len(OUTBOX) == 1 and OUTBOX[0][0] == "customer@example.com"
    assert world["db"].rows("SELECT status FROM tool_approvals") == [("consumed",)] and world["db"].rows("SELECT status FROM tool_invocations") == [("succeeded",)]
    assert provider.requests[1].tool_choice == WIRE


# ------------------------------------------------------------------------------------------------ the structured invocation: pause, approve, resume the exact call

def test_the_structured_invocation_pauses_before_dispatch_and_only_the_approved_call_runs(world, reg):
    provider = Scripted(asks(call(WIRE, ARGS)), final("The note was sent (simulated)."))
    paused = drive(world, reg, provider)  # no decision: the run stays suspended
    assert paused.status == "running" and OUTBOX == [] and world["db"].scalar("SELECT status FROM tool_approvals") == "pending"
    assert world["db"].rows("SELECT task_key, resumed_at IS NULL FROM tool_suspensions") == [("a", 1)]


def test_an_approved_required_action_completes_the_task_and_a_second_resume_dispatches_nothing_more(world, reg):
    provider = Scripted(asks(call(WIRE, ARGS)), final("The note was sent (simulated)."))
    view = drive(world, reg, provider, "approve")
    assert view.status == "succeeded" and len(OUTBOX) == 1 and len(provider.requests) == 2
    assert provider.requests[0].tool_choice is None  # the model needed no correction


def test_a_rejected_required_action_never_dispatches_and_the_task_is_not_reported_complete(world, reg):
    provider = Scripted(asks(call(WIRE, ARGS)), final("Understood, I did not send it."))
    view = drive(world, reg, provider, "reject")
    assert view.status == "failed" and codes(world) == [("a", "failed", "required_action_missing")]
    assert OUTBOX == [] and world["db"].rows("SELECT status FROM tool_approvals") == [("rejected",)]
    assert world["db"].rows("SELECT status FROM tool_invocations") == [("rejected",)]  # recorded as refused; nothing ran


# ------------------------------------------------------------------------------------------------ a model cannot grant itself a tool

def test_a_call_to_a_tool_the_agent_was_not_given_is_refused_and_does_not_satisfy_the_requirement(world, reg):
    provider = Scripted(asks(call("math__calculate__v1", {"expression": "1+1"})), final("Done."), final("Done again."))
    view = drive(world, reg, provider)
    assert view.status == "failed" and codes(world) == [("a", "failed", "required_action_missing")]
    assert OUTBOX == [] and set(r[0] for r in world["db"].rows("SELECT status FROM tool_invocations")) == {"rejected"}
