"""REST routes for the tool registry and tool invocations (M4-B). Read-only; every route uses ``get_scope``.

The CATALOG (``/tools``) is the same for every user (trusted code) but is shown together with facts that belong to the caller's workspace: which of
THEIR agents are permitted to use each tool (read from their pinned agent definitions) and how often the tools were really used. An invocation (``/tool-invocations``)
is READABLE by every member of the workspace that owns it (the same shared-read rule as plans, runs and usage, flow.md 25.36); another workspace's invocation is indistinguishable from an unknown one (404). Nothing here writes. Inputs and outputs are the
stored, redacted, bounded JSON; error text is fixed. Nothing here can run a tool: execution happens only inside an agent's execution (the runtime)."""

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentplatform.execution import CursorError, decode_cursor, encode_cursor
from agentplatform.service import AgentService
from agentplatform.tools import ToolRegistry, default_registry
from agentplatform.tools.invoker import InvocationNotFound, InvocationView, ToolStore
from auth.dependencies import get_session_factory
from authz.dependencies import get_scope
from ratelimit import limit
from agentplatform.tools import dispatch as dp
from memory.models import PlanRun, ToolDispatch

router = APIRouter()


def get_registry() -> ToolRegistry:
    return default_registry()


def get_tool_store(session_factory=Depends(get_session_factory)) -> ToolStore:
    return ToolStore(session_factory)


def _iso(value):
    return value.isoformat() if value is not None else None


def spec_json(spec, registry: ToolRegistry, *, detail: bool = False) -> dict:
    body = {
        "id": spec.id, "version": spec.version, "ref": spec.ref, "title": spec.title, "summary": spec.summary, "category": spec.category, "capabilities": list(spec.capabilities),
        "risk": spec.risk, "side_effect": spec.side_effect, "lifecycle": spec.lifecycle, "executable": registry.executable(spec), "policy": registry.policy.decide(spec), "requires_approval": registry.gated(spec), "grantable": registry.executable(spec) or (registry.allow_gated and registry.gated(spec)),
        "timeout_seconds": spec.timeout_seconds, "max_output_bytes": spec.max_output_bytes,
    }
    if spec.side_effect != "none":  # P0: what is guaranteed for this tool, in words and in facts
        body["delivery"] = {"retry_policy": spec.retry_policy, "remote_idempotency": spec.remote_idempotency, "can_query_remote": registry.lookup(spec) is not None,
                            "guarantee": spec.delivery or "At most one automatic dispatch per operation. No exactly-once claim: an unknown outcome is reconciled, never retried automatically."}
    if detail:
        body.update({"description": spec.description, "limitations": list(spec.limitations), "example": spec.example, "input_schema": spec.input_schema(), "output_schema": spec.output_schema()})
    return body


def _granting_agents(scope, session_factory) -> dict:
    """tool id -> [{id, slug, name, version}] of the caller's ACTIVE custom agents whose current version pins that tool."""
    out: dict = {}
    for agent in AgentService(session_factory).list_agents(scope, "active"):
        for pinned in agent.definition.allowed_tools:
            tool_id, _, version = pinned.partition("@")
            out.setdefault(tool_id, []).append({"id": str(agent.id), "slug": agent.slug, "name": agent.definition.name, "version": int(version) if version.isdigit() else None})
    return out


def invocation_json(view: InvocationView, *, detail: bool = False, dispatch: dict | None = None) -> dict:
    duration = int((view.completed_at - view.started_at).total_seconds() * 1000) if view.completed_at else None
    body = {
        "id": str(view.id), "execution_id": str(view.execution_id), "run_id": str(view.run_id) if view.run_id else None, "task": view.task_key, "agent": view.agent_key,
        "tool": {"id": view.tool_id, "version": view.tool_version}, "seq": view.seq, "status": view.status, "side_effect": view.side_effect,
        "error": {"code": view.error_code, "message": view.error_message} if view.error_code else None,
        "created_at": _iso(view.created_at), "started_at": _iso(view.started_at), "completed_at": _iso(view.completed_at), "duration_ms": duration,
        "dispatch": dispatch,  # P0: None for a tool without side effects; else the state, whether the outcome is confirmed or unknown, and any reconciliation
    }
    if detail:
        body.update({"input": view.input, "output": view.output, "args_hash": view.args_hash})
    else:
        body.update({"input_preview": _preview(view.input), "output_preview": _preview(view.output)})
    return body


def _preview(value, limit: int = 140):
    if value is None:
        return None
    text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= limit else text[: limit - 1] + "…"


