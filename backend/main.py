import asyncio
import hmac
import logging
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi.exceptions import RequestValidationError
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from pydantic import BaseModel
from sqlalchemy import text

from app_factory import build_app
from agentplatform.api import router as agent_platform_router, validation_error_handler
from agentplatform.execution import (
    HTTP_STATUS, AgentUnavailable, Busy, DbLimits, ExecutionFailed, ExecutionService, ExecutionStore, Limits, TaskInvalid,
)
from agentplatform.executions_api import router as executions_router
from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunRegistry, run_recovery_loop
from agentplatform.processes import ProcessRegistry
from agentplatform.tools import default_registry as default_tool_registry
from agentplatform.tools_api import router as tools_router
from agentplatform.usage_api import router as usage_router
from agentplatform.notifications_api import router as notifications_router
from agentplatform.workspaces_api import router as workspaces_router
from agentplatform.approvals_api import build_approvals_router, router as approvals_router
from agentplatform.tools.approvals import ApprovalStore
from agentplatform.demo import DemoProvider, SIMULATED_TOOL, build_demo_router
from agentplatform.usage import UsageRecorder
from agentplatform.tools.invoker import ToolInvoker, ToolStore
from agentplatform.plan_runs_api import build_plan_runs_router
from agentplatform.plans import PlanningService
from agentplatform.plans_api import build_plans_router
from agentplatform.providers import default_registry
from agentplatform import coordination
from agentplatform.coordination import Coordinator
from agentplatform.tools import email_resend
from agentplatform.resilience import RetryPolicy, wrap_registry
from auth.dependencies import get_auth_config, get_session_factory
from authz.dependencies import get_scope
from authz.errors import ScopeError
from autonomous import AutonomousExecutor
from config import get_settings
from improvement.self_improver import SelfImprover
from memory.cursor import CursorError
from memory.long_term import LongTermMemory, SessionLocal
from memory.repository import PAGE_DEFAULT, PAGE_MAX
from memory.short_term import ShortTermMemory
from migrations.schema_guard import SchemaVersionError, verify_schema_version
from orchestrator import AgentOrchestrator
from realtime import handle_websocket
import realtime
import alerting
import observability
import ratelimit
from ratelimit import limit
from tasks.scheduler import TaskScheduler
from telegram_bot import start_telegram_bot
from voice_handler import _say_response, build_incoming_response, process_recording

load_dotenv()

logger = logging.getLogger("uvicorn.error")

# Fails fast (ConfigError) on invalid configuration; names only, never values.
settings = get_settings()
observability.configure_logging(settings.log_format)
if settings.resend_api_key is not None:  # the REAL email tool exists only when explicitly configured (config.py validates it)
    email_resend.register(default_tool_registry(), email_resend.EmailConfig(api_key=settings.resend_api_key.get_secret_value(), sender=settings.email_from, allowed_domains=settings.email_allowed_domains))
