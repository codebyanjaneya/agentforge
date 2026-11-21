"""Sprint 2, P4: ONE provider cooldown and ONE global concurrency cap across processes. The cross-process tests need a DISPOSABLE Redis (AGENTFORGE_TEST_REDIS_URL, loopback) and are skipped
without it. The in-process logic of the integration (bounded admission, shared cooldown, release on every path, no coordinator = unchanged behaviour) uses a fake coordinator and always runs."""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentplatform.providers import ProviderError, ProviderResponse
from agentplatform.resilience import ResilientProvider, RetryPolicy

BACKEND = Path(__file__).resolve().parent.parent
CHILD = BACKEND / "tests" / "coordination_child.py"
URL = os.environ.get("AGENTFORGE_TEST_REDIS_URL")


class FakeCoordinator:
    def __init__(self, slots=1, cooldown=0.0):
        self.slots, self.cooldown, self.held, self.released, self.cooldown_set, self.acquires = slots, cooldown, 0, 0, [], 0

    def cooldown_remaining(self, provider):
        return self.cooldown

    def set_cooldown(self, provider, seconds):
        self.cooldown_set.append(seconds)

    def acquire(self, provider, limit, lease):
        self.acquires += 1
        if self.held >= self.slots:
            return None
        self.held += 1
        return "tok"

    def release(self, provider, token):
        self.held -= 1
        self.released += 1


class Inner:
    name = "groq"

    def __init__(self, *errors):
        self.errors, self.calls = list(errors), 0

    async def complete(self, request):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return ProviderResponse(text="ok", model="m")


def provider(inner, coord, **policy):
    slept = []

    async def sleep(s):
        slept.append(s)

    return ResilientProvider(inner, RetryPolicy(**policy), sleep=sleep, coordinator=coord, rng=lambda: 0.0), slept


def test_a_shared_cooldown_set_by_another_process_makes_this_one_wait_before_calling():
    coord = FakeCoordinator(cooldown=7.0)
    p, slept = provider(Inner(), coord)
    asyncio.run(p.complete(object()))
    assert slept[0] == 7.0


def test_a_429_sets_the_shared_cooldown_and_the_slot_is_released_on_every_path():
    coord = FakeCoordinator(slots=2)
    p, slept = provider(Inner(ProviderError("rate_limited", retry_after=4.0)), coord, base_delay=0.1)
    asyncio.run(p.complete(object()))
    assert coord.cooldown_set and coord.cooldown_set[0] >= 4.0
    assert coord.held == 0 and coord.released == 2  # released after the failed attempt AND after the successful one
    p2, _ = provider(Inner(ProviderError("bad_request")), coord)
    with pytest.raises(ProviderError):
        asyncio.run(p2.complete(object()))
    assert coord.held == 0  # a permanent error also released its slot


def test_admission_is_bounded_and_fails_instead_of_queueing_or_retrying_forever():
    coord = FakeCoordinator(slots=1)
    coord.held = 1  # every global slot is taken by other processes
    inner = Inner()
    p, slept = provider(inner, coord, admission_wait=1.0)
    with pytest.raises(ProviderError) as e:
        asyncio.run(p.complete(object()))
    assert e.value.code == "rate_limited" and inner.calls == 0  # the provider was never called
    assert sum(slept) >= 1.0 and coord.acquires < 30  # it polled for about admission_wait: not forever, not in a storm


def test_no_coordinator_keeps_the_existing_per_process_behaviour():
    p = ResilientProvider(Inner(), RetryPolicy())
    assert asyncio.run(p.complete(object())).text == "ok"


