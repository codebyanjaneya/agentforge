"""Redis client factory (sprint 4): a single node (``REDIS_URL``) or a Sentinel-managed master (``REDIS_SENTINELS`` + ``REDIS_SENTINEL_MASTER``). Deliberately dependency-light (redis only) so operational drills can
import it inside a bare container.

SENTINEL: redis-py's ``Sentinel.master_for`` asks the sentinels for the current master on every (re)connect, so after a failover the next call after the old connection fails is routed to the promoted
replica; there is no AgentForge-side re-discovery code to get wrong. Timeouts are short on purpose (a slow Redis must never slow the API): the breakers in ``ratelimit`` and ``agentplatform.coordination`` turn
connection errors into the documented degrade / fail-closed behaviour, and they re-probe every 15-30 s, which bounds the time after a failover during which the limits are per-process only.
SCOPE: rate limits and provider coordination use this factory. The legacy short-term chat memory (``memory/short_term.py``) still builds its own single-node client from ``REDIS_URL`` and is NOT Sentinel-aware:
point ``REDIS_URL`` at a stable endpoint (a Sentinel-aware proxy / virtual IP) if you need it to follow a failover; without it that memory degrades with a warning (agent history unavailable) during and after one.
Redis CLUSTER is not supported: the coordination scripts touch several keys and the limiter keys are not hash-tagged."""

from urllib.parse import unquote, urlparse

TIMEOUT = 0.25


def build(url: str, sentinels: tuple = (), master: str | None = None, *, timeout: float = TIMEOUT):
    import redis

    if sentinels:
        if not master:
            raise ValueError("REDIS_SENTINEL_MASTER is required with REDIS_SENTINELS")
        from redis.sentinel import Sentinel

        parsed = urlparse(url)
        auth = {"password": unquote(parsed.password)} if parsed.password else {}
        db = int((parsed.path or "/0").lstrip("/") or 0)
        manager = Sentinel(list(sentinels), socket_timeout=timeout, socket_connect_timeout=timeout, sentinel_kwargs=auth)
        return manager.master_for(master, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True, db=db, **auth)
    return redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=timeout, socket_timeout=timeout)