realtime.WS_CONNECTS_PER_MINUTE, realtime.WS_JOBS_PER_MINUTE = settings.rate_limits["ws_connects"], settings.rate_limits["ws_jobs"]
ratelimit.configure(settings.redis_url.get_secret_value(), settings.rate_limits, settings.redis_sentinels, settings.redis_sentinel_master)  # shared per-user rate limits (Redis, with a bounded per-process fallback)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read-only schema-version guard (fails closed in every APP_ENV). It never creates, migrates
    # or stamps: migrations are a separate, explicit deployment step (`alembic upgrade head`).
    try:
        await asyncio.to_thread(verify_schema_version, settings.database_url)
    except SchemaVersionError as exc:
        logger.error("Startup aborted: %s (cause: %s)", exc, exc.error_class or "n/a")
        raise
    logger.info("Configuration loaded: %s", settings.safe_summary())
    # M4-A2: this process registers itself (so the executions it runs have a live owner) before anything is served or recovered.
    processes = ProcessRegistry(SessionLocal, plan_run_registry.owner_id, "api", plan_run_registry.config)
    try:
        await asyncio.to_thread(processes.heartbeat)
    except Exception as exc:  # noqa: BLE001 - the heartbeat loop retries
        logger.error("Could not register this process (%s)", type(exc).__name__)
    process_task = asyncio.create_task(processes.run())
    try:  # standalone executions whose process is gone (plan-run executions are closed with their run, below)
        closed = await asyncio.to_thread(ExecutionStore(SessionLocal).fail_orphaned)
        if closed:
            logger.warning("Closed %d execution(s) left running by a previous process", closed)
    except Exception as exc:  # noqa: BLE001 - never block startup on the sweep
        logger.error("Could not close interrupted executions (%s)", type(exc).__name__)
    # Plan runs (M4-A1): only runs whose lease is expired or missing are closed, so a second process never interrupts a live run.
    recovery_store = PlanRunStore(SessionLocal, owner_id=plan_run_registry.owner_id, config=plan_run_registry.config, clock=plan_run_registry.clock)
    try:
        closed_runs = await asyncio.to_thread(recovery_store.recover_expired)
        if closed_runs:
            logger.warning("Recovered %d plan run(s) without a live owner", len(closed_runs))
    except Exception as exc:  # noqa: BLE001 - the periodic pass retries
        logger.error("Could not recover plan runs (%s)", type(exc).__name__)
    recovery_task = asyncio.create_task(run_recovery_loop(recovery_store, plan_run_registry.config, executions=ExecutionStore(SessionLocal),
                                                         resumer=get_plan_run_service(SessionLocal) if plan_run_registry.embedded else None))
    for warning in settings.warnings:
        logger.warning(warning)

    alert_stop, alert_task = asyncio.Event(), None
    if settings.alert_webhook_url is not None:  # LIVE alerting only when a webhook is configured; otherwise rules stay definitions
        manager = alerting.AlertManager(alerting.WebhookNotifier(settings.alert_webhook_url.get_secret_value()), alerting.Signals(), claims=alerting.RedisClaims(ratelimit.redis_client()))

        def alert_gauges() -> dict:
            return {**collect_gauges(), **alerting.read_backup_status(settings.backup_status_file, time.time())}

        alert_task = asyncio.create_task(alerting.run_forever(manager, alert_gauges, settings.alert_eval_interval, alert_stop))
        logger.info("Alert evaluation started (every %ss).", settings.alert_eval_interval)
    bot_task: asyncio.Task | None = None
    if settings.telegram_enabled:
        bot_task = asyncio.create_task(start_telegram_bot())
        logger.info("Telegram bot launched as a background task.")
    else:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set; skipping Telegram bot startup."
        )
    try:
        yield
    finally:
        alert_stop.set()
        if alert_task is not None:
            await alert_task
        recovery_task.cancel()
        try:
            await recovery_task
        except asyncio.CancelledError:
            pass
        stuck = await plan_run_registry.shutdown()  # no new runs; drivers stop, record "interrupted" and only then release their leases
        if stuck:
            logger.warning("%d plan run driver(s) did not stop in time; their leases will expire and be recovered", len(stuck))
        process_task.cancel()
        try:
            await process_task
        except asyncio.CancelledError:
            pass
        try:
            await asyncio.to_thread(processes.release)  # only now: this process can no longer write
        except Exception as exc:  # noqa: BLE001 - the row simply expires
            logger.warning("Could not release the process row (%s)", type(exc).__name__)
        if bot_task is not None:
            bot_task.cancel()
            try:
                await bot_task
            except asyncio.CancelledError:
                pass


app = build_app(settings, lifespan)

