"""Execution history REST routes (M2-B): read-only, scoped, newest first. Every route uses ``get_scope``; another user's
execution is indistinguishable from an unknown one (404). Responses never contain credentials or raw provider output
beyond the stored (truncated) result the caller already received."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from agentplatform.execution import CursorError, ExecutionNotFound, ExecutionStore, ExecutionView
from auth.dependencies import get_session_factory
from authz.dependencies import get_scope

router = APIRouter()


def get_store(session_factory=Depends(get_session_factory)) -> ExecutionStore:
    return ExecutionStore(session_factory)


def _iso(value):
    return value.isoformat() if value is not None else None


def summary(view: ExecutionView) -> dict:
    duration = int((view.completed_at - view.started_at).total_seconds() * 1000) if view.completed_at else None
    return {
        "id": str(view.id),
        "agent_source": view.agent_source,
        "agent_key": view.agent_key,
        "agent_name": view.agent_name,
        "agent_version": view.agent_version,
        "status": view.status,
        "error_code": view.error_code,
        "provider": view.provider,
        "model": view.model,
        "created_at": _iso(view.created_at),
        "started_at": _iso(view.started_at),
        "completed_at": _iso(view.completed_at),
        "duration_ms": duration,
    }


def detail(view: ExecutionView) -> dict:
    return {**summary(view), "agent_version_id": str(view.agent_version_id) if view.agent_version_id else None, "task": view.task,
            "result": view.result, "error_message": view.error_message}


@router.get("/executions")
def list_executions(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=300),
    agent: str | None = Query(default=None, min_length=1, max_length=64),
    scope=Depends(get_scope),
    store: ExecutionStore = Depends(get_store),
):
    """The caller's executions, newest first; ``agent`` filters by agent key; ``next_cursor`` pages onwards."""
    try:
        page = store.list(scope, limit=limit, cursor=cursor, agent_key=agent)
    except CursorError:
        raise HTTPException(status_code=400, detail={"code": "invalid_cursor"}) from None
    return {"items": [summary(view) for view in page["items"]], "next_cursor": page["next_cursor"]}


@router.get("/executions/{execution_id}")
def get_execution(execution_id: uuid.UUID, scope=Depends(get_scope), store: ExecutionStore = Depends(get_store)):
    try:
        return detail(store.get(scope, execution_id))
    except ExecutionNotFound:
        raise HTTPException(status_code=404, detail={"code": "not_found"}) from None
