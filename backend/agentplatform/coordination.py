"""Cross-process provider coordination (sprint 2, P4; reliability policy rewritten in sprint 4.1): ONE provider cooldown and ONE global concurrency cap shared by every API/worker process.

WHY REDIS (and not the database): the state is tiny, short-lived and hot (a cooldown lasts seconds, a slot lasts one model request); Redis is already a required dependency, and its server clock (``TIME``
inside the Lua scripts) is the single timebase, so processes with skewed clocks agree. Every admission is one atomic Lua call.

* COOLDOWN: ``set_cooldown(provider, seconds)`` extends (never shortens) a key with a millisecond TTL; ``cooldown_remaining`` reads it. A 429 seen by any process makes every process wait.
* SLOTS: a sorted set of leases scored by expiry. ``acquire`` prunes expired leases and adds one only while fewer than ``limit`` are live; a holder that dies is released by its lease expiring.
* BOUNDED WAITING is the caller's job (``ResilientProvider``): it polls ``acquire`` for at most ``admission_wait`` seconds and then fails the request.

THE INVARIANT AND WHAT IT COSTS. The global cap is a promise that no more than ``limit`` model requests are in flight across ALL processes. That promise can only be made while the shared accounting is trustworthy.
It is NOT trustworthy when (a) Redis is unreachable, or (b) Redis has moved or restarted and the slot accounting may have been lost: Redis replication is asynchronous, so a promoted replica can lack the most
recent leases (and a restarted master may have none), after which holders that are still running are no longer counted. In either case this module does not pretend:
  * ``closed`` (the DEFAULT, ``PROVIDER_REDIS_DOWN=closed``): admission is REFUSED. Requests wait for ``admission_wait`` and then fail as ``rate_limited``; the provider is never called while the cap cannot be
    guaranteed. A cap that is not enforced is never silently replaced by a per-process one.
  * ``degrade`` (explicit opt-in, ``PROVIDER_REDIS_DOWN=degrade``): admission falls back to the per-process limit ONLY. The global cap is NOT guaranteed; the process says so (a log line per outage, the metrics
    ``agentforge_provider_admission_total{decision="degraded"}``, ``agentforge_redis_unavailable_total{mode="degrade"}`` and the gauge ``agentforge_provider_global_cap_enforced`` = 0, alert
    ``AgentForgeProviderCapNotEnforced``). Choose it only if exceeding the provider quota is acceptable.
SERVER MOVE / RESTART DETECTION (works for brand-new processes too): the admission script compares the Redis server's ``run_id`` with the one stored in ``provider:origin``. If they differ (failover to a replica,
restart, restore), the script writes a ``provider:quarantine`` key lasting one full lease term (the longest any unaccounted holder can still be running) and refuses admission for every process until it expires,
then re-admits. ``provider:origin`` itself being lost together with the slots is indistinguishable from a first start: a residual risk, reduced (not removed) by the replica acknowledgement below and by running Redis
with ``min-replicas-to-write 1`` and ``min-replicas-max-lag 5``.
REPLICA ACKNOWLEDGEMENT (``replica_ack_ms`` > 0, used automatically with Sentinel): after a grant the coordinator issues ``WAIT 1 <ms>``; if no replica acknowledged the write the slot is released and admission is refused,
so every slot that was granted existed on at least one replica at that moment (when NO replica is connected there is nothing to acknowledge and nothing a failover could lose it to: the grant stands and a restart is caught by the origin check). This narrows the window in which a failover can lose an acknowledged lease; it cannot reduce it to zero (a replica other than the
promoted one may have acknowledged).
WHAT THIS DOES NOT TOUCH: run ownership, leases of plan runs, fencing and every side-effect protection live in the DATABASE (D051-D055, D066-D068); a Redis failure or failover can neither let a stale worker
dispatch nor duplicate a side effect. Only MODEL requests are coordinated here; tool invocations are never touched or retried.
"""

import logging
import time
import uuid

import observability

logger = logging.getLogger("agentforge.coordination")
RETRY_AFTER = 15.0
INSTANCES: list = []  # every Coordinator of this process (for the "cap enforced" gauge)

# Keys: slots, origin (the server that created the accounting), quarantine. Returns {code, run_id}: 1 granted, 0 no free slot, 2 quarantined (the server moved: accounting not trusted for one lease term).
_ACQUIRE = """
local info = redis.call('INFO', 'server')
local rid = string.match(info, 'run_id:(%w+)') or 'unknown'
local origin = redis.call('GET', KEYS[2])
if not origin then
  redis.call('SET', KEYS[2], rid)
elseif origin ~= rid then
  redis.call('SET', KEYS[2], rid)
  redis.call('SET', KEYS[3], rid, 'PX', ARGV[1])
end
if redis.call('EXISTS', KEYS[3]) == 1 then return {2, rid} end
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[1]), ARGV[3])
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  return {1, rid}
end
return {0, rid}
"""
_COOLDOWN = """
local cur = redis.call('PTTL', KEYS[1])
if cur < tonumber(ARGV[1]) then redis.call('SET', KEYS[1], '1', 'PX', ARGV[1]) end
return 1
"""