orchestrator = AgentOrchestrator()
executor = AutonomousExecutor(orchestrator)
scheduler = TaskScheduler()
improver = SelfImprover()
long_term = LongTermMemory()
short_term = ShortTermMemory()
providers = wrap_registry(default_registry(), RetryPolicy.from_env(settings.provider_tuning), Coordinator(ratelimit.redis_client(), fail_closed=settings.provider_redis_down == "closed", replica_ack_ms=100 if settings.redis_sentinels else 0))  # providers are created lazily (no credentials are read at import); M6b: bounded retry with backoff around the REAL provider
if settings.demo_mode:  # DEMO MODE (never in production, enforced in config): a SCRIPTED model and a SIMULATED sending tool; everything else is the real product
    from agentplatform.providers import ProviderRegistry as _PR
    from agentplatform.tools.fixtures import register_demo_tools

    providers = _PR({"groq": lambda: DemoProvider()})
    register_demo_tools(default_tool_registry())
    logger.warning("DEMO MODE: the model is scripted and %s is simulated. This is not a live deployment.", SIMULATED_TOOL)
execution_limits = Limits()  # process-wide concurrency limits shared by REST and WebSocket


def build_execution_service(session_factory, owner_id: str | None = None) -> ExecutionService:
    """The one runtime behind POST /autonomous/run and /ws. Collaborators are read at call time (tests replace them). Every execution
    it creates is owned by this process (``owner_id``) and admitted against the database-wide limits (M4-A2)."""
    return ExecutionService(session_factory, orchestrator=orchestrator, providers=providers, short_term=short_term,
                            long_term=long_term, limits=execution_limits, owner_id=owner_id or plan_run_registry.owner_id, db_limits=DbLimits(),
                            tools=ToolInvoker(ToolStore(session_factory), default_tool_registry(), ApprovalStore(session_factory, policy_fingerprint=default_tool_registry().policy.fingerprint)),
                            usage=UsageRecorder(session_factory))


def get_execution_service(session_factory=Depends(get_session_factory)) -> ExecutionService:
    return build_execution_service(session_factory)


def build_planning_service(session_factory) -> PlanningService:
    """Planning (M3-A) reuses the M2-B provider registry and the shared concurrency limits; it never executes anything."""
    return PlanningService(session_factory, providers=providers, limits=execution_limits, usage=UsageRecorder(session_factory))


def get_planning_service(session_factory=Depends(get_session_factory)) -> PlanningService:
    return build_planning_service(session_factory)


plan_run_registry = RunRegistry(embedded=settings.run_execution == "embedded")  # the live plan-run schedulers of this process (M3-B); api-only: runs are recorded for a worker


def get_plan_run_service(session_factory=Depends(get_session_factory)) -> PlanRunService:
    """Plan execution (M3-B) runs every task through the one M2-B ExecutionService (same limits, records and safe errors)."""
    return PlanRunService(session_factory, executions=build_execution_service(session_factory), registry=plan_run_registry)


class AutonomousRequest(BaseModel):
    """No identity field: the caller is the verified Principal; any extra body field is ignored."""

    agent: str
    task: str


class ScheduleRequest(BaseModel):
    agent: str
    task: str
    interval_minutes: int


def _require_agent(name: str) -> None:
    if name not in orchestrator.agent_names:
        raise HTTPException(status_code=404, detail="Unknown agent")


app.include_router(agent_platform_router)  # M2-A: templates and custom agents (every route uses get_scope)
app.include_router(executions_router)  # M2-B: execution history (every route uses get_scope)
app.include_router(build_demo_router(settings.demo_mode))  # M5: demo status and the starter workforce (every route uses get_scope)
app.include_router(approvals_router)  # M4-C: human approval of exact tool invocations (every route uses get_scope)
app.include_router(build_approvals_router(get_plan_run_service))  # the decision routes (owner/admin only); a decision wakes a process that can resume the suspended task
app.include_router(tools_router)  # M4-B: tool catalog and tool-invocation history (read-only; every route uses get_scope)
app.include_router(usage_router)  # M6: per-run model usage and cost (read-only; get_scope)
app.include_router(notifications_router)  # M6: in-app notifications and the inbox badge count (get_scope)
app.include_router(workspaces_router)  # M6: list/create workspaces, add members (owner only)
app.include_router(build_plans_router(get_planning_service))  # M3-A: plans (every route uses get_scope; nothing is executed)
app.include_router(build_plan_runs_router(get_plan_run_service))  # M3-B: execute approved plans (every route uses get_scope)
app.add_exception_handler(RequestValidationError, validation_error_handler)  # 422 bodies never echo submitted values