@router.get("/tools")
async def list_tools(scope=Depends(get_scope), registry: ToolRegistry = Depends(get_registry), store: ToolStore = Depends(get_tool_store),
                     session_factory=Depends(get_session_factory)):
    import asyncio

    usage, grants = await asyncio.gather(asyncio.to_thread(store.usage, scope), asyncio.to_thread(_granting_agents, scope, session_factory))
    grouped: dict = {}
    for spec in registry.list_specs():
        grouped.setdefault(spec.id, []).append(spec)
    items = []
    for tool_id, specs in grouped.items():
        latest = registry.get(tool_id) or specs[0]
        calls = [usage[(tool_id, s.version)] for s in specs if (tool_id, s.version) in usage]
        items.append({
            "id": tool_id, "title": latest.title, "summary": latest.summary, "category": latest.category, "capabilities": list(latest.capabilities), "latest": spec_json(latest, registry),
            "versions": [{"version": s.version, "lifecycle": s.lifecycle, "executable": registry.executable(s)} for s in specs],
            "usage": {"calls": sum(c["calls"] for c in calls), "last_used": _iso(max((c["last_used"] for c in calls), default=None))},
            "agents": grants.get(tool_id, []),
        })
    return {"items": items, "policy": {"max_risk": "read", "side_effects": "none", "approvals": "per_call"}}


@router.get("/tools/{tool_id}")
async def get_tool(tool_id: str, scope=Depends(get_scope), registry: ToolRegistry = Depends(get_registry), store: ToolStore = Depends(get_tool_store),
                   session_factory=Depends(get_session_factory)):
    import asyncio

    specs = [s for s in registry.list_specs() if s.id == tool_id]
    if not specs:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    usage, grants, recent = await asyncio.gather(asyncio.to_thread(store.usage, scope), asyncio.to_thread(_granting_agents, scope, session_factory),
                                                 asyncio.to_thread(lambda: store.list(scope, tool_id=tool_id, limit=10)))
    return {
        "id": tool_id, "versions": [{**spec_json(s, registry, detail=True), "usage": {"calls": usage.get((tool_id, s.version), {}).get("calls", 0),
                                                                                        "last_used": _iso(usage.get((tool_id, s.version), {}).get("last_used"))}} for s in specs],
        "agents": grants.get(tool_id, []), "recent_invocations": [invocation_json(v) for v in recent["items"]],
    }


def _page(result: dict, dispatches: dict | None = None) -> dict:
    nxt = result["next"]
    dispatches = dispatches or {}
    return {"items": [invocation_json(v, dispatch=dispatches.get(v.id)) for v in result["items"]], "next_cursor": encode_cursor(nxt[0], nxt[1]) if nxt else None}


@router.get("/tool-invocations")
async def list_invocations(run_id: uuid.UUID | None = None, execution_id: uuid.UUID | None = None, tool_id: str | None = Query(default=None, max_length=64),
                           agent: str | None = Query(default=None, max_length=64), status: str | None = Query(default=None, max_length=16),
                           limit: int = Query(default=25, ge=1, le=100), cursor: str | None = Query(default=None, max_length=300),
                           scope=Depends(get_scope), store: ToolStore = Depends(get_tool_store)):
    import asyncio

    try:
        before = decode_cursor(cursor) if cursor else None
    except CursorError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"}) from None
    result = await asyncio.to_thread(lambda: store.list(scope, run_id=run_id, execution_id=execution_id, tool_id=tool_id, agent_key=agent, status=status, limit=limit, before=before))
    return _page(result, await asyncio.to_thread(store.dispatches_for, scope, [v.id for v in result["items"]]))


@router.get("/tool-invocations/{invocation_id}")
async def get_invocation(invocation_id: uuid.UUID, scope=Depends(get_scope), store: ToolStore = Depends(get_tool_store)):
    import asyncio

    try:
        view = await asyncio.to_thread(store.get, scope, invocation_id)
        return invocation_json(view, detail=True, dispatch=(await asyncio.to_thread(store.dispatches_for, scope, [view.id])).get(view.id))
    except InvocationNotFound:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None


@router.get("/plan-runs/{run_id}/tool-invocations")
async def run_invocations(run_id: uuid.UUID, limit: int = Query(default=100, ge=1, le=100), scope=Depends(get_scope), store: ToolStore = Depends(get_tool_store),
                          session_factory=Depends(get_session_factory)):
    """Every tool call of a run, oldest first, so a run page can show them under their tasks. Scoped through the run itself (foreign == unknown)."""
    import asyncio

    def owned() -> bool:
        with session_factory() as session:
            return session.execute(select(PlanRun.id).where(PlanRun.id == run_id, PlanRun.workspace_id == scope.workspace_id)).first() is not None  # read: any member of the workspace

    if not await asyncio.to_thread(owned):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    result = await asyncio.to_thread(lambda: store.list(scope, run_id=run_id, limit=limit))
    dispatches = await asyncio.to_thread(store.dispatches_for, scope, [v.id for v in result["items"]])
    return {"items": [invocation_json(v, dispatch=dispatches.get(v.id)) for v in reversed(result["items"])], "truncated": result["next"] is not None}


