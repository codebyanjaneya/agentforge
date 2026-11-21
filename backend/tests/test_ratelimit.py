"""Sprint 2, P2: shared per-user rate limits. Unit tests of the limiter (window, Retry-After, key isolation, Redis outage, bounded memory, shared state across limiter instances = processes), and the HTTP/WebSocket
integration through the real application."""

import threading

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import ratelimit
from ratelimit import RateLimiter, limit


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class SharedRedis:
    """A tiny in-memory Redis (INCR/EXPIRE via pipeline) that several limiter instances can share, like several API processes."""

    def __init__(self):
        self.d, self.lock = {}, threading.Lock()

    def pipeline(self):
        outer = self

        class P:
            def __init__(self):
                self.ops = []

            def incr(self, k):
                self.ops.append(("incr", k))

            def expire(self, k, s):
                self.ops.append(("expire", k))

            def execute(self):
                with outer.lock:
                    out = []
                    for op, k in self.ops:
                        if op == "incr":
                            outer.d[k] = outer.d.get(k, 0) + 1
                            out.append(outer.d[k])
                        else:
                            out.append(True)
                    return out

        return P()


class DeadRedis:
    calls = 0

    def pipeline(self):
        DeadRedis.calls += 1
        raise ConnectionError("down")


def test_allows_up_to_the_limit_then_refuses_with_the_time_left_in_the_window():
    clock = Clock(1000.0)  # 1000 % 60 = 40 -> 20 s left
    rl = RateLimiter(clock=clock)
    assert [rl.hit("b", "u", 3) for _ in range(3)] == [None, None, None]
    assert rl.hit("b", "u", 3) == 20
    clock.t = 1021.0  # next window
    assert rl.hit("b", "u", 3) is None


def test_users_and_buckets_are_isolated():
    rl = RateLimiter(clock=Clock())
    for _ in range(2):
        rl.hit("b", "alice", 2)
    assert rl.hit("b", "alice", 2) is not None
    assert rl.hit("b", "bob", 2) is None and rl.hit("other", "alice", 2) is None


def test_two_processes_share_one_budget_through_redis():
    store, clock = SharedRedis(), Clock()
    p1, p2 = RateLimiter(store, clock), RateLimiter(store, clock)
    assert p1.hit("b", "u", 2) is None and p2.hit("b", "u", 2) is None
    assert p1.hit("b", "u", 2) is not None and p2.hit("b", "u", 2) is not None  # the third request is refused whichever process receives it


def test_concurrent_requests_never_exceed_the_limit():
    store = SharedRedis()
    procs = [RateLimiter(store) for _ in range(4)]
    allowed, lock = [], threading.Lock()

    def worker(i):
        for _ in range(25):
            r = procs[i % 4].hit("b", "u", 40, window=3600)
            with lock:
                allowed.append(r is None)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sum(allowed) == 40 and len(allowed) == 200


def test_a_redis_outage_falls_back_to_a_local_counter_and_is_not_retried_every_request():
    DeadRedis.calls = 0
    clock = Clock()
    rl = RateLimiter(DeadRedis(), clock)
    results = [rl.hit("b", "u", 2) for _ in range(4)]
    assert results[:2] == [None, None] and results[2] is not None  # still limited, never unlimited
    assert DeadRedis.calls == 1  # the breaker: no new attempt inside the retry interval


def test_local_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(ratelimit, "MAX_LOCAL_KEYS", 50)
    rl = RateLimiter(clock=Clock())
    for i in range(500):
        rl.hit("b", f"u{i}", 5)
    assert len(rl._local) <= 51


