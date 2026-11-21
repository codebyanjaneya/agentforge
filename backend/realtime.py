"""Secured ``/ws`` handler (D026, M1-F).

Handshake, in order:
1. The connection is accepted, then the ``Origin`` header must be present and exactly equal to an entry of the
   configured allow-list (the same list as CORS, D022). An empty list therefore rejects every browser connection
   (fail closed); there is no wildcard. Failure closes with 4403 before any frame is read.
2. The FIRST frame must arrive within ``AUTH_TIMEOUT_SECONDS`` (5) and be exactly
   ``{"type": "auth", "token": "<access token>"}`` (any other key, e.g. an email or workspace id, is refused). The token
   goes through the same M1-D verifier and Principal resolution as REST, then workspace authorization (the
   Principal's own personal workspace, verified in the database). Failure closes with 4401 (unauthenticated,
   invalid, timed out) or 4403 (inactive user / workspace refused); an internal failure closes with 1011.
3. The verified Principal's scope is bound to the connection. Later frames never change it: a second ``auth``
   frame is refused and ``user_email`` / workspace fields are ignored. The binding ends when the token expires
   (the next frame after ``exp`` closes with 4401; clients re-authenticate by reconnecting).

Afterwards: frames larger than ``MAX_MESSAGE_BYTES`` close with 1009; one job at a time per connection; ``ping``
answers ``pong`` even while a job runs; on disconnect the running job is cancelled and awaited. Messages carry
fixed strings only (no token, claim, key, driver or exception text).

The size check runs after the frame has been received; the transport limit (uvicorn ``--ws-max-size``) still
applies to what the server will buffer.
"""

import asyncio
import json
import logging
import time

import ratelimit
from auth.errors import AuthConfigurationError, AuthenticationError, InactiveUserError
from auth.provisioning import resolve_principal
from auth.tokens import verify_access_token
from authz.errors import AuthorizationError
from authz.workspaces import authorize_personal_scope
from agentplatform.execution import ExecutionFailed, ExecutionRefused

logger = logging.getLogger("agentforge.ws")

AUTH_TIMEOUT_SECONDS = 5
MAX_MESSAGE_BYTES = 65536  # 64 KiB (approved)
CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_TOO_BIG = 1009
CLOSE_INTERNAL = 1011
CLOSE_TRY_LATER = 1013
WS_CONNECTS_PER_MINUTE = 30
WS_JOBS_PER_MINUTE = 30


async def _close(websocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001 - the peer may already be gone
        pass


async def _send(websocket, payload: dict) -> bool:
    try:
        await websocket.send_json(payload)
        return True
    except Exception:  # noqa: BLE001 - disconnected peer
        return False


def _frame_text(message) -> str | None:
    """The text of a receive() event, or None for a non-text frame."""
    text = message.get("text")
    return text if isinstance(text, str) else None


def _too_big(text: str, limit: int) -> bool:
    return len(text) > limit or len(text.encode("utf-8")) > limit


async def _authenticate(websocket, auth_config, session_factory, timeout, limit):
    """Return ``(scope, expires_at)`` or close the socket and return None."""
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=timeout)
    except asyncio.TimeoutError:
        await _close(websocket, CLOSE_UNAUTHENTICATED)
        return None
    if message.get("type") != "websocket.receive":
        return None  # the client went away
    text = _frame_text(message)
    if text is None:
        await _close(websocket, CLOSE_UNAUTHENTICATED)
        return None
    if _too_big(text, limit):
        await _close(websocket, CLOSE_TOO_BIG)
        return None
    try:
        frame = json.loads(text)
    except ValueError:
        frame = None
    if not (isinstance(frame, dict) and set(frame) == {"type", "token"} and frame["type"] == "auth"
            and isinstance(frame["token"], str) and frame["token"]):
        await _close(websocket, CLOSE_UNAUTHENTICATED)
        return None

    def verify():
        claims = verify_access_token(frame["token"], auth_config)
        principal = resolve_principal(session_factory, claims)
        return authorize_personal_scope(session_factory, principal), claims.expires_at

    try:
        return await asyncio.to_thread(verify)
    except AuthenticationError as exc:
        logger.info("WebSocket authentication rejected (%s)", exc.reason)
        await _close(websocket, CLOSE_UNAUTHENTICATED)
    except (InactiveUserError, AuthorizationError) as exc:
        logger.info("WebSocket access refused (%s)", exc.reason)
        await _close(websocket, CLOSE_FORBIDDEN)
    except AuthConfigurationError as exc:
        logger.error("WebSocket authentication is misconfigured (%s)", exc.reason)
        await _close(websocket, CLOSE_INTERNAL)
    except Exception as exc:  # noqa: BLE001 - fail closed, expose nothing
        logger.error("WebSocket authentication failed unexpectedly (%s)", type(exc).__name__)
        await _close(websocket, CLOSE_INTERNAL)
    return None


