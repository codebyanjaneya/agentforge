"""Per-user rate limiting for the expensive and sensitive routes and the WebSocket (sprint 2, P2).

SHARED STATE: a fixed window counter in Redis (INCR + EXPIRE, one atomic pipeline), the store the application already depends on, so every API process of a deployment shares one budget. The key is
(bucket, user): never the connection, so opening many connections or sockets does not multiply a user's budget. Health and readiness are not limited.

FAILURE MODE: if Redis cannot be reached the limiter falls back to a bounded per-process counter (still protective, no longer shared) and does not retry Redis for ``RETRY_REDIS_AFTER`` seconds, so an outage
neither blocks requests nor adds latency. It fails OPEN to the local counter, never to "unlimited"; the fallback is logged once per outage.

LIMITS OF A FIXED WINDOW: a caller can burst up to 2x the limit across a window boundary. Retry-After is the time left in the current window. Unauthenticated floods are not counted here (the identity is
unknown before authentication): that belongs to the reverse proxy.
"""

import observability
import asyncio
import logging
import math
import time

from fastapi import Depends, HTTPException

from authz.dependencies import get_scope

logger = logging.getLogger("agentforge.ratelimit")
RETRY_REDIS_AFTER = 30.0
MAX_LOCAL_KEYS = 20000


class RateLimiter:
    def __init__(self, client=None, clock=time.time):
        self.client, self.clock = client, clock
        self.degraded = False  # True while the shared (Redis) budget could not be used: only the per-process counter is limiting
        self._local: dict = {}
        self._redis_down_until = 0.0

    def hit(self, bucket: str, key: str, limit: int, window: int = 60) -> int | None:
        """Count one request. ``None`` = allowed; otherwise the whole seconds to wait (Retry-After)."""
        now = self.clock()
        window_id = int(now // window)
        wait = max(1, math.ceil(window - (now % window)))
        count = self._redis_count(bucket, key, window, window_id, now)
        if count is None:
            count = self._local_count(bucket, key, window_id)
        return None if count <= limit else wait

    def _redis_count(self, bucket, key, window, window_id, now):
        if self.client is None or now < self._redis_down_until:
            return None
        name = f"rl:{bucket}:{key}:{window_id}"
        try:
            pipe = self.client.pipeline()
            pipe.incr(name)
            pipe.expire(name, window + 1)
            count = int(pipe.execute()[0])
            self.degraded = False
            return count
        except Exception:  # noqa: BLE001 - any Redis failure: use the local counter for a while
            self.degraded = True
            observability.inc("redis_unavailable_total", feature="rate_limit", mode="degrade")
            if self._redis_down_until == 0.0 or now >= self._redis_down_until:
                logger.warning("rate limit store unavailable; using the per-process counter for %ss", RETRY_REDIS_AFTER)
            self._redis_down_until = now + RETRY_REDIS_AFTER
            return None

    def _local_count(self, bucket, key, window_id) -> int:
        if len(self._local) > MAX_LOCAL_KEYS:  # bounded memory: drop everything from earlier windows
            self._local = {k: v for k, v in self._local.items() if v[0] == window_id}
            if len(self._local) > MAX_LOCAL_KEYS:
                self._local.clear()
        current = self._local.get((bucket, key))
        count = (current[1] if current and current[0] == window_id else 0) + 1
        self._local[(bucket, key)] = (window_id, count)
        return count


_limiter = RateLimiter()


_limits: dict = {}


def configure(redis_url: str | None, limits: dict | None = None, sentinels: tuple = (), master: str | None = None) -> None:
    """Called once at start-up with the configured Redis URL (short timeouts: a slow Redis must not slow the API) and the validated per-bucket limits (config.RATE_LIMIT_DEFAULTS + overrides)."""
    global _limiter, _limits
    _limits = dict(limits or {})
    client = None
    if redis_url:
        import redis_client

        client = redis_client.build(redis_url, sentinels, master)  # a single node, or the current master of a Sentinel group
    _limiter = RateLimiter(client)


def redis_client():
    """The short-timeout Redis client the limiter was configured with (shared with provider coordination), or None."""
    return _limiter.client


def get_limiter() -> RateLimiter:
    return _limiter


def too_many(retry_after: int) -> HTTPException:
    return HTTPException(status_code=429, detail={"code": "too_many_requests", "message": "Too many requests. Please wait a moment and try again."}, headers={"Retry-After": str(retry_after)})


def limit(bucket: str, per_minute: int, window: int = 60):
    """A route dependency: at most ``per_minute`` calls of this bucket per user per window, shared across processes. Runs after authentication (get_scope is resolved once per request)."""

    async def check(scope=Depends(get_scope), limiter: RateLimiter = Depends(get_limiter)) -> None:
        wait = await asyncio.to_thread(limiter.hit, bucket, str(scope.user_id), _limits.get(bucket, per_minute), window)  # the configured value, else the route default
        if wait is not None:
            observability.inc("rate_limited_total", bucket=bucket)
            logger.info("rate limited bucket=%s", bucket)  # never the user id: identity stays out of the logs
            raise too_many(wait)

    return check
