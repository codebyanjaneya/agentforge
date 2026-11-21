"""Recording and reading model usage (M6). See ``pricing.py`` for the honesty rules.

Recording is BEST EFFORT by design: an accounting failure is logged (by exception type only) and never fails or delays an execution or a plan. Reading is scoped to the caller's user and
workspace (a foreign run is indistinguishable from an unknown one)."""

import observability
import logging
import uuid
from datetime import datetime

from sqlalchemy import func, select

from agentplatform import pricing
from authz.scope import require_scope
from memory.models import ModelUsage, PlanRun

logger = logging.getLogger("agentforge.usage")


class UsageRecorder:
    def __init__(self, session_factory, *, clock=datetime.utcnow):
        self._sessions, self._now = session_factory, clock

    def record(self, scope, *, purpose: str, provider: str, model: str, usage, plan_id=None, run_id=None, task_key=None, execution_id=None, agent_key=None) -> None:
        try:
            scope = require_scope(scope)
            tokens_in, tokens_out = getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)
            cached = getattr(usage, "cached_tokens", None)
            known = type(tokens_in) is int and type(tokens_out) is int
            cost, basis = pricing.cost_micros(provider, model, tokens_in, tokens_out)
            row = ModelUsage(user_id=scope.user_id, workspace_id=scope.workspace_id, plan_id=plan_id, run_id=run_id, task_key=task_key, execution_id=execution_id,
                             agent_key=agent_key, purpose=purpose, provider=str(provider)[:32], model=str(model)[:100], input_tokens=tokens_in if known else None,
                             output_tokens=tokens_out if known else None, cached_tokens=cached if type(cached) is int and cached >= 0 else None, usage_known=known,
                             pricing_version=pricing.PRICING_VERSION if basis == "calculated" else None, cost_micros=cost, cost_basis=basis, created_at=self._now())
            with self._sessions() as session, session.begin():
                session.add(row)
            if known:
                observability.inc("model_tokens_total", tokens_in, provider=str(provider)[:32], model=str(model)[:60], direction="input")
                observability.inc("model_tokens_total", tokens_out, provider=str(provider)[:32], model=str(model)[:60], direction="output")
            if basis == "calculated":
                observability.inc("model_cost_micros_total", cost, provider=str(provider)[:32], model=str(model)[:60])
        except Exception as exc:  # noqa: BLE001 - accounting must never break the work
            logger.error("Could not record model usage (%s)", type(exc).__name__)


def _summary(rows) -> dict:
    requests, unknown = len(rows), sum(1 for r in rows if not r.usage_known)
    cost_rows = [r for r in rows if r.cost_basis == "calculated"]
    return {
        "requests": requests,
        "input_tokens": sum(r.input_tokens or 0 for r in rows), "output_tokens": sum(r.output_tokens or 0 for r in rows), "cached_tokens": sum(r.cached_tokens or 0 for r in rows),
        "requests_without_usage": unknown,
        "cost_micros": sum(r.cost_micros for r in cost_rows) if cost_rows else None,
        "cost_complete": requests > 0 and len(cost_rows) == requests,  # True only when EVERY request has a calculated cost
        "requests_without_price": sum(1 for r in rows if r.cost_basis == "unknown_pricing"),
    }


def run_usage(session_factory, scope, run_id) -> dict:
    """Usage of one run: its agent requests (by task and by model) and the planning requests of the plan it executes, from persisted rows only. Raises LookupError for an unknown run."""
    scope = require_scope(scope)
    with session_factory() as session:
        run = session.execute(select(PlanRun).where(PlanRun.id == run_id, PlanRun.workspace_id == scope.workspace_id)).scalar_one_or_none()
        if run is None:
            raise LookupError("not found")
        mine = [ModelUsage.workspace_id == scope.workspace_id]  # sprint 5: the run's cost is visible to every member of the workspace (the usage rows belong to the run, whoever started it)
        agent = session.execute(select(ModelUsage).where(*mine, ModelUsage.run_id == run_id, ModelUsage.purpose == "agent").order_by(ModelUsage.created_at)).scalars().all()
        planning = session.execute(select(ModelUsage).where(*mine, ModelUsage.plan_id == run.plan_id, ModelUsage.purpose == "plan").order_by(ModelUsage.created_at)).scalars().all()
        every = [*planning, *agent]

        def group(key):
            out = {}
            for r in agent:
                out.setdefault(key(r), []).append(r)
            return out

        return {
            "totals": _summary(every), "planning": _summary(planning) if planning else None, "agents": _summary(agent) if agent else None,
            "by_model": [{"provider": p, "model": m, **_summary([r for r in every if (r.provider, r.model) == (p, m)])} for p, m in sorted({(r.provider, r.model) for r in every})],
            "by_task": [{"task": t, "agent": rows[0].agent_key, **_summary(rows)} for t, rows in sorted(group(lambda r: r.task_key).items(), key=lambda kv: str(kv[0]))],
            "pricing_versions": sorted({r.pricing_version for r in every if r.pricing_version}),
        }
