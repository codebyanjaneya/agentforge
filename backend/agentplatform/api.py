"""REST contract for templates and custom agents (M2-A).

Every route depends on ``get_scope`` (M1-F): a verified bearer token, the reserved ``X-Workspace-Id`` header rejected,
and the Principal's database-verified workspace. Request bodies forbid unknown fields, so a client-supplied user or
workspace id is refused, not ignored. Foreign and unknown agents are both 404. Error bodies carry fixed codes only.
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator

from agentplatform.schema import AgentDefinition, canonical, clean_note, validate_slug
from agentplatform.service import (
    AgentArchived,
    AgentNotFound,
    AgentService,
    AgentView,
    LimitReached,
    SlugTaken,
    VersionConflict,
    VersionInfo,
    VersionNotFound,
)
from agentplatform.templates import get_template, list_templates
from auth.dependencies import get_session_factory
from authz.dependencies import get_scope

router = APIRouter()


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 without the submitted values: FastAPI's default body echoes every rejected ``input`` back, which would repeat a
    pasted credential in the response (and in any log of it). Only the location, message and type are returned."""
    errors = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def get_service(session_factory=Depends(get_session_factory)) -> AgentService:
    return AgentService(session_factory)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAgentRequest(_Body):
    slug: str
    definition: AgentDefinition
    change_note: str | None = Field(default=None, max_length=200)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value):
        return validate_slug(value)

    @field_validator("change_note")
    @classmethod
    def _change_note(cls, value):
        return clean_note(value)


class UpdateAgentRequest(_Body):
    base_version: StrictInt = Field(ge=1)
    definition: AgentDefinition
    change_note: str | None = Field(default=None, max_length=200)

    @field_validator("change_note")
    @classmethod
    def _change_note(cls, value):
        return clean_note(value)


class RollbackRequest(_Body):
    to_version: StrictInt = Field(ge=1)
    base_version: StrictInt = Field(ge=1)


class InstantiateRequest(_Body):
    slug: str | None = None
    name: str | None = Field(default=None, max_length=80)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value):
        return None if value is None else validate_slug(value)


# ------------------------------------------------------------------------------------------------ serialization

def _iso(value):
    return value.isoformat() if value is not None else None


def _version_json(info: VersionInfo) -> dict:
    return {
        "version": info.number,
        "content_hash": info.content_hash,
        "change_note": info.change_note,
        "restored_from_version": info.restored_from_version,
        "created_at": _iso(info.created_at),
    }


def _summary(view: AgentView) -> dict:
    d = view.definition
    return {
        "id": str(view.id),
        "slug": view.slug,
        "name": d.name,
        "description": d.description,
        "role": d.role,
        "status": view.status,
        "current_version": view.current_version,
        "latest_version": view.latest_version,
        "template_id": view.template_id,
        "template_version": view.template_version,
        "created_at": _iso(view.created_at),
        "updated_at": _iso(view.updated_at),
        "archived_at": _iso(view.archived_at),
    }


def _detail(view: AgentView) -> dict:
    return {**_summary(view), "definition": canonical(view.definition), "version": _version_json(view.version)}


def _template_summary(template) -> dict:
    d = template.definition
    return {"id": template.id, "version": template.version, "name": d.name, "description": d.description, "role": d.role,
            "capabilities": list(d.capabilities)}


def _template_detail(template) -> dict:
    return {**_template_summary(template), "definition": canonical(template.definition)}


def _fail(status: int, code: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, **extra})


def _guard(call):
    """Run a service call and translate its domain errors into fixed API errors."""
    try:
        return call()
    except AgentNotFound:
        raise _fail(404, "not_found") from None
    except VersionNotFound:
        raise _fail(404, "version_not_found") from None
    except SlugTaken:
        raise _fail(409, "slug_taken") from None
    except AgentArchived:
        raise _fail(409, "agent_archived") from None
    except VersionConflict as exc:
        raise _fail(409, "version_conflict", latest_version=exc.latest_version) from None
    except LimitReached as exc:
        raise _fail(409, "limit_reached", limit=exc.what) from None


# ------------------------------------------------------------------------------------------------ templates

@router.get("/agent-templates")
def templates(scope=Depends(get_scope)):
    return {"items": [_template_summary(t) for t in list_templates()]}


@router.get("/agent-templates/{template_id}")
def template(template_id: str, scope=Depends(get_scope)):
    found = get_template(template_id)
    if found is None:
        raise _fail(404, "not_found")
    return _template_detail(found)


@router.post("/agent-templates/{template_id}/instantiate", status_code=201)
def instantiate(template_id: str, body: InstantiateRequest | None = None, scope=Depends(get_scope),
                service: AgentService = Depends(get_service)):
    found = get_template(template_id)
    if found is None:
        raise _fail(404, "not_found")
    body = body or InstantiateRequest()
    data = canonical(found.definition)  # an independent copy: the template itself is never touched
    if body.name is not None:
        data["name"] = body.name
    try:
        definition = AgentDefinition.model_validate(data)
    except ValidationError:
        raise _fail(422, "invalid_definition") from None
    slug = body.slug or found.id
    return _detail(_guard(lambda: service.create(scope, slug, definition, template_id=found.id, template_version=found.version,
                                                 change_note=f"Created from template {found.id}")))


# ------------------------------------------------------------------------------------------------ agents

@router.post("/agents", status_code=201)
def create_agent(body: CreateAgentRequest, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.create(scope, body.slug, body.definition, change_note=body.change_note)))


@router.get("/agents")
def list_agents(status: Literal["active", "archived", "all"] = Query(default="active"), scope=Depends(get_scope),
                service: AgentService = Depends(get_service)):
    return {"items": [_summary(view) for view in service.list_agents(scope, status)]}


@router.get("/agents/{agent_id}")
def get_agent(agent_id: uuid.UUID, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.get(scope, agent_id)))


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: uuid.UUID, body: UpdateAgentRequest, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.update(scope, agent_id, body.base_version, body.definition, change_note=body.change_note)))


@router.post("/agents/{agent_id}/archive")
def archive_agent(agent_id: uuid.UUID, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.archive(scope, agent_id)))


@router.post("/agents/{agent_id}/unarchive")
def unarchive_agent(agent_id: uuid.UUID, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.unarchive(scope, agent_id)))


@router.get("/agents/{agent_id}/versions")
def list_versions(agent_id: uuid.UUID, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return {"items": [_version_json(info) for info in _guard(lambda: service.list_versions(scope, agent_id))]}


@router.get("/agents/{agent_id}/versions/{version}")
def get_version(agent_id: uuid.UUID, version: int = Path(ge=1), scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    info, definition = _guard(lambda: service.get_version(scope, agent_id, version))
    return {**_version_json(info), "definition": canonical(definition)}


@router.post("/agents/{agent_id}/rollback")
def rollback_agent(agent_id: uuid.UUID, body: RollbackRequest, scope=Depends(get_scope), service: AgentService = Depends(get_service)):
    return _detail(_guard(lambda: service.rollback(scope, agent_id, body.to_version, body.base_version)))