def spawn(*args):
    code = f"import sys; sys.path.insert(0, {str(BACKEND)!r}); sys.argv = ['child'] + {list(args)!r}; exec(open({str(CHILD)!r}).read())"
    return subprocess.Popen([sys.executable, "-I", "-c", code], cwd=BACKEND, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.mark.skipif(not URL, reason="needs a disposable Redis (AGENTFORGE_TEST_REDIS_URL)")
class TestAcrossProcesses:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.undo()  # lift the suite's network guard: this class talks to a DISPOSABLE loopback Redis on purpose
        import redis

        self.r = redis.Redis.from_url(URL, decode_responses=True)
        self.r.flushdb()
        yield
        self.r.flushdb()

    def test_the_global_cap_is_never_exceeded_by_three_real_processes(self):
        procs = [spawn(URL, "load", "8") for _ in range(3)]  # 24 requests, 3 processes, global limit 3, per-process limit 8
        for p in procs:
            out, err = p.communicate(timeout=120)
            assert p.returncode == 0, err[-800:]
        assert 2 <= int(self.r.get("maxactive")) <= 3  # shared, and really exercised
        assert self.r.zcard("provider:slots:groq") == 0 and int(self.r.get("active")) == 0  # every slot released

    def test_a_429_in_one_process_holds_back_another(self):
        first = spawn(URL, "429once", "1")
        deadline = time.time() + 30
        while time.time() < deadline and self.r.pttl("provider:cooldown:groq") <= 0:
            time.sleep(0.02)
        assert self.r.pttl("provider:cooldown:groq") > 0  # the first process published the cooldown
        started = time.time()
        second = spawn(URL, "load", "1")
        out, err = second.communicate(timeout=60)
        assert second.returncode == 0, err[-800:]
        assert time.time() - started >= 0.8  # the second process never saw a 429 and still waited for the shared cooldown
        first.communicate(timeout=60)
        assert first.returncode == 0

    def test_a_crashed_holder_frees_its_slot_when_the_lease_expires(self):
        from agentplatform.coordination import Coordinator

        c = Coordinator(self.r)
        tokens = [c.acquire("groq", 2, 0.4) for _ in range(3)]
        assert tokens[0] and tokens[1] and tokens[2] is None  # the cap held
        time.sleep(0.6)  # the holders "died": no release
        assert c.acquire("groq", 2, 5) is not None


# ---------------------------------------------------------------- sprint 4.1: the Redis outage / failover POLICY (the global cap is never silently dropped)

class FlakyClient:
    """A Redis stand-in implementing the SAME logic as the admission Lua script (so the policy code is tested deterministically), with switches for an outage, a server move (failover/restart) and
    replica acknowledgement. The real script is exercised against a real Redis in TestRealScript below (opt-in)."""

    def __init__(self):
        self.down, self.held, self.cooldown, self.rid, self.origin, self.quarantined_until, self.now, self.replicas_acking = False, {}, 0, "run-A", None, 0.0, 1000.0, 1
        self.replicas_connected = 1
        self.waits = []

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")

    def register_script(self, source):
        if "provider:origin" in source or "KEYS[3]" in source:
            def acquire(keys, args):
                self._check()
                lease_ms, limit, token = int(args[0]), int(args[1]), args[2]
                if self.origin is None:
                    self.origin = self.rid
                elif self.origin != self.rid:
                    self.origin, self.quarantined_until = self.rid, self.now + lease_ms / 1000
                if self.now < self.quarantined_until:
                    return [2, self.rid]
                self.held = {t: exp for t, exp in self.held.items() if exp > self.now}
                if len(self.held) < limit:
                    self.held[token] = self.now + lease_ms / 1000
                    return [1, self.rid]
                return [0, self.rid]
            return acquire
        return lambda keys, args: (self._check(), setattr(self, "cooldown", int(args[0])))[1]

    def execute_command(self, *cmd):
        self._check()
        if cmd[0] == "WAIT":
            self.waits.append(cmd[1:])
            return self.replicas_acking
        raise AssertionError(cmd)

    def info(self, section):
        self._check()
        return {"connected_slaves": self.replicas_connected}

    def pttl(self, key):
        self._check()
        return self.cooldown or -2

    def zrem(self, key, token):
        self._check()
        self.held.pop(token, None)

    def zcard(self, key):
        self._check()
        return len(self.held)


def coordinator(client, **kw):
    import agentplatform.coordination as co

    c = co.Coordinator(client, clock=lambda: client.now, **kw)
    return c


def test_the_default_policy_is_closed_so_the_global_cap_is_never_silently_dropped():
    from config import load_settings

    s = load_settings({"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app"})
    assert s.provider_redis_down == "closed" and not any("PROVIDER_REDIS_DOWN" in w for w in s.warnings)
    import agentplatform.coordination as co

    assert co.Coordinator(FlakyClient()).fail_closed is True  # also the constructor default
    d = load_settings({"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app", "PROVIDER_REDIS_DOWN": "degrade"})
    assert d.provider_redis_down == "degrade" and any("EXPLICIT opt-in" in w and "NOT enforced" in w for w in d.warnings)  # degrade is opt-in and disclosed


def test_closed_mode_refuses_while_the_store_is_unreachable_counts_it_and_reports_the_cap_as_not_enforced():
    import agentplatform.coordination as co
    import observability

    observability.reset()
    co.INSTANCES.clear()
    client = FlakyClient()
    c = coordinator(client, fail_closed=True)
    assert co.cap_enforced() == 1 and c.acquire("groq", 2, 60) is not None
    client.down = True
    assert c.acquire("groq", 2, 60) is None and c.enforced is False and co.cap_enforced() == 0
    assert observability.total("provider_admission_total", decision="refused", reason="store_unavailable") == 1 and observability.total("redis_unavailable_total", mode="closed") == 1
    client.down = False
    client.now += co.RETRY_AFTER + 1
    assert c.acquire("groq", 2, 60) is not None and co.cap_enforced() == 1  # the same server is back: accounting is intact, admission resumes at once
    co.INSTANCES.clear()


def test_degrade_mode_is_per_process_only_and_says_so_every_time():
    import agentplatform.coordination as co
    import observability

    observability.reset()
    co.INSTANCES.clear()
    client = FlakyClient()
    c = coordinator(client, fail_closed=False)
    client.down = True
    assert c.acquire("groq", 1, 60) is not None and c.acquire("groq", 1, 60) is not None  # NOT limited globally: that is exactly what degrade means
    assert c.enforced is False and co.cap_enforced() == 0
    assert observability.total("provider_admission_total", decision="degraded", reason="store_unavailable") == 2  # visible, never silent
    co.INSTANCES.clear()


def test_a_server_move_quarantines_admission_for_one_lease_term_for_every_process_including_new_ones():
    import agentplatform.coordination as co
    import observability

    observability.reset()
    co.INSTANCES.clear()
    client = FlakyClient()
    old = coordinator(client, fail_closed=True)
    holder = old.acquire("groq", 1, 60)
    assert holder is not None and old.acquire("groq", 1, 60) is None  # the cap of 1 holds
    client.rid, client.held = "run-B", {}  # FAILOVER: a promoted replica with a different run_id that lost the recent lease (asynchronous replication)
    fresh = coordinator(client, fail_closed=True)  # a process that started AFTER the failover and never saw the old server
    assert fresh.acquire("groq", 1, 60) is None and old.acquire("groq", 1, 60) is None  # without the quarantine this would be granted while `holder` is still running: cap 2 > 1
    assert observability.total("provider_admission_total", reason="server_moved") == 2 and co.cap_enforced() == 0
    client.now += 59
    assert fresh.acquire("groq", 1, 60) is None  # still inside the lease term of the lost holder
    client.now += 2
    assert fresh.acquire("groq", 1, 60) is not None and old.acquire("groq", 1, 60) is None  # one lease term later the lost holder cannot be running: normal service, cap intact
    assert co.cap_enforced() in (0, 1)
    co.INSTANCES.clear()


def test_a_replica_acknowledgement_is_required_in_sentinel_mode_and_an_unacknowledged_grant_is_withdrawn():
    import agentplatform.coordination as co
    import observability

    observability.reset()
    co.INSTANCES.clear()
    client = FlakyClient()
    c = coordinator(client, fail_closed=True, replica_ack_ms=100)
    assert c.acquire("groq", 2, 60) is not None and client.waits == [(1, 100)]
    client.replicas_acking = 0  # no replica confirmed the write: after a failover this lease could be missing
    assert c.acquire("groq", 2, 60) is None
    assert len(client.held) == 1  # the unacknowledged slot was released again, never relied on
    assert observability.total("provider_admission_total", reason="replica_ack_missing") == 1
    client.replicas_connected = 0  # after a failover consumed the only replica: nothing exists that could lose a lease, so the grant stands (and admission is NOT refused forever)
    assert c.acquire("groq", 2, 60) is not None and observability.total("provider_admission_total", reason="no_replica") == 1
    co.INSTANCES.clear()


def test_through_the_resilient_provider_a_refused_admission_never_reaches_the_model_and_fails_as_rate_limited():
    import agentplatform.coordination as co

    co.INSTANCES.clear()
    client = FlakyClient()
    client.down = True
    inner = Inner()
    p, slept = provider(inner, coordinator(client, fail_closed=True), admission_wait=1.0)
    with pytest.raises(ProviderError) as e:
        asyncio.run(p.complete(object()))
    assert e.value.code == "rate_limited" and inner.calls == 0 and sum(slept) >= 1.0  # default policy: fail closed
    degraded_client = FlakyClient()
    degraded_client.down = True
    inner2 = Inner()
    p2, _ = provider(inner2, coordinator(degraded_client, fail_closed=False))
    assert asyncio.run(p2.complete(object())).text == "ok" and inner2.calls == 1  # explicit degrade: served, but not globally capped
    co.INSTANCES.clear()


def test_an_unreachable_redis_at_startup_neither_crashes_nor_silently_removes_the_limits():
    import redis

    from agentplatform.coordination import Coordinator
    from ratelimit import RateLimiter

    dead = redis.Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.05, socket_timeout=0.05)
    closed = Coordinator(dead)  # constructing it must not connect
    assert closed.cooldown_remaining("groq") == 0.0 and closed.acquire("groq", 1, 60) is None  # default: refused
    assert Coordinator(dead, fail_closed=False).acquire("groq", 1, 60) is not None  # explicit degrade only
    rl = RateLimiter(dead)
    assert rl.hit("b", "u", 2) is None and rl.hit("b", "u", 2) is None and rl.degraded is True
    assert rl.hit("b", "u", 2) is not None  # per-user limits stay bounded (and the gauge rate_limit_shared_enforced reports 0)


