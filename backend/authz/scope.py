"""``MemoryScope``: the only key to owned memory (D027).

A scope is the pair (internal ``user_id``, authorized ``workspace_id``). It can only be issued by
``authz.workspaces.authorize_memory_scope``, which derives it from a trusted M1-D ``Principal`` and a workspace
membership read from the database. It cannot be constructed from an email, request JSON, a query parameter or a
header: the constructor always refuses, ``dataclasses.replace`` therefore refuses too, and the fields are frozen.

This is a guard against accidental or careless use (an email string, a half-built scope), not a sandbox against
code that deliberately subverts Python's object model.
"""

import uuid
from dataclasses import dataclass

from authz.errors import ScopeError

_ISSUE = object()  # private proof that issuance came from this package's authorization path


@dataclass(frozen=True, slots=True, init=False)
class MemoryScope:
    user_id: uuid.UUID
    workspace_id: uuid.UUID

    def __init__(self, *args, **kwargs):
        raise ScopeError("scope_not_constructible")  # only authz.workspaces.authorize_memory_scope issues scopes

    def __repr__(self) -> str:  # ids only; never an email
        return f"MemoryScope(user_id={self.user_id}, workspace_id={self.workspace_id})"


def _valid_id(value) -> bool:
    return isinstance(value, uuid.UUID) and value.int != 0


def _issue_scope(user_id, workspace_id, *, _proof=None) -> MemoryScope:
    """Issue a scope. PRIVATE: used only by ``authz.workspaces`` after membership was verified."""
    if _proof is not _ISSUE:
        raise ScopeError("scope_not_constructible")
    if not _valid_id(user_id) or not _valid_id(workspace_id):  # both required, both real UUIDs
        raise ScopeError("invalid_scope")
    scope = object.__new__(MemoryScope)
    object.__setattr__(scope, "user_id", user_id)
    object.__setattr__(scope, "workspace_id", workspace_id)
    return scope


def require_scope(scope) -> MemoryScope:
    """Return ``scope`` if it is a genuine MemoryScope, else raise ScopeError (fail closed)."""
    if not isinstance(scope, MemoryScope):
        raise ScopeError("scope_required")
    if not _valid_id(scope.user_id) or not _valid_id(scope.workspace_id):
        raise ScopeError("invalid_scope")
    return scope
