"""REST contract for plan execution (M3-B). Every route uses ``get_scope``. Starting a run takes NO client input: the plan id
in the path selects the plan, and the executed revision, the owner and the agent versions all come from the stored, approved
plan. Foreign and unknown ids are both 404. Progress is exposed by polling (``GET /plan-runs/{id}``): the existing WebSocket
protocol is a first-frame-authenticated single-job protocol and is deliberately left unchanged. Error bodies carry fixed
codes and texts only."""

import uuid
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as PathParam
from pydantic import BaseModel, ConfigDict, Field

from agentplatform.plan_runs import (
    AlreadyExecuted,
    PlanCancelledForRun,
    PlanNotApproved,
    PlanRunService,
    PlanStale,
    RunInProgress,
    RunInvalid,
    RunLimit,
    RetryNotAllowed,
    RunNotFound,
    RunView,
    ShuttingDown,
    TaskRunNotFound,
    TaskRunView,
    ReconciliationRequired,
    UncertainOutcome,
)
from agentplatform.plans import PlanNotFound
from authz.dependencies import get_scope
from ratelimit import limit


class StartRunRequest(BaseModel):
    """Empty on purpose: a client-supplied revision, user, workspace or agent version is refused (extra fields -> 422)."""

    model_config = ConfigDict(extra="forbid")


class RetryRequest(BaseModel):
    """The retry decision. Only the tasks whose previous outcome is uncertain need acknowledging; nothing else (no revision, user,
    workspace, agent or task list) is accepted from the client (extra fields -> 422)."""

    model_config = ConfigDict(extra="forbid")
    acknowledge_uncertain: list[str] = Field(default_factory=list, max_length=100)


def _iso(value):
    return value.isoformat() if value is not None else None


def _fail(status: int, code: str, message: str | None = None, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, **({"message": message} if message else {}), **extra})


def task_json(t: TaskRunView) -> dict:
    return {
        "id": t.key, "position": t.position, "level": t.level, "title": t.title, "status": t.status, "depends_on": list(t.depends_on),
        "blocked_by": list(t.blocked_by), "execution_id": str(t.execution_id) if t.execution_id else None,
        "agent": {"source": t.agent_source, "key": t.agent_key, "name": t.agent_name, "version": t.agent_version,
                  "version_id": str(t.agent_version_id) if t.agent_version_id else None},
        "error": {"code": t.error_code, "message": t.error_message} if t.error_code else None,
        "created_at": _iso(t.created_at), "started_at": _iso(t.started_at), "completed_at": _iso(t.completed_at),
        "reused": {"run_id": str(t.reused_run_id), "attempt": t.reused_attempt} if t.reused else None,
        "retry": t.retry_class,  # None | "safe" | "uncertain": whether re-running a task that did not succeed could repeat an effect
    }


def _link(value):
    return {"id": str(value["id"]), "attempt": value["attempt"]} if value else None


def run_json(run: RunView, outputs: list | None = None) -> dict:
    return {
        "id": str(run.id), "plan_id": str(run.plan_id), "revision": run.revision, "attempt": run.attempt, "status": run.status,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None, "counts": run.counts,
        "created_at": _iso(run.created_at), "completed_at": _iso(run.completed_at), "tasks": [task_json(t) for t in run.tasks],
        "outputs": [{**o, "execution_id": str(o["execution_id"])} for o in (outputs or [])],
        "started_by": run.started_by, "retry_of": _link(run.retry_of), "retried_by": _link(run.retried_by), "ownership": run.ownership, "retry_options": run.retry,
    }


def event_json(e) -> dict:
    """An event as the API shows it: the actor TYPE only (never a process or host identity)."""
    return {"id": e.id, "type": e.event_type, "task": e.task_key, "execution_id": str(e.execution_id) if e.execution_id else None,
            "actor": e.actor_type, "detail": e.detail, "created_at": _iso(e.created_at)}


def _start_error(exc: Exception) -> HTTPException | None:
    """The shared mapping of the start/retry refusals to fixed, safe error bodies."""
    if isinstance(exc, (PlanNotFound, RunNotFound)):
        return _fail(404, "not_found")
    if isinstance(exc, PlanCancelledForRun):
        return _fail(409, "plan_cancelled")
    if isinstance(exc, PlanNotApproved):
        return _fail(409, "plan_not_approved")
    if isinstance(exc, PlanStale):
        return _fail(409, "plan_stale")
    if isinstance(exc, RunInProgress):
        return _fail(409, "run_in_progress")
    if isinstance(exc, AlreadyExecuted):
        return _fail(409, "already_executed")
    if isinstance(exc, RunLimit):
        return _fail(409, "limit_reached", limit=exc.what)
    if isinstance(exc, ShuttingDown):
        return _fail(503, "shutting_down", "The server is shutting down. Please try again in a moment.")
    if isinstance(exc, RunInvalid):
        return _fail(409, "plan_invalid", "The plan can no longer be executed with the agents available now.", errors=exc.errors)
    if isinstance(exc, ReconciliationRequired):
        return _fail(409, "reconciliation_required", "A side-effecting action of these tasks has an unknown outcome. Reconcile it before retrying: acknowledging is not enough.", tasks=exc.tasks)
    if isinstance(exc, UncertainOutcome):
        return _fail(409, "uncertain_outcome", "These tasks may already have taken effect. Acknowledge them to run them again.", tasks=exc.tasks)
    if isinstance(exc, RetryNotAllowed):
        return _fail(409 if exc.code != "unknown_task" else 422, exc.code)
    return None


