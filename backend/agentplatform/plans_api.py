"""REST contract for plans (M3-A). Every route uses ``get_scope`` (verified bearer Principal + database-verified workspace); bodies
forbid unknown fields, so a client-supplied user or workspace id is refused. Foreign and unknown plans are both 404. Creating,
revising and approving a plan never starts execution. Error bodies carry fixed codes and texts only."""

import uuid
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from agentplatform.execution import Busy, CursorError
from agentplatform.planning import PlanningError, PlanRejected, ProposedTask, Unplannable
from agentplatform.plans import (
    PlanCancelled,
    PlanConflict,
    PlanInvalid,
    PlanLimitReached,
    PlanNotFound,
    PlanningService,
    PlanView,
    RevisionNotFound,
    RevisionView,
)
from agentplatform.schema import clean_text
from authz.dependencies import get_scope
from ratelimit import limit

PLANNING_STATUS = {"timeout": 504, "rate_limited": 429, "unavailable": 503, "auth": 503, "unsupported_model": 503, "unsupported_provider": 503,
                   "bad_request": 502, "invalid_response": 502, "invalid_output": 502, "internal_error": 500}


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePlanRequest(_Body):
    objective: str


class RevisedTask(ProposedTask):
    rationale: str = "Assigned by the user."


class ReviseRequest(_Body):
    base_revision: StrictInt = Field(ge=1)
    interpretation: str | None = Field(default=None, max_length=600)
    tasks: list[RevisedTask] = Field(min_length=1, max_length=12)

    @field_validator("interpretation")
    @classmethod
    def _interpretation(cls, value):
        return None if value in (None, "") else clean_text(value, minimum=10, maximum=600, multiline=True)


class ApproveRequest(_Body):
    revision: StrictInt = Field(ge=1)


def _iso(value):
    return value.isoformat() if value is not None else None


def task_json(task) -> dict:
    return {
        "id": task.key, "position": task.position, "level": task.level, "title": task.title, "description": task.description,
        "expected_output": task.expected_output, "rationale": task.rationale, "dependency_reason": task.dependency_reason,
        "depends_on": list(task.depends_on), "complexity": task.complexity, "priority": task.priority, "required_tool": task.required_tool,
        "agent": {"source": task.agent_source, "key": task.agent_key, "name": task.agent_name, "version": task.agent_version,
                  "version_id": str(task.agent_version_id) if task.agent_version_id else None},
    }


def revision_json(rev: RevisionView) -> dict:
    return {
        "revision": rev.revision, "origin": rev.origin, "interpretation": rev.interpretation, "warnings": list(rev.warnings),
        "unsupported": list(rev.unsupported), "provider": rev.provider, "model": rev.model, "created_at": _iso(rev.created_at),
        "levels": [list(level) for level in rev.levels], "tasks": [task_json(t) for t in rev.tasks],
    }


def summary_json(plan, task_count: int, creators: dict | None = None) -> dict:
    return {
        "id": str(plan.id), "objective": plan.objective if len(plan.objective) <= 200 else plan.objective[:199] + "…", "status": plan.status,
        "latest_revision": plan.latest_revision, "approved_revision": plan.approved_revision, "task_count": task_count,
        "created_at": _iso(plan.created_at), "updated_at": _iso(plan.updated_at), "approved_at": _iso(plan.approved_at),
        "created_by": (creators or {}).get(plan.user_id),
    }


def detail_json(view: PlanView, latest_run: dict | None = None) -> dict:
    """``latest_run`` (M3-B, additive) is the newest run of the plan or None; ``execution.started`` is true once a run exists.
    A plan's own status never changes because it was run: cancelling the plan only prevents FUTURE runs."""
    return {
        "id": str(view.id), "objective": view.objective, "status": view.status, "latest_revision": view.latest_revision,
        "approved_revision": view.approved_revision, "created_by": view.created_by, "created_at": _iso(view.created_at), "updated_at": _iso(view.updated_at),
        "approved_at": _iso(view.approved_at), "revision": revision_json(view.current),
        "execution": {"started": latest_run is not None},
        "latest_run": ({"id": str(latest_run["id"]), "status": latest_run["status"], "attempt": latest_run["attempt"],
                        "created_at": _iso(latest_run["created_at"]), "completed_at": _iso(latest_run["completed_at"])} if latest_run else None),
    }


def _detail(service, scope, view: PlanView) -> dict:
    return detail_json(view, service.store.latest_run(scope, view.id))


