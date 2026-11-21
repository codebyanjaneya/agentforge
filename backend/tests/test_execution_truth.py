"""Execution truth (sprint 2, P0): a task is reported successful only when the server's own records prove its external actions. A model's prose never substitutes for evidence.

Contract (D069): intent is derived from the structural ``required_tool`` (D064): none -> a reasoning task (never needs a tool); a tool whose ``side_effect`` is ``none`` -> tool-required; a tool with a side effect ->
side-effect-required. Independently of intent, ANY side-effecting dispatch of the execution that is unproven (unknown or still in flight) or confirmedly failed blocks success. The check is made by the server in the
tool loop, before the task can complete. Approval pending / rejected / refused cases are covered by tests/test_required_action.py (they were already enforced there)."""

import asyncio

import pytest

from agentplatform.plan_runs import ReconciliationRequired
from tests.test_durable_dispatch import (ARGS, WIRE, SEND_THEN_DONE, _run, plan, process, retry, rig, script, states, invocation_statuses, world)  # noqa: F401
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_tools import asks, call, final

REF = "remote.send@1"


def task_row(world):
    return world["db"].rows("SELECT status, error_code FROM plan_task_runs")[0]


@pytest.mark.parametrize("required", [False, True])
def test_a_confirmed_success_completes_the_task(world, rig, required):
    remote, registry = rig()
    _, svc = process(world, registry, script(SEND_THEN_DONE))
    view = asyncio.run(_run(world, svc, plan(world, **({"required_tool": REF} if required else {}))))
    assert view.status == "succeeded" and task_row(world) == ("succeeded", None) and states(world) == ["succeeded"] and remote.deliveries == 1


def test_a_reasoning_task_needs_no_tool_even_when_its_agent_has_one(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script([final("Here is the analysis.")]))
    view = asyncio.run(_run(world, svc, plan(world)))
    assert view.status == "succeeded" and invocation_statuses(world) == [] and remote.deliveries == 0


def test_prose_alone_never_completes_a_side_effect_required_task_and_nothing_is_sent(world, rig):
    remote, registry = rig()
    _, svc = process(world, registry, script([final("I have sent it."), final("Yes, it was sent.")]))  # the model claims success twice, including after the server's one correction
    view = asyncio.run(_run(world, svc, plan(world, required_tool=REF)))
    assert view.status == "failed" and task_row(world) == ("failed", "required_action_missing")
    assert invocation_statuses(world) == [] and remote.deliveries == 0


@pytest.mark.parametrize("required", [False, True])
def test_an_unknown_outcome_blocks_success_and_the_model_saying_sent_changes_nothing(world, rig, required):
    remote, registry = rig(timeout=0.3)
    remote.mode = "error_after_accept"  # the remote accepted, the answer was lost
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("Sent.")]))
    view = asyncio.run(_run(world, svc, plan(world, **({"required_tool": REF} if required else {}))))
    assert view.status == "failed" and task_row(world) == ("failed", "side_effect_outcome_unknown")
    assert states(world) == ["unknown"] and remote.deliveries == 1


def test_a_retry_after_an_uncertain_outcome_is_refused_and_nothing_is_sent_again(world, rig):
    remote, registry = rig(timeout=0.3)
    remote.mode = "error_after_accept"
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("Sent.")], [asks(call(WIRE, ARGS)), final("Sent.")]))
    first = asyncio.run(_run(world, svc, plan(world, required_tool=REF)))
    remote.mode = "ok"
    with pytest.raises(ReconciliationRequired):
        retry(world, svc, first.id)
    assert remote.deliveries == 1 and states(world) == ["unknown"]


@pytest.mark.parametrize("required", [False, True])
def test_a_confirmed_failure_never_becomes_success(world, rig, required):
    remote, registry = rig()
    remote.mode = "refuse"  # proven not performed
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("Sent it, all good.")]))
    view = asyncio.run(_run(world, svc, plan(world, **({"required_tool": REF} if required else {}))))
    assert view.status == "failed" and task_row(world) == ("failed", "side_effect_failed") and states(world) == ["failed"] and remote.deliveries == 0


def test_a_later_confirmed_success_of_the_same_tool_clears_an_earlier_confirmed_failure(world, rig):
    remote, registry = rig(retry_policy="after_confirmed_failure")
    remote.mode = "refuse"

    def heal():  # the remote recovers between the model's two attempts
        remote.mode = "ok"

    original = remote.send

    def send(args, dispatch=None):
        try:
            return original(args, dispatch)
        finally:
            heal()

    registry._tools[("remote.send", 1)] = (registry._tools[("remote.send", 1)][0], send)
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), asks(call(WIRE, ARGS, id="c2")), final("Sent on the second try.")]))
    view = asyncio.run(_run(world, svc, plan(world, required_tool=REF)))
    assert view.status == "succeeded" and states(world) == ["failed", "succeeded"] and remote.deliveries == 1


def test_a_retry_whose_send_is_refused_by_policy_cannot_complete_the_task_over_an_earlier_confirmed_failure(world, rig):
    """Found in a real-browser journey: attempt 2 showed Completed. The retry's send was refused (retry_policy never, no new dispatch), the model answered in prose, and a guard that looked only at the current
    execution forgot attempt 1's confirmed failure. The lineage (same plan task, every attempt) counts."""
    remote, registry = rig(retry_policy="never")
    remote.mode = "refuse"
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("could not send")], [asks(call(WIRE, ARGS)), final("Sorry, I could not send it.")]))
    first = asyncio.run(_run(world, svc, plan(world)))  # NOT a required-action task: a reasoning task whose agent chose to send
    assert first.status == "failed" and states(world) == ["failed"]
    second = retry(world, svc, first.id)
    assert second.status == "failed" and remote.deliveries == 0 and states(world) == ["failed"]  # no new dispatch, and still not a success
    assert world["db"].rows("SELECT error_code FROM plan_task_runs ORDER BY created_at DESC LIMIT 1") == [("side_effect_failed",)]
