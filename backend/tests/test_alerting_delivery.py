"""Sprint 5 closure (D074, local only): webhook DELIVERY edge cases against loopback receivers, and the in-process evaluator's restart behaviour.

NOT verified here (needs an authorised environment): a real receiver, on-call routing, delivery from a real network. These tests only prove how the notifier behaves
when the receiver misbehaves, so that a production receiver's misbehaviour cannot silence or flood the alerts."""

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import alerting
import observability
from alerting import AlertManager, RedisClaims, Rule, Signals, WebhookNotifier

DOCS = Path(__file__).resolve().parents[2] / "docs" / "operations.md"


class Hook:
    """A loopback receiver whose behaviour per request is scripted: ('status', code[, location]) | ('garbage',) | ('truncate',) | ('slow', seconds)."""

    def __init__(self, script):
        self.script, self.hits, self.bodies, self.methods, self.paths = list(script), 0, [], [], []
        hook = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                hook.hits += 1
                hook.bodies.append(body)
                hook.methods.append(self.command)
                hook.paths.append(self.path)
                step = hook.script.pop(0) if hook.script else ("status", 200)
                if step[0] == "garbage":
                    self.wfile.write(b"this is not http at all\r\n\r\n")
                    self.close_connection = True
                    return
                if step[0] == "truncate":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\nshort")
                    self.close_connection = True
                    return
                if step[0] == "slow":
                    time.sleep(step[1])
                code = step[1] if step[0] == "status" else 200
                self.send_response(code)
                if len(step) > 2:
                    self.send_header("Location", step[2])
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = self.base + "/hook/SECRET-TOKEN-IN-URL"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.undo()  # lift the suite's network guard: loopback receivers only
    observability.reset()
    made = []

    def make(script):
        h = Hook(script)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()


def notifier(h, **kw):
    return WebhookNotifier(h.url, sleep=lambda s: None, **kw)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 410, 422, 429])
def test_a_4xx_answer_is_a_failed_delivery_bounded_by_the_attempt_limit_and_never_raises(hook, code, caplog):
    h = hook([("status", code)] * 10)
    with caplog.at_level(logging.DEBUG, logger="agentforge.alerting"):
        assert notifier(h).deliver({"text": "x"}) is False
    assert h.hits == alerting.ATTEMPTS  # bounded: never an unbounded retry storm against a receiver that refuses us
    assert "SECRET-TOKEN" not in caplog.text


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_a_5xx_answer_is_retried_with_bounded_backoff_and_then_succeeds_or_gives_up(hook, code):
    slept = []
    h = hook([("status", code), ("status", code), ("status", 200)])
    assert WebhookNotifier(h.url, sleep=slept.append).deliver({"text": "x"}) is True and h.hits == 3
    assert slept == [0.5, 1.0]  # exponential and small: the evaluator thread is never held for long
    h2 = hook([("status", code)] * 5)
    assert WebhookNotifier(h2.url, sleep=lambda s: None).deliver({"text": "x"}) is False and h2.hits == alerting.ATTEMPTS


def test_a_response_that_is_not_http_is_a_failed_attempt_not_a_crash_and_not_a_success(hook, caplog):
    h = hook([("garbage",)] * 10)
    with caplog.at_level(logging.DEBUG, logger="agentforge.alerting"):
        assert notifier(h).deliver({"text": "x"}) is False
    assert h.hits == alerting.ATTEMPTS
    assert "SECRET-TOKEN" not in caplog.text and "not http at all" not in caplog.text  # the receiver's bytes are never logged either


def test_a_2xx_status_line_is_a_delivery_even_if_the_response_body_is_cut_short_and_is_not_resent(hook):
    """Documented choice: the receiver accepted the POST (2xx status line); the body of its answer is irrelevant, and retrying would announce the same alert twice."""
    h = hook([("truncate",)] * 3)
    assert notifier(h).deliver({"text": "x"}) is True and h.hits == 1