def _fail(status: int, code: str, message: str | None = None, **extra) -> HTTPException:
    detail = {"code": code, **({"message": message} if message else {}), **extra}
    return HTTPException(status_code=status, detail=detail)


def _guard(call):
    try:
        return call()
    except PlanNotFound:
        raise _fail(404, "not_found") from None
    except RevisionNotFound:
        raise _fail(404, "revision_not_found") from None
    except PlanConflict as exc:
        raise _fail(409, "revision_conflict", latest_revision=exc.latest_revision) from None
    except PlanCancelled:
        raise _fail(409, "plan_cancelled") from None
    except PlanLimitReached as exc:
        raise _fail(409, "limit_reached", limit=exc.what) from None
    except PlanInvalid as exc:
        raise _fail(409, "plan_invalid", "The plan no longer validates against the agents available now.", errors=exc.errors, warnings=exc.warnings) from None
    except PlanRejected as exc:
        raise _fail(422, "plan_rejected", "The plan did not pass validation.", errors=exc.errors) from None


def build_plans_router(get_service: Callable) -> APIRouter:
    router = APIRouter()

    @router.post("/plans", status_code=201, dependencies=[Depends(limit("plan_create", 15))])
    async def create_plan(body: CreatePlanRequest, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        try:
            return detail_json(await service.create_plan(scope, body.objective))
        except ValueError as exc:
            raise _fail(422, "objective_invalid", str(exc)) from None
        except Busy as exc:
            raise HTTPException(status_code=429, detail={"code": exc.code, "message": exc.message}, headers={"Retry-After": "5"}) from None
        except PlanningError as exc:
            raise _fail(PLANNING_STATUS.get(exc.code, 500), exc.code, exc.message) from None
        except Unplannable as exc:
            raise _fail(422, "objective_not_plannable", "This objective cannot be planned safely with the agents available to you.",
                        interpretation=exc.interpretation, unsupported=exc.unsupported) from None
        except PlanRejected as exc:
            raise _fail(422, "plan_rejected", "The planner proposed a plan that failed validation. Please try again or rephrase the objective.",
                        errors=exc.errors) from None
        except PlanLimitReached as exc:
            raise _fail(409, "limit_reached", limit=exc.what) from None

    @router.get("/plans")
    def list_plans(status: Literal["active", "draft", "approved", "cancelled", "all"] = Query(default="active"),
                   limit: int = Query(default=20, ge=1, le=100), cursor: str | None = Query(default=None, max_length=300),
                   scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        try:
            page = service.store.list_plans(scope, status=status, limit=limit, cursor=cursor)
        except CursorError:
            raise _fail(400, "invalid_cursor") from None
        return {"items": [summary_json(plan, count, page.get("creators")) for plan, count in page["items"]], "next_cursor": page["next_cursor"]}

    @router.get("/plans/{plan_id}")
    def get_plan(plan_id: uuid.UUID, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return _detail(service, scope, _guard(lambda: service.store.get(scope, plan_id)))

    @router.get("/plans/{plan_id}/revisions")
    def list_revisions(plan_id: uuid.UUID, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        rows = _guard(lambda: service.store.list_revisions(scope, plan_id))
        return {"items": [{"revision": n, "origin": origin, "created_at": _iso(created), "provider": provider, "model": model}
                          for n, origin, created, provider, model in rows]}

    @router.get("/plans/{plan_id}/revisions/{revision}")
    def get_revision(plan_id: uuid.UUID, revision: int = Path(ge=1), scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return revision_json(_guard(lambda: service.store.get_revision(scope, plan_id, revision)))

    @router.post("/plans/{plan_id}/validate")
    def validate_plan(plan_id: uuid.UUID, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return _guard(lambda: service.validate(scope, plan_id))

    @router.post("/plans/{plan_id}/revisions", dependencies=[Depends(limit("plan_revise", 40))])
    def revise_plan(plan_id: uuid.UUID, body: ReviseRequest, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return _detail(service, scope, _guard(lambda: service.revise(scope, plan_id, body.base_revision, body.interpretation, list(body.tasks))))

    @router.post("/plans/{plan_id}/approve", dependencies=[Depends(limit("plan_approve", 40))])
    def approve_plan(plan_id: uuid.UUID, body: ApproveRequest, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return _detail(service, scope, _guard(lambda: service.approve(scope, plan_id, body.revision)))

    @router.post("/plans/{plan_id}/cancel")
    def cancel_plan(plan_id: uuid.UUID, scope=Depends(get_scope), service: PlanningService = Depends(get_service)):
        return _detail(service, scope, _guard(lambda: service.store.cancel(scope, plan_id)))

    return router