def cap_enforced() -> int | None:
    """1 when every coordinator of this process currently trusts its shared accounting, 0 when any does not, None when this process has no coordinator."""
    if not INSTANCES:
        return None
    return 1 if all(c.enforced for c in INSTANCES) else 0


class Coordinator:
    def __init__(self, client, *, clock=time.monotonic, fail_closed: bool = True, replica_ack_ms: int = 0):
        self.client, self._clock, self._down_until, self.fail_closed, self.replica_ack_ms = client, clock, 0.0, fail_closed, replica_ack_ms
        self.enforced = client is not None  # False while the shared accounting is not trustworthy (outage, server moved, replica did not acknowledge)
        self._acquire = client.register_script(_ACQUIRE) if client is not None else None
        self._set_cooldown = client.register_script(_COOLDOWN) if client is not None else None
        if client is not None:
            INSTANCES.append(self)

    @property
    def mode(self) -> str:
        return "closed" if self.fail_closed else "degrade"

    def _guard(self, fn, default):
        if self.client is None or self._clock() < self._down_until:
            return default
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - the store is unreachable: the caller's policy decides
            observability.inc("redis_unavailable_total", feature="provider_coordination", mode=self.mode)
            if self._down_until == 0.0 or self._clock() >= self._down_until:
                logger.warning("provider coordination store unavailable (%s): global provider cap %s for %ss", type(exc).__name__,
                               "NOT ENFORCED, requests are refused (closed)" if self.fail_closed else "NOT ENFORCED, per-process limits only (degrade)", RETRY_AFTER)
            self._down_until = self._clock() + RETRY_AFTER
            return default

    def cooldown_remaining(self, provider: str) -> float:
        ms = self._guard(lambda: self.client.pttl(f"provider:cooldown:{provider}"), -2)
        return max(0.0, ms / 1000.0) if isinstance(ms, int) and ms > 0 else 0.0

    def set_cooldown(self, provider: str, seconds: float) -> None:
        self._guard(lambda: self._set_cooldown(keys=[f"provider:cooldown:{provider}"], args=[int(seconds * 1000)]), None)

    def _decide(self, reason: str, token: str) -> str | None:
        """The accounting cannot be trusted right now (``reason``): refuse (closed) or admit per process only (degrade). Never silent."""
        self.enforced = False
        observability.inc("provider_admission_total", decision="refused" if self.fail_closed else "degraded", reason=reason)
        return None if self.fail_closed else token

    def acquire(self, provider: str, limit: int, lease_seconds: float) -> str | None:
        """A lease token when a global slot was granted; None when admission is refused (no free slot, or the shared accounting cannot be trusted and the mode is ``closed``)."""
        token = uuid.uuid4().hex
        if self.client is None:
            return token  # no shared store configured at all: no global cap is claimed (a deployment without Redis)
        result = self._guard(lambda: self._acquire(keys=[f"provider:slots:{provider}", "provider:origin", "provider:quarantine"], args=[int(lease_seconds * 1000), int(limit), token]), None)
        if result is None:
            return self._decide("store_unavailable", token)
        code = int(result[0])
        if code == 2:
            logger.error("the Redis server changed or restarted: provider slot accounting may be lost; admission is %s for one lease term", "REFUSED" if self.fail_closed else "per-process only (degrade)")
            return self._decide("server_moved", token)
        if code == 0:
            self.enforced = True
            observability.inc("provider_admission_total", decision="full", reason="cap_reached")
            return None
        if self.replica_ack_ms > 0:
            acked = self._guard(lambda: self.client.execute_command("WAIT", 1, self.replica_ack_ms), 0)
            if not acked:
                replicas = self._guard(lambda: int(self.client.info("replication").get("connected_slaves", 0)), 1)  # unknown: assume one exists (safe)
                if replicas == 0:
                    observability.inc("provider_admission_total", decision="granted", reason="no_replica")  # nothing exists that could lose the lease in a failover (a restart is caught by the origin check)
                else:
                    self._guard(lambda: self.client.zrem(f"provider:slots:{provider}", token), None)  # a replica exists but did not confirm the write: do not rely on it
                    return self._decide("replica_ack_missing", token)
        self.enforced = True
        observability.inc("provider_admission_total", decision="granted", reason="ok")
        return token

    def release(self, provider: str, token: str) -> None:
        self._guard(lambda: self.client.zrem(f"provider:slots:{provider}", token), None)

    def in_use(self, provider: str) -> int:
        return int(self._guard(lambda: self.client.zcard(f"provider:slots:{provider}"), 0) or 0)
