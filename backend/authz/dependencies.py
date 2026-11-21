"""FastAPI dependency (M1-F, extended in M6): ``get_scope`` = verified Principal + database-verified workspace membership.

Order: authentication first (401/403/500 from ``get_principal``), then the workspace. WITHOUT ``X-Workspace-Id`` the workspace is the Principal's own personal
workspace (unchanged). WITH it (M6, the "later step" D025 reserved the header for) the value is only a REQUESTED resource: it must parse as a UUID (400
otherwise) and the Principal's membership of it is read from the database now (403 when they are not a member; an unknown workspace and a workspace of
strangers are indistinguishable). The role comes from the database, never from the request. Responses carry fixed strings only.
"""

import logging
import uuid

from fastapi import Depends, Header, HTTPException

from auth.dependencies import get_principal, get_session_factory
from authz.errors import AuthorizationError
from authz.workspaces import authorize_memory_scope, authorize_personal_scope

logger = logging.getLogger("agentforge.authz")


def get_scope(
    principal=Depends(get_principal),
    session_factory=Depends(get_session_factory),
    x_workspace_id: str | None = Header(default=None),
):
    requested = None
    if x_workspace_id is not None and x_workspace_id != "":
        try:
            requested = uuid.UUID(x_workspace_id)
        except ValueError:
            logger.info("Request refused (invalid_workspace_id)")
            raise HTTPException(status_code=400, detail="X-Workspace-Id is invalid") from None
    try:
        return authorize_memory_scope(session_factory, principal, requested) if requested is not None else authorize_personal_scope(session_factory, principal)
    except AuthorizationError as exc:
        logger.info("Workspace authorization refused (%s)", exc.reason)
        raise HTTPException(status_code=403, detail="Forbidden") from None
    except Exception as exc:  # noqa: BLE001 - database or internal failure: fail closed, expose nothing
        logger.error("Workspace authorization failed unexpectedly (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Authorization is not available") from None