# ------------------------------------------------------------------------------------------------ P0: reconciliation of unknown outcomes

class ReconcileBody(BaseModel):
    """``mode="query"`` asks the REMOTE service (only for a tool that can be queried; the decision then follows its answer). ``mode="attest"`` records a person's statement: it is stored and shown as
    an attestation, never as proof of the remote outcome. Unknown fields are refused; the actor and workspace always come from the session."""

    model_config = ConfigDict(extra="forbid")
    mode: str = Field(pattern="^(query|attest)$")
    decision: str | None = Field(default=None, pattern="^(confirmed_succeeded|confirmed_failed|unresolved)$")
    evidence: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=3, max_length=500)


RECONCILE_STATUS = {"not_found": 404, "reconciler_role_required": 403, "already_resolved": 409, "not_unknown": 409, "invalid_decision": 422, "evidence_and_reason_required": 422,
                    "lookup_unavailable": 422}


@router.post("/tool-invocations/{invocation_id}/reconcile", dependencies=[Depends(limit("reconcile", 20))])
async def reconcile_invocation(invocation_id: uuid.UUID, body: ReconcileBody, scope=Depends(get_scope), registry: ToolRegistry = Depends(get_registry),
                               session_factory=Depends(get_session_factory)):
    """Resolve an invocation whose outcome is UNKNOWN. Owners and admins only. Never dispatches anything: it only records evidence and decides what is KNOWN."""
    import asyncio

    def go() -> dict:
        with session_factory() as session:
            row = session.execute(select(ToolDispatch).where(ToolDispatch.invocation_id == invocation_id, ToolDispatch.workspace_id == scope.workspace_id)).scalar_one_or_none()
            if row is None:
                raise dp.ReconcileError("not_found")
            key, spec_ref = row.idempotency_key, (row.tool_id, row.tool_version)
        if body.mode == "attest":
            if body.decision not in ("confirmed_succeeded", "confirmed_failed") or not body.evidence:
                raise dp.ReconcileError("evidence_and_reason_required")
            return dp.reconcile(session_factory, scope, invocation_id, decision=body.decision, evidence_kind="operator_attestation", evidence=body.evidence, reason=body.reason)
        spec = registry.get(spec_ref[0], spec_ref[1])
        lookup = registry.lookup(spec) if spec is not None else None
        if lookup is None:
            raise dp.ReconcileError("lookup_unavailable")
        try:
            status, ref, evidence = lookup(key)
        except Exception:  # noqa: BLE001 - the remote could not be asked: nothing is decided, the attempt is recorded
            status, ref, evidence = "unknown", None, "The remote service could not be queried."
        # an answer is only PROOF when the remote says it performed the operation, or (for a service that honours idempotency keys) that it never accepted the key
        decision = "confirmed_succeeded" if status == "succeeded" else "confirmed_failed" if status == "failed" or (status == "not_found" and spec.remote_idempotency) else "unresolved"
        return dp.reconcile(session_factory, scope, invocation_id, decision=decision, evidence_kind="remote_query", evidence=str(evidence or status)[:500], reason=body.reason, external_ref=ref)

    try:
        return {"dispatch": await asyncio.to_thread(go)}
    except dp.ReconcileError as exc:
        raise HTTPException(status_code=RECONCILE_STATUS.get(exc.code, 400), detail={"code": exc.code}) from None


@router.get("/reconciliations")
async def pending_reconciliations(scope=Depends(get_scope), session_factory=Depends(get_session_factory)):
    """The workspace's side-effecting calls whose outcome is UNKNOWN (any member may see that they exist; only an owner or admin may resolve them)."""
    import asyncio

    def load():
        from memory.models import WorkspaceMembership

        with session_factory() as session:
            role = session.execute(select(WorkspaceMembership.role).where(WorkspaceMembership.workspace_id == scope.workspace_id, WorkspaceMembership.user_id == scope.user_id)).scalar_one_or_none()
            rows = session.execute(select(ToolDispatch).where(ToolDispatch.workspace_id == scope.workspace_id, ToolDispatch.state == "unknown")
                                   .order_by(ToolDispatch.created_at.desc()).limit(100)).scalars().all()
            return {"can_reconcile": role in dp.RECONCILER_ROLES, "items": [{"invocation_id": str(r.invocation_id), "run_id": str(r.run_id) if r.run_id else None, "task": r.task_key,
                                                                             "tool": f"{r.tool_id}@{r.tool_version}", "dispatch": dp.dispatch_view(r)} for r in rows]}

    return await asyncio.to_thread(load)
