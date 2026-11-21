"""REST routes for human approvals of tool invocations (M4-C). Every route uses ``get_scope``: another workspace's approval is indistinguishable from an
unknown one (404). A decision is made by an authenticated member of the approval's workspace; the server recomputes and checks everything, and a hash
sent by the client is only a staleness check ("approve what I was shown"). Nothing here runs a tool: a tool runs only inside the waiting execution,
after the approval is consumed in the same transaction as the invocation's intent row."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from agentplatform.tools import ToolRegistry, default_registry
from agentplatform.tools.approvals import ApprovalConflict, ApprovalForbidden, ApprovalNotFound, ApprovalStale, ApprovalStore, ApprovalView
from auth.dependencies import get_session_factory
from memory.models import Plan, PlanRun, PlanTask
from sqlalchemy import select
from authz.dependencies import get_scope
from ratelimit import limit

router = APIRouter()


class DecisionBody(BaseModel):
    """What the person was shown. Only a staleness check: the server recomputes the hash and decides everything itself. Unknown fields are refused."""

    model_config = ConfigDict(extra="forbid")
    payload_hash: str | None = Field(default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
STATUSES = ("pending", "approved", "rejected", "expired", "invalidated", "consumed")

IMPACT = {
    "none": "Changes nothing.",
    "idempotent": "Changes something. Running it again has the same effect.",
    "non_idempotent": "Changes something outside the agent's own work. Running it again could repeat the effect, and it may not be reversible.",
}


def get_registry() -> ToolRegistry:
    return default_registry()


def get_approval_store(session_factory=Depends(get_session_factory)) -> ApprovalStore:
    return ApprovalStore(session_factory)


def _iso(value):
    return value.isoformat() if value is not None else None


def run_context(session_factory, scope, views) -> dict:
    """What an approver needs to judge a request that belongs to ANOTHER member's run (M6): the objective, the task and who started it (you / another member; never an email).
    Read only through the approval's own workspace, which the caller is a verified member of; nothing else of the run is exposed."""
    ids = {v.run_id for v in views if v.run_id}
    if not ids:
        return {}
    with session_factory() as session:
        rows = session.execute(select(PlanRun.id, PlanRun.user_id, Plan.objective).join(Plan, Plan.id == PlanRun.plan_id)
                               .where(PlanRun.id.in_(ids), PlanRun.workspace_id == scope.workspace_id)).all()
        titles = {(r.id, t.task_key): t.title for r in session.execute(select(PlanRun.id, PlanRun.revision_id).where(PlanRun.id.in_(ids))).all()
                  for t in session.execute(select(PlanTask).where(PlanTask.revision_id == r.revision_id)).scalars()}
    return {rid: {"objective": objective, "started_by": "you" if uid == scope.user_id else "another member", "task_titles": {k: t for (r2, k), t in titles.items() if r2 == rid}}
            for rid, uid, objective in rows}


def approval_json(view: ApprovalView, registry: ToolRegistry, can_decide: bool = True, context: dict | None = None) -> dict:
    spec = registry.get(view.tool_id, view.tool_version)
    return {
        "id": str(view.id), "status": view.status, "execution_id": str(view.execution_id), "run_id": str(view.run_id) if view.run_id else None, "task": view.task_key,
        "agent": view.agent_key, "agent_version_id": str(view.agent_version_id) if view.agent_version_id else None, "seq": view.seq,
        "tool": {"id": view.tool_id, "version": view.tool_version, "ref": f"{view.tool_id}@{view.tool_version}", "title": spec.title if spec else view.tool_id,
                 "summary": spec.summary if spec else None, "risk": view.risk, "side_effect": view.side_effect, "limitations": list(spec.limitations) if spec else []},
        "impact": IMPACT.get(view.side_effect, IMPACT["non_idempotent"]), "reversible": None,  # reversibility is not declared by tools: stated as unknown, never guessed
        "arguments": view.args, "args_hash": view.args_hash, "payload_hash": view.payload_hash, "reason": view.reason,
        "created_at": _iso(view.created_at), "expires_at": _iso(view.expires_at), "decided_at": _iso(view.decided_at), "decided": view.decided_by is not None,
        "consumed_at": _iso(view.consumed_at), "invocation_id": str(view.invocation_id) if view.invocation_id else None,
        "suspended": view.suspended,  # the task is durably suspended on this approval right now (no process holds it)
        "can_decide": can_decide,  # D050: only a workspace owner or admin may decide; a member can see the request
        "context": ({"objective": context["objective"], "started_by": context["started_by"], "task_title": context["task_titles"].get(view.task_key)} if context else None),
    }