@pytest.mark.skipif(not URL, reason="needs a disposable Redis (AGENTFORGE_TEST_REDIS_URL)")
class TestRealScript:
    """The REAL Lua admission script against a real Redis: the server-move detection compares ``run_id`` with ``provider:origin``."""

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.undo()
        import redis

        self.r = redis.Redis.from_url(URL, decode_responses=True)
        self.r.flushdb()
        yield
        self.r.flushdb()

    def test_a_normal_grant_stores_the_origin_and_the_cap_holds(self):
        from agentplatform.coordination import Coordinator

        c = Coordinator(self.r)
        assert c.acquire("groq", 2, 60) and c.acquire("groq", 2, 60) and c.acquire("groq", 2, 60) is None
        assert self.r.get("provider:origin") == self.r.info("server")["run_id"] and not self.r.exists("provider:quarantine")

    def test_a_different_server_identity_quarantines_every_process_for_one_lease_term(self):
        from agentplatform.coordination import Coordinator

        c = Coordinator(self.r)
        assert c.acquire("groq", 5, 3) is not None
        self.r.set("provider:origin", "a-run-id-of-some-OTHER-server")  # what a promoted replica's replicated key looks like
        fresh = Coordinator(self.r)  # a process that never saw the old server
        assert fresh.acquire("groq", 5, 3) is None and c.acquire("groq", 5, 3) is None
        assert self.r.exists("provider:quarantine") and 0 < self.r.pttl("provider:quarantine") <= 3000
        time.sleep(3.3)
        assert fresh.acquire("groq", 5, 3) is not None  # the quarantine expired with the lease term; origin now matches this server
