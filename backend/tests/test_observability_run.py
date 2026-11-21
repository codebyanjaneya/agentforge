"""Sprint 2, P3: the real instrumentation points fire on a real run (kept apart from the API fixtures, which purge modules)."""

import asyncio

import observability as ob
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_durable_dispatch import ARGS, WIRE, _run, plan, process, rig, script, world  # noqa: F401
from tests.test_tools import asks, call, final


def test_a_real_run_with_an_unknown_outcome_moves_the_operational_metrics(world, rig):
    ob.reset()
    remote, registry = rig(timeout=0.3)
    remote.mode = "error_after_accept"
    _, svc = process(world, registry, script([asks(call(WIRE, ARGS)), final("Sent.")]))
    asyncio.run(_run(world, svc, plan(world, required_tool="remote.send@1")))
    text = ob.render()
    assert 'agentforge_runs_finished_total{code="none",status="failed"} 1' in text
    assert 'agentforge_tasks_finished_total{code="side_effect_outcome_unknown",status="failed"} 1' in text
    assert 'agentforge_dispatch_unknown_total{source="finish"} 1' in text
    assert 'agentforge_tool_invocations_total{code=' in text
    assert "user" not in text.lower().replace("user_", "")  # no identity anywhere in the exposition
