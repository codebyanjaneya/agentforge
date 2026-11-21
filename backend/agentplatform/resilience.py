"""Provider resilience (M6b): bounded retry with backoff for the MODEL PROVIDER, independent of which provider it is.

WHAT IS RETRIED. Only a provider REQUEST that failed in a transient way (``rate_limited`` HTTP 429, ``unavailable``, ``timeout``). A model completion has no side effect, so repeating it is
safe. A TOOL invocation is never retried here (a tool that may have taken effect and lost its process is closed ``uncertain`` by the invoker and is never re-run). Permanent failures
(``auth``, ``bad_request``, ``unsupported_model``, ``invalid_response`` ...) are raised at once.

HOW. Up to ``max_attempts`` tries; between tries a wait of the provider's own ``Retry-After`` when it gave one, otherwise bounded exponential backoff, always with jitter (so concurrent
callers do not all retry on the same tick), never more than ``max_wait`` per wait and ``budget`` in total. A ``Retry-After`` longer than ``max_wait`` is NOT slept through: the request ends as
exhausted at once (waiting minutes inside a task would only hold a slot). A provider-wide COOLDOWN is set from every 429 so other callers hold their requests instead of hammering a
provider that just said stop (no retry storm), and a per-provider CONCURRENCY limit bounds simultaneous requests. Nothing is ever replaced by a script: when the budget is spent the
original error is raised and the task fails honestly with ``rate_limited`` (or ``unavailable``/``timeout``), which the explicit run retry can recover while reusing finished tasks.

OBSERVABILITY. Every wait, retry, recovery and the final failure is reported to a sink found in a context variable (set by the runtime around its calls) so it can be persisted with the
run: kinds ``waiting`` (about to sleep), ``retrying`` (the next try starts), ``recovered`` (it worked after at least one retry), ``exhausted`` (the retry budget of a rate limit is spent),
``failed`` (any other final failure, including a transient one that exhausted its attempts, and permanent ones)."""

import uuid
import observability
import asyncio
import contextvars
import logging
import random
import time
from dataclasses import dataclass

from agentplatform.providers import ProviderError

logger = logging.getLogger("agentforge.resilience")

TRANSIENT = ("rate_limited", "unavailable", "timeout", "malformed_tool_call")

# who is asking, and where to report what happened (set by the runtime; absent = nothing is persisted)
CONTEXT: contextvars.ContextVar = contextvars.ContextVar("provider_context", default=None)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 5          # first try + 4 retries
    base_delay: float = 1.0        # seconds before the first retry (without Retry-After)
    factor: float = 2.0
    max_wait: float = 90.0         # the longest single wait; a longer Retry-After is not slept through (a free-tier token-per-minute window can ask for ~60 s)
    budget: float = 240.0          # the most time spent waiting in total for one request
    jitter: float = 0.25           # +-25% on every wait
    concurrency: int = 2           # simultaneous requests to this provider (per event loop)
    cooldown_cap: float = 90.0     # the longest provider-wide cooldown a 429 can set
    global_concurrency: int = 8    # simultaneous requests to this provider across ALL processes (needs a coordinator)
    admission_wait: float = 30.0   # the longest a request waits for a global slot before it fails (never queued forever, never retried)
    lease_seconds: float = 90.0    # a slot lease AND the post-failover quarantine: must exceed the longest single provider call (60 s default timeout) plus margin; a process that dies holding one frees it by expiry

    @classmethod
    def from_env(cls, environ) -> "RetryPolicy":
        """Defaults, overridable per deployment (``PROVIDER_MAX_ATTEMPTS``, ``PROVIDER_BASE_DELAY``, ``PROVIDER_MAX_WAIT``, ``PROVIDER_BUDGET``, ``PROVIDER_CONCURRENCY``); a bad value is ignored."""
        env = environ  # the PROVIDER_* values collected by config.py (Settings.provider_tuning); this module never reads the process environment
        out = {}
        for field, key, kind in (("max_attempts", "PROVIDER_MAX_ATTEMPTS", int), ("base_delay", "PROVIDER_BASE_DELAY", float), ("max_wait", "PROVIDER_MAX_WAIT", float),
                                 ("budget", "PROVIDER_BUDGET", float), ("concurrency", "PROVIDER_CONCURRENCY", int), ("global_concurrency", "PROVIDER_GLOBAL_CONCURRENCY", int),
                                 ("admission_wait", "PROVIDER_ADMISSION_WAIT", float), ("lease_seconds", "PROVIDER_LEASE_SECONDS", float)):
            try:
                value = kind(env[key])
                if value > 0:
                    out[field] = value
            except (KeyError, ValueError):
                pass
        return cls(**out)

    def delay(self, retry_number: int, retry_after: float | None, rng=random.random) -> float:
        """The wait before retry ``retry_number`` (1-based). The provider's hint wins over the schedule; jitter only ever ADDS up to ``jitter`` to a hint (never waits less than asked)."""
        if retry_after is not None:
            return retry_after * (1 + self.jitter * rng())
        raw = min(self.base_delay * self.factor ** (retry_number - 1), self.max_wait)
        return raw * (1 - self.jitter + 2 * self.jitter * rng())


