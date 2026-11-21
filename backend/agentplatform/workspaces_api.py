"""REST routes for workspaces and their members (M6). Authenticated by the Principal (not by ``get_scope``: these routes choose or list workspaces).

Membership and role come from the database. Creating a workspace makes the caller its sole owner; only an owner adds a member (an EXISTING user, found by email as a
lookup aid only); owner/admin/member are the only roles. Refusals are fixed strings and an unknown workspace looks like a workspace of strangers (404)."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from auth.dependencies import get_principal, get_session_factory
from authz.errors import AuthorizationError
from authz.invitations import (accept_invitation, change_role_by_email, create_invitation, leave_workspace, list_invitations, remove_member_by_email,
                               revoke_invitation)
from authz.workspaces import add_member, create_team_workspace, list_members, list_memberships

router = APIRouter()


class CreateWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class AddMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(pattern="^(admin|member)$")


REFUSALS = {"not_a_member": (404, "not_found"), "owner_required": (403, "owner_required"), "target_unknown": (404, "user_not_found"), "already_a_member": (409, "already_a_member"),
            "personal_workspace": (409, "personal_workspace"), "invalid_name": (422, "invalid_name"), "unsupported_role": (422, "unsupported_role"), "invalid_target": (422, "invalid_target"),
            "invalid_invitation": (404, "invalid_invitation"), "sole_owner_protected": (409, "sole_owner_protected"), "target_not_a_member": (404, "user_not_found")}


def _refuse(exc: AuthorizationError):
    status, code = REFUSALS.get(exc.reason, (403, "forbidden"))
    return HTTPException(status_code=status, detail={"code": code})


@router.get("/workspaces")
async def workspaces(principal=Depends(get_principal), sf=Depends(get_session_factory)):
    return {"items": await asyncio.to_thread(list_memberships, sf, principal)}


@router.post("/workspaces", status_code=201)
async def create_workspace(body: CreateWorkspace, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    try:
        wid = await asyncio.to_thread(create_team_workspace, sf, principal, body.name)
    except AuthorizationError as exc:
        raise _refuse(exc) from None
    return {"id": str(wid), "name": body.name.strip(), "personal": False, "role": "owner", "members": 1}


@router.get("/workspaces/{workspace_id}/members")
async def members(workspace_id: uuid.UUID, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    try:
        return {"items": await asyncio.to_thread(list_members, sf, principal, workspace_id)}
    except AuthorizationError as exc:
        raise _refuse(exc) from None


@router.post("/workspaces/{workspace_id}/members", status_code=201)
async def add(workspace_id: uuid.UUID, body: AddMember, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    try:
        await asyncio.to_thread(add_member, sf, principal, workspace_id, body.email, body.role)
    except AuthorizationError as exc:
        raise _refuse(exc) from None
    return {"email": body.email.strip().lower(), "role": body.role}


class AcceptInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


class MemberTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except AuthorizationError as exc:
        raise _refuse(exc) from None


@router.post("/workspaces/{workspace_id}/invitations", status_code=201)
async def invite(workspace_id: uuid.UUID, body: AddMember, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    return await _call(create_invitation, sf, principal, workspace_id, body.email, body.role)  # the token is in THIS response only


@router.get("/workspaces/{workspace_id}/invitations")
async def invitations(workspace_id: uuid.UUID, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    return {"items": await _call(list_invitations, sf, principal, workspace_id)}


@router.delete("/workspaces/{workspace_id}/invitations/{invitation_id}", status_code=204)
async def revoke(workspace_id: uuid.UUID, invitation_id: uuid.UUID, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    await _call(revoke_invitation, sf, principal, workspace_id, invitation_id)


@router.post("/invitations/accept")
async def accept(body: AcceptInvitation, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    return await _call(accept_invitation, sf, principal, body.token)


@router.patch("/workspaces/{workspace_id}/members")
async def set_role(workspace_id: uuid.UUID, body: AddMember, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    await _call(change_role_by_email, sf, principal, workspace_id, body.email, body.role)
    return {"email": body.email.strip().lower(), "role": body.role}


@router.post("/workspaces/{workspace_id}/members/remove", status_code=204)
async def remove(workspace_id: uuid.UUID, body: MemberTarget, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    await _call(remove_member_by_email, sf, principal, workspace_id, body.email)


@router.post("/workspaces/{workspace_id}/leave", status_code=204)
async def leave(workspace_id: uuid.UUID, principal=Depends(get_principal), sf=Depends(get_session_factory)):
    await _call(leave_workspace, sf, principal, workspace_id)
