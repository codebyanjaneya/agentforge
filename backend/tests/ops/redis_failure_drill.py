"""Redis failure drill (sprint 3). NOT a pytest test: it stops and starts a DISPOSABLE Redis container you name, so it must never be pointed at a shared one.

    python tests/ops/redis_failure_drill.py <container-name> redis://127.0.0.1:<port>/0

It prints one line per observation and exits non-zero if an expectation fails. Expectations (documented in docs/operations.md section 10):
  * Redis up: the global cap is enforced.
  * Redis down: ``degrade`` still grants a slot (per-process limits only) and counts the outage; ``fail_closed`` grants none; the per-user rate limiter keeps limiting locally; nothing raises.
  * Redis back (after the breaker interval): a restart changes the server's identity, which quarantines admission for one lease term (the accounting cannot be trusted even if the RDB kept it); total state loss
    including the origin key (modelled with FLUSHDB) is indistinguishable from a first start, so the cap can be briefly exceeded: the documented residual risk; a lease that expired is reclaimed by the next acquire.
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

import redis  # noqa: E402

import observability  # noqa: E402
from agentplatform import coordination  # noqa: E402
from agentplatform.coordination import Coordinator  # noqa: E402
from ratelimit import RateLimiter  # noqa: E402

container, url = sys.argv[1], sys.argv[2]
client = redis.Redis.from_url(url, decode_responses=True, socket_connect_timeout=0.25, socket_timeout=0.25)
failures = []


def check(label, ok):
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def docker(*args):
    subprocess.run(["docker", *args], check=True, capture_output=True, timeout=60)


def wait_up():
    for _ in range(40):
        try:
            if client.ping():
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    raise SystemExit("redis did not come back")


client.flushdb()
degrade, closed = Coordinator(client, fail_closed=False), Coordinator(client, fail_closed=True)  # degrade is an EXPLICIT opt-in since sprint 4.1; closed is the default
limiter = RateLimiter(client)

holder = degrade.acquire("groq", 2, 120)
check("up: a slot is granted and counted", holder is not None and degrade.in_use("groq") == 1)
check("up: a second slot is granted, a third is refused (cap 2 enforced)", degrade.acquire("groq", 2, 120) is not None and degrade.acquire("groq", 2, 120) is None)

docker("stop", container)
t0 = time.time()
got_degrade = degrade.acquire("groq", 2, 120)
got_closed = closed.acquire("groq", 2, 120)
check("down: degrade still grants (per-process limits only)", got_degrade is not None)
check("down: fail_closed grants nothing", got_closed is None)
check("down: the calls returned quickly (no hang)", time.time() - t0 < 3.0)
check("down: the per-user limiter keeps limiting locally", [limiter.hit("b", "u", 2) for _ in range(3)][2] is not None)
check("down: every outage is on the metrics", "agentforge_redis_unavailable_total" in observability.render())
degrade.release("groq", holder)  # a release while down must not raise
check("down: release/cooldown calls do not raise", degrade.cooldown_remaining("groq") == 0.0)

docker("start", container)
wait_up()
degrade._down_until = closed._down_until = 0.0  # skip the breaker interval for the drill (it is coordination.RETRY_AFTER = %ss in production)
survived = degrade.in_use("groq")
check("back (graceful restart): leases written before the outage survive in Redis's RDB (%d held)" % survived, survived == 2)
check("back: the server's identity changed (restart), so the default policy QUARANTINES admission for one lease term even though the leases survived (closed refuses)", closed.acquire("groq", 3, 120) is None and client.exists("provider:quarantine") == 1)
check("back: ...and the explicit degrade mode is told the same and falls back per process (not globally capped)", degrade.acquire("groq", 3, 120) is not None and degrade.enforced is False)
client.flushdb()  # a Redis that restarts WITHOUT persistence (or is killed between saves) forgets every lease
check("lost state: holders are forgotten, so the cap can be briefly exceeded (documented)", degrade.in_use("groq") == 0 and degrade.acquire("groq", 1, 120) is not None)
client.flushdb()
check("lease: a short lease is reclaimed after it expires", degrade.acquire("groq", 1, 0.5) is not None and degrade.acquire("groq", 1, 0.5) is None)
time.sleep(0.8)
check("lease: ...and the next acquire gets the slot", degrade.acquire("groq", 1, 5) is not None)
print("breaker interval in production: %ss" % coordination.RETRY_AFTER)
client.flushdb()
sys.exit(1 if failures else 0)
