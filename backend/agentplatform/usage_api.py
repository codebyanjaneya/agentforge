"""REST route for the model usage and cost of one run (M6). Read-only, scoped through the run itself (a foreign run is an unknown one: 404). Persisted rows only."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException

from agentplatform import pricing
from agentplatform.usage import run_usage
from sqlalchemy import select

from auth.dependencies import get_session_factory
from authz.dependencies import get_scope
from memory.models import PlanRun, ProviderEvent

router = APIRouter()


@router.get("/plan-runs/{run_id}/usage")
async def get_run_usage(run_id: uuid.UUID, scope=Depends(get_scope), session_factory=Depends(get_session_factory)):
    try:
        data = await asyncio.to_thread(run_usage, session_factory, scope, run_id)
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None
    return {**data, "pricing": {"current_version": pricing.PRICING_VERSION, "unit": "micro-USD (1e-6 USD)",
                                "note": "Costs are calculated from provider-reported tokens with AgentForge's configured, versioned prices; they are not invoices."}}


STATE = {"waiting": "waiting", "retrying": "retrying", "recovered": "recovered", "exhausted": "exhausted", "failed": "failed"}


@router.get("/plan-runs/{run_id}/provider-events")
async def get_provider_events(run_id: uuid.UUID, scope=Depends(get_scope), session_factory=Depends(get_session_factory)):
    """What happened while this run's provider requests were retried (M6b), oldest first, and the CURRENT state per task (the latest event). Fixed codes only: no prompt, answer or credential."""
    def load():
        with session_factory() as session:
            run = session.execute(select(PlanRun.id).where(PlanRun.id == run_id, PlanRun.workspace_id == scope.workspace_id)).first()  # read: any member of the workspace
            if run is None:
                raise LookupError("not found")
            rows = session.execute(select(ProviderEvent).where(ProviderEvent.run_id == run_id, ProviderEvent.workspace_id == scope.workspace_id)
                                   .order_by(ProviderEvent.created_at, ProviderEvent.id).limit(500)).scalars().all()
            return [{"task": r.task_key, "kind": r.kind, "attempt": r.attempt, "error_code": r.error_code, "wait_ms": r.wait_ms, "detail": r.detail, "provider": r.provider,
                     "at": r.created_at.isoformat()} for r in rows]

    try:
        items = await asyncio.to_thread(load)
    except LookupError:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None
    latest: dict = {}
    for item in items:
        latest[item["task"]] = item
    return {"items": items, "by_task": {task: {"state": STATE[e["kind"]], "attempt": e["attempt"], "error_code": e["error_code"], "wait_ms": e["wait_ms"]} for task, e in latest.items() if task}}