def make_app(per_minute, limiter):
    app = FastAPI()
    from authz.dependencies import get_scope

    class Scope:
        def __init__(self, user_id):
            self.user_id = user_id

    app.dependency_overrides[get_scope] = lambda: Scope("u1")
    app.dependency_overrides[ratelimit.get_limiter] = lambda: limiter

    @app.get("/x", dependencies=[Depends(limit("x", per_minute))])
    def x():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def test_the_http_dependency_answers_429_with_retry_after_and_leaks_nothing():
    c = make_app(2, RateLimiter(clock=Clock(1000.0)))
    assert [c.get("/x").status_code for _ in range(2)] == [200, 200]
    r = c.get("/x")
    assert r.status_code == 429 and r.headers["retry-after"] == "20"
    assert r.json() == {"detail": {"code": "too_many_requests", "message": "Too many requests. Please wait a moment and try again."}}
    assert "u1" not in r.text
    assert all(c.get("/health").status_code == 200 for _ in range(50))  # health is never limited


# ---------------------------------------------------------------- through the real application

from starlette.websockets import WebSocketDisconnect  # noqa: E402

from tests.api_support import ORIGIN, api, auth_db, main_module  # noqa: E402,F401  (fixtures)


@pytest.fixture
def fresh_limiter(monkeypatch):
    rl = RateLimiter(clock=Clock(1000.0))
    monkeypatch.setattr(ratelimit, "_limiter", rl)
    return rl


def test_the_agent_run_route_is_limited_per_user_not_per_connection(api, fresh_limiter):
    a, b = api.auth("user-a", "a@example.com"), api.auth("user-b", "b@example.com")
    statuses = [api.client.post("/autonomous/run", json={"agent": "ceo", "task": "hi"}, headers=a).status_code for _ in range(32)]
    assert statuses.count(429) == 2 and 429 not in statuses[:30]
    blocked = api.client.post("/autonomous/run", json={"agent": "ceo", "task": "hi"}, headers=a)
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1 and "user-a" not in blocked.text and "a@example.com" not in blocked.text
    assert api.client.post("/autonomous/run", json={"agent": "ceo", "task": "hi"}, headers=b).status_code != 429  # another person is unaffected
    assert api.client.get("/health").status_code == 200


def test_websocket_jobs_share_the_users_budget_across_connections(api, fresh_limiter, monkeypatch):
    import realtime

    monkeypatch.setattr(realtime, "WS_JOBS_PER_MINUTE", 2)
    seen = []
    for _ in range(2):  # two separate sockets of the same user
        with api.client.websocket_connect("/ws", headers={"origin": ORIGIN}) as ws:
            ws.send_json({"type": "auth", "token": api.token("user-a", "a@example.com")})
            assert ws.receive_json() == {"type": "authenticated"}
            for _ in range(2):
                ws.send_json({"agent": "no-such-agent", "task": "x"})
                seen.append(ws.receive_json())
    assert [m.get("error") for m in seen[:2]] == ["Unknown agent.", "Unknown agent."]  # the first two jobs passed admission
    assert all("retry_after" in m and m["error"].startswith("Too many requests") for m in seen[2:])  # the second socket got no fresh budget


def test_websocket_connection_admission_is_per_user(api, fresh_limiter, monkeypatch):
    import realtime

    monkeypatch.setattr(realtime, "WS_CONNECTS_PER_MINUTE", 1)
    with api.client.websocket_connect("/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "auth", "token": api.token("user-a", "a@example.com")})
        assert ws.receive_json() == {"type": "authenticated"}
    with api.client.websocket_connect("/ws", headers={"origin": ORIGIN}) as ws:
        ws.send_json({"type": "auth", "token": api.token("user-a", "a@example.com")})
        assert "retry_after" in ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1013


def test_a_configured_limit_overrides_the_route_default_and_unconfigured_buckets_keep_it(monkeypatch):
    monkeypatch.setattr(ratelimit, "_limits", {"x": 1})
    c = make_app(50, RateLimiter(clock=Clock(1000.0)))  # the route default is 50, the configured value 1
    assert c.get("/x").status_code == 200 and c.get("/x").status_code == 429
    monkeypatch.setattr(ratelimit, "_limits", {})
    c2 = make_app(2, RateLimiter(clock=Clock(1000.0)))
    assert [c2.get("/x").status_code for _ in range(3)] == [200, 200, 429]