def test_a_receiver_slower_than_the_attempt_timeout_fails_fast_and_bounded(hook):
    h = hook([("slow", 1.5)] * 3)
    started = time.monotonic()
    assert WebhookNotifier(h.url, attempts=2, timeout=0.3, sleep=lambda s: None).deliver({"text": "x"}) is False
    assert time.monotonic() - started < 5  # 2 attempts x 0.3 s, not the receiver's 1.5 s each


def test_a_redirect_is_never_treated_as_delivery_and_the_alert_is_never_replayed_elsewhere(hook):
    """A 3xx must not be 'followed': urllib turns a redirected POST into a GET and would report the redirect target's 200 as a delivered alert while no alert was delivered."""
    target = hook([("status", 200)] * 5)
    redirector = hook([("status", 302, target.base + "/elsewhere")] * 5)
    assert notifier(redirector).deliver({"text": "x"}) is False
    assert target.hits == 0  # the redirect target was never contacted: no alert body was leaked to a location chosen by the receiver
    assert redirector.hits == alerting.ATTEMPTS


def test_the_payload_that_is_sent_is_the_documented_closed_vocabulary_and_never_the_url(hook):
    h = hook([])
    clock = type("C", (), {"t": 1_000_000.0, "__call__": lambda self: self.t})()
    signals = Signals(clock)
    rule = Rule("AgentForgeProbe", "page", 0, "A probe condition holds", lambda s: True)
    AlertManager(notifier(h), signals, rules=(rule,), clock=clock).evaluate()
    (body,) = h.bodies
    sent = json.loads(body)
    assert set(sent) == {"text", "alert", "severity", "status", "since"} and "SECRET-TOKEN" not in body.decode()


def test_an_evaluator_restart_forgets_pending_and_firing_state_but_the_shared_claim_prevents_a_duplicate_announcement():
    """The documented limitation: the evaluator is in-process. After a restart a still-true condition starts again from 'pending' and (without Redis) would announce again;
    with the shared Redis claim the same episode is not announced twice within the claim TTL."""

    class Collect:
        def __init__(self):
            self.sent = []

        def deliver(self, payload):
            self.sent.append(payload)
            return True

    class Clock:
        t = 1_000_000.0

        def __call__(self):
            return self.t

    class Store:
        def __init__(self):
            self.keys = set()

        def set(self, key, value, nx=False, ex=None):
            if key in self.keys:
                return None
            self.keys.add(key)
            return True

    clock, store = Clock(), Store()
    rule = Rule("AgentForgeRestart", "page", 60, "Condition holds", lambda s: s.gauges.get("x", 0) > 0)

    def boot(notifier, claims):
        signals = Signals(clock)
        signals.record({"x": 1}, {})
        return AlertManager(notifier, signals, rules=(rule,), claims=claims, clock=clock), signals

    first = Collect()
    m1, _ = boot(first, RedisClaims(store))
    m1.evaluate()
    clock.t += 61
    m1.evaluate()
    assert [p["status"] for p in first.sent] == ["firing"]
    second = Collect()
    m2, _ = boot(second, RedisClaims(store))  # the process restarted: new evaluator, empty state
    assert m2.state["AgentForgeRestart"]["firing"] is False and m2.state["AgentForgeRestart"]["since"] is None
    m2.evaluate()
    clock.t += 61
    m2.evaluate()
    assert second.sent == []  # the condition re-entered pending and fired again, but the claim for this episode already exists
    alone = Collect()
    m3, _ = boot(alone, None)  # no Redis at all: a restart CAN announce the same condition again (documented trade-off)
    m3.evaluate()
    clock.t += 61
    m3.evaluate()
    assert [p["status"] for p in alone.sent] == ["firing"]


def test_the_operations_guide_states_the_in_process_limits_and_recommends_an_independent_monitor():
    text = DOCS.read_text(encoding="utf-8").lower()
    assert "in-process" in text and "restart" in text and "independent" in text and "external monitor" in text