class ResilientProvider:
    """Wraps ANY provider with the policy above. Same interface, same ``name``."""

    def __init__(self, inner, policy: RetryPolicy | None = None, *, sleep=asyncio.sleep, clock=time.monotonic, rng=random.random, coordinator=None):
        self._inner, self.policy, self._sleep, self._clock, self._rng = inner, policy or RetryPolicy(), sleep, clock, rng
        self._coord, self._pname = coordinator, str(getattr(inner, "name", "provider"))  # shared cooldown + global slots (None: per-process behaviour only)
        self.name = getattr(inner, "name", "provider")
        self._cooldown_until = 0.0
        self._semaphores: dict = {}  # per event loop: an asyncio semaphore belongs to the loop that first used it

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        return self._semaphores.setdefault(id(loop), asyncio.Semaphore(max(1, self.policy.concurrency)))

    @staticmethod
    async def _report(kind: str, **fields) -> None:
        ctx = CONTEXT.get()
        sink = ctx.get("sink") if isinstance(ctx, dict) else None
        if sink is None:
            return
        try:
            await asyncio.to_thread(sink, kind, ctx, fields)
        except Exception as exc:  # noqa: BLE001 - reporting must never change the outcome of a request
            logger.error("Could not record a provider event (%s)", type(exc).__name__)

    async def _admit(self):
        """A global slot (lease token), waiting at most ``admission_wait`` seconds in short polls; None when no coordinator is configured. Out of time: the request FAILS as rate_limited (reported), it is not retried."""
        if self._coord is None:
            return None
        waited, policy = 0.0, self.policy
        while True:
            token = await asyncio.to_thread(self._coord.acquire, self._pname, max(1, policy.global_concurrency), policy.lease_seconds)
            if token is not None:
                return token
            if waited >= policy.admission_wait:
                await self._report("failed", attempt=1, error_code="rate_limited", detail="admission_timeout")
                raise ProviderError("rate_limited")
            step = 0.1 + 0.2 * self._rng()
            await self._sleep(step)
            waited += step

    async def complete(self, request):
        policy, spent, attempt = self.policy, 0.0, 0
        observability.set_context(provider_request=uuid.uuid4().hex[:16])  # one id per logical model request (all its retries share it)
        while True:
            attempt += 1
            hold = self._cooldown_until - self._clock()
            if self._coord is not None:
                hold = max(hold, await asyncio.to_thread(self._coord.cooldown_remaining, self._pname))  # a 429 seen by ANY process
            if hold > 0:  # another request just got a 429: wait with it instead of adding to the storm
                await self._sleep(hold)
            token = await self._admit()
            try:
                try:
                    async with self._semaphore():
                        response = await self._inner.complete(request)
                finally:
                    if token is not None:
                        await asyncio.to_thread(self._coord.release, self._pname, token)
            except ProviderError as error:
                if error.code not in TRANSIENT:
                    await self._report("failed", attempt=attempt, error_code=error.code)
                    raise
                wait = policy.delay(attempt, error.retry_after, self._rng)
                if error.code == "rate_limited":
                    self._cooldown_until = max(self._cooldown_until, self._clock() + min(wait, policy.cooldown_cap))
                    if self._coord is not None:
                        await asyncio.to_thread(self._coord.set_cooldown, self._pname, min(wait, policy.cooldown_cap))
                out_of_attempts = attempt >= policy.max_attempts
                too_long = error.retry_after is not None and error.retry_after > policy.max_wait
                if out_of_attempts or too_long or spent + wait > policy.budget:
                    await self._report("exhausted" if error.code == "rate_limited" else "failed", attempt=attempt, error_code=error.code,
                                       detail="retry_after_too_long" if too_long else "retry_budget_spent", retry_after=error.retry_after,
                                       wait_ms=int(error.retry_after * 1000) if error.retry_after is not None else None)  # for an exhausted request wait_ms is what the provider ASKED us to wait
                    raise
                await self._report("waiting", attempt=attempt, error_code=error.code, wait_ms=int(wait * 1000), retry_after=error.retry_after)
                await self._sleep(wait)
                spent += wait
                await self._report("retrying", attempt=attempt + 1, error_code=error.code)
                continue
            if attempt > 1:
                await self._report("recovered", attempt=attempt)
            return response


def wrap_registry(registry, policy: RetryPolicy | None = None, coordinator=None):
    """The same registry, whose providers (created lazily as before) are wrapped. Used only where the REAL provider is configured; tests that inject fakes are untouched."""
    from agentplatform.providers import ProviderRegistry

    return ProviderRegistry({name: (lambda factory=factory: ResilientProvider(factory(), policy, coordinator=coordinator)) for name, factory in registry._factories.items()})


def make_sink(session_factory, *, clock=None):
    """The sink the runtime puts in ``CONTEXT``: persists one ``provider_events`` row (best effort; a failure is logged by type only and never changes the request's outcome)."""
    from datetime import datetime

    from memory.models import ProviderEvent

    now = clock or datetime.utcnow

    def sink(kind: str, ctx: dict, fields: dict) -> None:
        scope = ctx["scope"]
        observability.inc("provider_events_total", kind=kind, provider=str(ctx.get("provider") or "provider")[:32])
        if fields.get("wait_ms"):
            observability.observe("provider_wait_seconds", fields["wait_ms"] / 1000)
        with session_factory() as session, session.begin():
            session.add(ProviderEvent(user_id=scope.user_id, workspace_id=scope.workspace_id, plan_id=ctx.get("plan_id"), run_id=ctx.get("run_id"), task_key=ctx.get("task_key"),
                                      execution_id=ctx.get("execution_id"), provider=str(ctx.get("provider") or "provider")[:32], kind=kind, attempt=int(fields.get("attempt") or 1),
                                      error_code=fields.get("error_code"), wait_ms=fields.get("wait_ms"), detail=fields.get("detail"), created_at=now()))

    return sink
