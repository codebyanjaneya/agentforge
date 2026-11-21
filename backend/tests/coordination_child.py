"""Child process for tests/test_provider_coordination.py: its OWN interpreter, its own ResilientProvider, the SAME Redis. argv: redis_url mode n
mode ``load``: n requests through a fake provider that records the highest number of simultaneously active requests ACROSS processes in Redis.
mode ``429once``: the first request gets a 429 (retry-after 1.5 s)."""
import asyncio
import sys
import time

import redis

from agentplatform.coordination import Coordinator
from agentplatform.providers import ProviderError, ProviderResponse
from agentplatform.resilience import ResilientProvider, RetryPolicy

url, mode, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
r = redis.Redis.from_url(url, decode_responses=True)
MAXSCRIPT = "local m = tonumber(redis.call('GET', KEYS[2]) or '0'); local a = redis.call('INCR', KEYS[1]); if a > m then redis.call('SET', KEYS[2], a) end; return a"


class Inner:
    name = "groq"

    def __init__(self):
        self.script = r.register_script(MAXSCRIPT)
        self.calls = 0

    async def complete(self, request):
        self.calls += 1
        if mode == "429once" and self.calls == 1:
            raise ProviderError("rate_limited", retry_after=1.5)
        await asyncio.to_thread(self.script, keys=["active", "maxactive"])
        await asyncio.sleep(0.15)
        r.decr("active")
        return ProviderResponse(text="ok", model="m")


async def main():
    policy = RetryPolicy(global_concurrency=3, concurrency=8, admission_wait=60.0, base_delay=0.05)
    p = ResilientProvider(Inner(), policy, coordinator=Coordinator(r))
    t0 = time.time()
    await asyncio.gather(*(p.complete(object()) for _ in range(n)))
    print(f"done {time.time() - t0:.2f}")


asyncio.run(main())