@app.get("/health")
async def health():
    return {"status": "ok"}


def collect_gauges() -> dict:
    """Shared-state gauges read from the database (the same from every process) plus this process's pool; used by /metrics and by the alert evaluator."""
    out = {}
    try:
        with SessionLocal() as session:
            for name, sql in (("runs_running", "SELECT count(*) FROM plan_runs WHERE status = 'running'"),
                              ("dispatch_unresolved", "SELECT count(*) FROM tool_dispatches WHERE state = 'unknown'"),
                              ("approvals_pending", "SELECT count(*) FROM tool_approvals WHERE status = 'pending'"),
                              ("workers_registered", "SELECT count(*) FROM worker_processes")):
                out[name] = session.execute(text(sql)).scalar() or 0
    except Exception:  # noqa: BLE001 - a failed gauge query is reported as an absent gauge, never an error page
        logger.warning("metrics gauges unavailable")
    cap = coordination.cap_enforced()
    if cap is not None:
        out["provider_global_cap_enforced"] = cap  # 0 = the global provider concurrency cap is NOT being enforced right now (outage, failover quarantine, or explicit degrade)
    out["rate_limit_shared_enforced"] = 0 if ratelimit.get_limiter().degraded else 1
    try:
        from memory.long_term import engine

        out["db_pool_checked_out"] = engine.pool.checkedout()
        out["db_pool_size"] = engine.pool.size() + getattr(engine.pool, "_max_overflow", 0)
    except Exception:  # noqa: BLE001 - a pool without these counters (e.g. SQLite) simply has no gauge
        pass
    return out


@app.middleware("http")
async def http_metrics(request: Request, call_next):
    """Request count and latency by ROUTE TEMPLATE (``/plans/{plan_id}``, never the raw path; an unmatched path is one label), by status class. No user, workspace or run id ever reaches a label."""
    started, status = time.perf_counter(), 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", None) or "unmatched"
        if path not in ("/metrics", "/health", "/ready"):  # probes and scrapes would drown the signal
            observability.inc("http_requests_total", route=path, status=f"{status // 100}xx")
            observability.observe("http_request_seconds", time.perf_counter() - started, route=path)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    """Prometheus text metrics of THIS process plus shared-state gauges read from the database. Exists only when METRICS_TOKEN is configured; bearer-protected; aggregate numbers only."""
    token = settings.metrics_token.get_secret_value() if settings.metrics_token is not None else None
    supplied = request.headers.get("authorization", "")
    if token is None or not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=404)  # the same answer whether the endpoint is disabled or the token is wrong

    return Response(observability.render(await asyncio.to_thread(collect_gauges)), media_type="text/plain; version=0.0.4")


@app.get("/ready")
async def ready(response: Response):
    """Readiness (can this process serve traffic: is the database reachable?), distinct from liveness. Reports no detail on failure."""
    def probe() -> None:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))

    try:
        await asyncio.to_thread(probe)
    except Exception:  # noqa: BLE001 - any failure means not ready; the cause goes to the log, never to the caller
        observability.inc("readiness_failures_total")
        logging.getLogger("agentforge.ready").exception("readiness probe failed")
        response.status_code = 503
        return {"status": "unavailable"}
    return {"status": "ready"}


# Every user-specific route below depends on get_scope = verified bearer Principal + database-verified workspace
# membership (M1-D/M1-E). Identity never comes from the body, query, path or headers.