async def _run_job(websocket, executions, agent: str, task: str, scope) -> None:
    """One job through the SAME runtime as REST. Cancellation (a closed socket) is recorded as ``cancelled`` by the
    execution service before it propagates; frames carry fixed safe text only."""
    try:
        outcome = await executions.run(scope, agent, task, delegate=True)
        if outcome.extra:  # the CEO's delegation result (existing protocol)
            payload = {
                "agent": agent,
                "result": outcome.extra["primary_response"],
                "primary_agent": outcome.extra["primary_agent"],
                "primary_response": outcome.extra["primary_response"],
                "delegations": outcome.extra["delegations"],
                "execution_id": str(outcome.execution_id),
            }
        else:
            payload = {"agent": agent, "result": outcome.result, "execution_id": str(outcome.execution_id)}
    except asyncio.CancelledError:
        raise
    except ExecutionFailed as exc:
        payload = {"agent": agent, "error": exc.message, "execution_id": str(exc.execution_id)}
    except ExecutionRefused as exc:
        payload = {"agent": agent, "error": exc.message}
    except Exception as exc:  # noqa: BLE001 - never forward exception text
        logger.error("WebSocket agent run failed (%s)", type(exc).__name__)
        payload = {"agent": agent, "error": "Agent failed."}
    await _send(websocket, payload)


async def handle_websocket(websocket, *, allowed_origins, auth_config, session_factory, executions,
                           timeout=None, max_bytes=None) -> None:
    timeout = AUTH_TIMEOUT_SECONDS if timeout is None else timeout
    limit = MAX_MESSAGE_BYTES if max_bytes is None else max_bytes
    await websocket.accept()

    origin = websocket.headers.get("origin")
    if not origin or not allowed_origins or origin not in allowed_origins:
        await _close(websocket, CLOSE_FORBIDDEN)
        return

    if auth_config is None or session_factory is None:  # server-side authentication is not usable: fail closed
        await _close(websocket, CLOSE_INTERNAL)
        return

    bound = await _authenticate(websocket, auth_config, session_factory, timeout, limit)
    if bound is None:
        return
    scope, expires_at = bound
    limiter = ratelimit.get_limiter()
    wait = await asyncio.to_thread(limiter.hit, "ws_connect", str(scope.user_id), WS_CONNECTS_PER_MINUTE)  # per user, not per connection: many sockets do not multiply the budget
    if wait is not None:
        await _send(websocket, {"error": "Too many connections. Please wait a moment.", "retry_after": wait})
        await _close(websocket, CLOSE_TRY_LATER)
        return
    if not await _send(websocket, {"type": "authenticated"}):
        return

    job: asyncio.Task | None = None
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") != "websocket.receive":
                break  # disconnect
            if time.time() >= expires_at:
                await _close(websocket, CLOSE_UNAUTHENTICATED)
                break
            text = _frame_text(message)
            if text is None:
                await _send(websocket, {"error": "Invalid message."})
                continue
            if _too_big(text, limit):
                await _close(websocket, CLOSE_TOO_BIG)
                break
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if not isinstance(data, dict):
                await _send(websocket, {"error": "Invalid message."})
                continue
            kind = data.get("type")
            if kind == "ping":
                await _send(websocket, {"type": "pong"})
                continue
            if kind == "auth":  # identity is bound for the life of the connection
                await _send(websocket, {"error": "Already authenticated."})
                continue
            agent, task = data.get("agent"), data.get("task")  # user_email / workspace fields are never read
            if not isinstance(agent, str) or not isinstance(task, str) or not agent or not task:
                await _send(websocket, {"error": "Both 'agent' and 'task' are required."})
                continue
            wait = await asyncio.to_thread(limiter.hit, "ws_job", str(scope.user_id), WS_JOBS_PER_MINUTE)  # shared with every other socket of this user, in every process
            if wait is not None:
                await _send(websocket, {"error": "Too many requests. Please wait a moment.", "retry_after": wait})
                continue
            if await executions.resolve(scope, agent) is None:  # unknown, archived and foreign agents look the same
                await _send(websocket, {"error": "Unknown agent."})
                continue
            if job is not None and not job.done():
                await _send(websocket, {"error": "A job is already running on this connection."})
                continue
            job = asyncio.create_task(_run_job(websocket, executions, agent, task, scope))
    finally:
        if job is not None and not job.done():
            job.cancel()
        if job is not None:
            try:
                await job
            except asyncio.CancelledError:
                pass