@router.get("/approvals")
async def list_approvals(status: str | None = Query(default=None), run_id: uuid.UUID | None = None, limit: int = Query(default=50, ge=1, le=100), scope=Depends(get_scope),
                         store: ApprovalStore = Depends(get_approval_store), registry: ToolRegistry = Depends(get_registry), session_factory=Depends(get_session_factory)):
    if status is not None and status not in STATUSES:
        raise HTTPException(status_code=400, detail={"code": "invalid_status"})
    items = await asyncio.to_thread(store.list, scope, status=status, run_id=run_id, limit=limit)
    pending = await asyncio.to_thread(store.pending_count, scope)
    can = await asyncio.to_thread(store.can_decide, scope)
    contexts = await asyncio.to_thread(run_context, session_factory, scope, items)
    return {"items": [approval_json(v, registry, can, contexts.get(v.run_id)) for v in items], "pending": pending, "can_decide": can}


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: uuid.UUID, scope=Depends(get_scope), store: ApprovalStore = Depends(get_approval_store), registry: ToolRegistry = Depends(get_registry),
                       session_factory=Depends(get_session_factory)):
    try:
        view = await asyncio.to_thread(store.get, scope, approval_id)
        contexts = await asyncio.to_thread(run_context, session_factory, scope, [view])
        return approval_json(view, registry, await asyncio.to_thread(store.can_decide, scope), contexts.get(view.run_id))
    except ApprovalNotFound:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None


async def _decide(approval_id, decision, body, scope, store, registry, resume=None):
    submitted = body.payload_hash if body is not None else None
    try:
        view = await asyncio.to_thread(store.decide, scope, approval_id, decision, submitted)
        if resume is not None:
            resume()  # a process that executes runs picks the decided task up now (best effort; a worker's own poll does the same within seconds)
        return approval_json(view, registry)
    except ApprovalNotFound:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None
    except ApprovalForbidden:
        raise HTTPException(status_code=403, detail={"code": "approver_role_required"}) from None
    except ApprovalStale:
        raise HTTPException(status_code=409, detail={"code": "approval_stale"}) from None
    except ApprovalConflict as conflict:
        raise HTTPException(status_code=409, detail={"code": f"approval_{conflict.status}"}) from None


def build_approvals_router(get_plan_run_service) -> APIRouter:
    """The decision routes. After a decision the run service is asked to look for a task it can resume (a no-op in an API-only deployment, where a worker's
    own poll does it): the decision itself never runs anything, a process resumes the exact suspended call afterwards."""
    decisions = APIRouter()

    def nudge(service):
        def go():
            if service is not None and getattr(service.registry, "embedded", False):
                asyncio.ensure_future(service.claim_work(resume=True))  # best effort; failures are logged inside

        return go

    @decisions.post("/approvals/{approval_id}/approve", dependencies=[Depends(limit("approval_decide", 40))])
    async def approve(approval_id: uuid.UUID, body: DecisionBody | None = None, scope=Depends(get_scope), store: ApprovalStore = Depends(get_approval_store),
                      registry: ToolRegistry = Depends(get_registry), service=Depends(get_plan_run_service)):
        return await _decide(approval_id, "approve", body, scope, store, registry, nudge(service))

    @decisions.post("/approvals/{approval_id}/reject", dependencies=[Depends(limit("approval_decide", 40))])
    async def reject(approval_id: uuid.UUID, body: DecisionBody | None = None, scope=Depends(get_scope), store: ApprovalStore = Depends(get_approval_store),
                     registry: ToolRegistry = Depends(get_registry), service=Depends(get_plan_run_service)):
        return await _decide(approval_id, "reject", body, scope, store, registry, nudge(service))

    return decisions