def build_plan_runs_router(get_service: Callable) -> APIRouter:
    router = APIRouter()

    @router.post("/plans/{plan_id}/runs", status_code=202, dependencies=[Depends(limit("run_start", 15))])
    async def start_run(plan_id: uuid.UUID, body: StartRunRequest | None = None, scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        try:
            return run_json(await service.start(scope, plan_id))
        except PlanNotFound:
            raise _fail(404, "not_found") from None
        except PlanCancelledForRun:
            raise _fail(409, "plan_cancelled") from None
        except PlanNotApproved:
            raise _fail(409, "plan_not_approved", "Only an approved plan can be executed.") from None
        except PlanStale:
            raise _fail(409, "plan_stale", "The approved revision is no longer the latest one. Approve the current revision first.") from None
        except RunInProgress:
            raise _fail(409, "run_in_progress", "This plan is already running.") from None
        except AlreadyExecuted:
            raise _fail(409, "already_executed", "This revision of the plan already completed successfully. Revise the plan to run it again.") from None
        except RunLimit as exc:
            raise _fail(409, "limit_reached", limit=exc.what) from None
        except ShuttingDown:
            raise _fail(503, "shutting_down", "The server is shutting down. Please try again in a moment.") from None
        except RunInvalid as exc:
            raise _fail(409, "plan_invalid", "The plan can no longer be executed with the agents available now.", errors=exc.errors) from None

    @router.get("/plans/{plan_id}/runs")
    async def list_runs(plan_id: uuid.UUID, scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        import asyncio

        try:
            runs = await asyncio.to_thread(service.store.list_for_plan, scope, plan_id)
        except PlanNotFound:
            raise _fail(404, "not_found") from None
        return {"items": [{"id": str(r.id), "revision": r.revision, "attempt": r.attempt, "status": r.status,
                           "error": {"code": r.error_code} if r.error_code else None, "counts": r.counts,
                           "created_at": _iso(r.created_at), "completed_at": _iso(r.completed_at)} for r in runs]}

    @router.get("/plan-runs/{run_id}")
    async def get_run(run_id: uuid.UUID, scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        try:
            return run_json(*await service.detail(scope, run_id))
        except RunNotFound:
            raise _fail(404, "not_found") from None

    @router.get("/plan-runs/{run_id}/tasks/{task_key}")
    async def get_task_run(run_id: uuid.UUID, task_key: str = PathParam(max_length=32), scope=Depends(get_scope),
                           service: PlanRunService = Depends(get_service)):
        try:
            run, task, record = await service.task_detail(scope, run_id, task_key)
        except RunNotFound:
            raise _fail(404, "not_found") from None
        except TaskRunNotFound:
            raise _fail(404, "task_not_found") from None
        return {**task_json(task), "run_id": str(run.id), "description": task.description, "expected_output": task.expected_output,
                "execution": {"id": str(record["execution_id"]), "status": record["status"], "provider": record["provider"], "model": record["model"],
                              "input": record["input"], "result": record["result"], "started_at": _iso(record["started_at"]),
                              "completed_at": _iso(record["completed_at"])} if record else None}

    @router.post("/plan-runs/{run_id}/retry", status_code=202, dependencies=[Depends(limit("run_retry", 15))])
    async def retry_run(run_id: uuid.UUID, body: RetryRequest | None = None, scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        """An explicit retry attempt of a run that ended without success: succeeded tasks are reused, the rest run again; uncertain tasks need
        an acknowledgement. Everything else comes from the stored, approved plan."""
        try:
            return run_json(await service.retry(scope, run_id, tuple((body or RetryRequest()).acknowledge_uncertain)))
        except (PlanNotFound, RunNotFound, PlanCancelledForRun, PlanNotApproved, PlanStale, RunInProgress, AlreadyExecuted, RunLimit, ShuttingDown, RunInvalid,
                ReconciliationRequired, UncertainOutcome, RetryNotAllowed) as exc:
            raise _start_error(exc) from None

    @router.get("/plan-runs/{run_id}/events")
    async def run_events_page(run_id: uuid.UUID, after: int = Query(default=0, ge=0, le=2**62), limit: int = Query(default=100, ge=1, le=200),
                              scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        """The append-only history of a run, oldest first; ``next_after`` continues the page."""
        import asyncio

        try:
            page = await asyncio.to_thread(service.store.events, scope, run_id, after, limit)
        except RunNotFound:
            raise _fail(404, "not_found") from None
        return {"items": [event_json(e) for e in page["items"]], "next_after": page["next_after"]}

    @router.post("/plan-runs/{run_id}/cancel")
    async def cancel_run(run_id: uuid.UUID, scope=Depends(get_scope), service: PlanRunService = Depends(get_service)):
        try:
            return run_json(await service.cancel(scope, run_id))
        except RunNotFound:
            raise _fail(404, "not_found") from None

    return router