@app.post("/autonomous/run", dependencies=[Depends(limit("agent_run", 30))])
async def autonomous_run(payload: AutonomousRequest, scope=Depends(get_scope), service: ExecutionService = Depends(get_execution_service)):
    """Runs a built-in or custom agent through the shared runtime. The response keeps its original fields and adds the
    execution record's id/status and the agent identity (and version, for a custom agent)."""
    try:
        outcome = await service.run(scope, payload.agent, payload.task)
    except AgentUnavailable:
        raise HTTPException(status_code=404, detail="Unknown agent") from None
    except TaskInvalid as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": exc.message}) from None
    except Busy as exc:
        raise HTTPException(status_code=429, detail={"code": exc.code, "message": exc.message}, headers={"Retry-After": "5"}) from None
    except ExecutionFailed as exc:
        raise HTTPException(status_code=HTTP_STATUS.get(exc.code, 500),
                            detail={"code": exc.code, "message": exc.message, "execution_id": str(exc.execution_id)}) from None
    return {
        **executor.describe(payload.agent, payload.task, outcome.result),
        "execution_id": str(outcome.execution_id),
        "status": outcome.status,
        "agent_source": outcome.agent_source,
        "agent_version": outcome.agent_version,
    }


@app.post("/tasks/schedule")
def schedule_task(payload: ScheduleRequest, scope=Depends(get_scope)):
    _require_agent(payload.agent)
    return scheduler.add_task(scope, payload.agent, payload.task, payload.interval_minutes)


@app.get("/tasks")
def list_tasks(scope=Depends(get_scope)):
    return scheduler.get_scheduled_tasks(scope)


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str, scope=Depends(get_scope)):
    removed = scheduler.remove_task(scope, task_id)
    return {"removed": removed, "id": task_id}


@app.post("/improve/{agent_name}", dependencies=[Depends(limit("improve", 6))])
async def run_improvement(agent_name: str, scope=Depends(get_scope)):
    _require_agent(agent_name)
    return await improver.improve(agent_name, scope)


@app.get("/improve/{agent_name}")
def get_improvement(agent_name: str, scope=Depends(get_scope)):
    _require_agent(agent_name)
    return improver.get_improvements(agent_name, scope)


@app.get("/history")
def get_history(
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
    cursor: str | None = Query(default=None, max_length=300),
    scope=Depends(get_scope),
):
    """The caller's own history, newest first, in pages (default 50, max 100) with an opaque cursor."""
    try:
        return long_term.get_history_page(scope, limit, cursor)
    except CursorError:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


# --- Twilio service webhooks: PUBLIC and NOT SECURE until S2 (official signature validation + service
# principal). They are an isolated temporary boundary: no user Principal is attached and user memory is never
# reachable from here. With no scope available the voice agent run fails closed (see below).


@app.post("/voice/incoming")
async def voice_incoming():
    return Response(
        content=build_incoming_response(),
        media_type="application/xml",
    )


@app.post("/voice/process")
async def voice_process(request: Request):
    form = await request.form()
    recording_url = form.get("RecordingUrl", "")
    try:
        twiml = await process_recording(recording_url, orchestrator)
    except ScopeError:  # pending S2: voice has no memory scope; say so instead of a raw server error
        logger.error("Voice request refused: no memory scope (pending S2)")
        twiml = _say_response("Sorry, the voice assistant is not available right now. Goodbye.")
    return Response(content=twiml, media_type="application/xml")


def _resolve_for_ws(websocket: WebSocket, dependency):
    """Resolve an auth dependency (honouring test overrides) without letting its HTTPException abort the handshake:
    an unavailable dependency becomes None and the handler closes with 1011 after the Origin check."""
    try:
        return websocket.app.dependency_overrides.get(dependency, dependency)()
    except Exception as exc:  # noqa: BLE001 - never expose the cause
        logger.error("WebSocket dependency unavailable (%s)", type(exc).__name__)
        return None


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    session_factory = _resolve_for_ws(websocket, get_session_factory)
    await handle_websocket(
        websocket,
        allowed_origins=settings.allowed_origins,
        auth_config=_resolve_for_ws(websocket, get_auth_config),
        session_factory=session_factory,
        executions=build_execution_service(session_factory) if session_factory is not None else None,
    )
