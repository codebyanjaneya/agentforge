"""FastAPI application construction with the CORS / API-docs policy (M0.1 / T0).

Deliberately imports nothing from the rest of the backend so the policy can be
tested without a database, Redis, Groq or Telegram.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import Settings

# Verified against frontend/app/**: fetch uses GET, POST, PATCH, DELETE and Content-Type only
# (PATCH: the Studio agent editor, M2-A `PATCH /agents/{id}`).
# Authorization is pre-allowed for the Bearer-token contract (T4/T9).
ALLOWED_METHODS = ["GET", "POST", "PATCH", "DELETE"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "X-Workspace-Id"]  # M6: the workspace a person selected (a request: membership is verified server-side)


class RequestSizeLimit:
    """Pure ASGI middleware: refuse a request body larger than ``max_bytes`` with 413 BEFORE the application reads it. A declared Content-Length over the limit is refused at once; a body without one (chunked)
    is counted as it streams and refused the moment it crosses the limit. This is defence in depth: the reverse proxy (``client_max_body_size``) is the first line (docs/operations.md section 17).
    WebSockets are not limited here (``realtime.py`` limits each message)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._refuse(send)
        seen, state = 0, {"refused": False, "started": False}

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes and not state["started"] and not state["refused"]:
                    state["refused"] = True
                    await self._refuse(send)  # answer NOW (frameworks may turn an exception while reading the body into a 400), then tell the app the client is gone
                    return {"type": "http.disconnect"}
            return message

        async def tracked_send(message):
            if state["refused"]:
                return  # the 413 is already on the wire: drop whatever the application tries to say afterwards
            if message["type"] == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, counted, tracked_send)
        except Exception:
            if not state["refused"]:
                raise  # not ours: the normal error handling applies

    @staticmethod
    async def _refuse(send):
        body = b'{"detail":{"code":"request_too_large","message":"The request body is too large."}}'
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})



def build_app(settings: Settings, lifespan=None) -> FastAPI:
    docs = settings.api_docs_enabled
    app = FastAPI(
        title="Multi-Agent Startup OS",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        max_age=600,
    )
    app.add_middleware(RequestSizeLimit, max_bytes=settings.max_request_bytes)
    if settings.allowed_hosts:  # Host-header allowlist (outermost): a request for another host never reaches the application
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    return app
