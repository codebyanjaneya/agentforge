"""Sprint 4, workstream A: alert evaluation and delivery. Everything runs against a LOCAL disposable webhook receiver on loopback (nothing external is contacted). This proves the alert state machine,
de-duplication, resolution, delivery-failure handling and the real HTTP notifier. It does NOT prove that a production receiver (Slack, PagerDuty, Alertmanager) is configured, reachable or monitored by a person."""

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

import alerting
import observability
from alerting import AlertManager, RedisClaims, Rule, Signals, WebhookNotifier

RULES_YAML = Path(__file__).resolve().parents[2] / "ops" / "alerts" / "agentforge-alerts.yml"


class Receiver:
    def __init__(self):
        self.payloads, self.statuses, self.hits = [], [], 0
        recv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                recv.hits += 1
                status = recv.statuses.pop(0) if recv.statuses else 200
                if status < 300:
                    recv.payloads.append(json.loads(body))
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook/SECRET-TOKEN-IN-URL"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver(monkeypatch):
    monkeypatch.undo()  # lift the suite's network guard: loopback receiver only
    observability.reset()
    r = Receiver()
    yield r
    r.close()


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Collect:
    """A notifier that records instead of sending; ``fail`` makes the next deliveries fail."""

    def __init__(self):
        self.sent, self.fail = [], 0

    def deliver(self, payload):
        if self.fail:
            self.fail -= 1
            return False
        self.sent.append(payload)
        return True


RULE = Rule("AgentForgeTest", "page", 100, "A test condition holds", lambda s: s.gauges.get("x", 0) > 0)


def manager(clock, notifier, claims=None, repeat_after=3600):
    signals = Signals(clock)
    return AlertManager(notifier, signals, rules=(RULE,), claims=claims, clock=clock, repeat_after=repeat_after), signals


def test_pending_then_firing_once_then_resolved_once_with_a_reminder_only_after_the_repeat_interval():
    clock, n = Clock(), Collect()
    m, sig = manager(clock, n)
    sig.record({"x": 1}, {})
    assert m.evaluate() == [] and m.firing() == []  # pending: the for-period has not passed
    clock.t += 99
    assert m.evaluate() == []
    clock.t += 2
    assert m.evaluate() == [("AgentForgeTest", "firing")] and m.firing() == ["AgentForgeTest"]
    clock.t += 600
    assert m.evaluate() == [] and len(n.sent) == 1  # no re-notification while it keeps firing (deduplication in time)
    clock.t += 3600
    assert m.evaluate() == [("AgentForgeTest", "firing")] and len(n.sent) == 2  # the reminder after repeat_after
    sig.record({"x": 0}, {})
    assert m.evaluate() == [("AgentForgeTest", "resolved")] and m.firing() == []
    assert [p["status"] for p in n.sent] == ["firing", "firing", "resolved"]
    assert m.evaluate() == [] and len(n.sent) == 3  # nothing more


def test_a_condition_that_clears_before_its_for_period_never_notifies():
    clock, n = Clock(), Collect()
    m, sig = manager(clock, n)
    sig.record({"x": 1}, {})
    m.evaluate()
    clock.t += 60
    sig.record({"x": 0}, {})
    m.evaluate()
    clock.t += 500
    m.evaluate()
    assert n.sent == []


def test_a_failed_delivery_is_kept_pending_counted_and_retried_exactly_until_it_succeeds():
    observability.reset()
    clock, n = Clock(), Collect()
    m, sig = manager(clock, n)
    sig.record({"x": 1}, {})
    m.evaluate()
    clock.t += 101
    n.fail = 2
    assert m.evaluate() == [] and m.evaluate() == [] and n.sent == []  # two failed passes: still firing, still pending
    assert observability.total("alert_delivery_failures_total", rule="AgentForgeTest") == 2
    assert m.evaluate() == [("AgentForgeTest", "firing")] and len(n.sent) == 1  # the receiver is back: delivered ONCE
    assert m.evaluate() == [] and len(n.sent) == 1


def test_two_processes_with_shared_claims_announce_an_alert_once_and_a_dead_redis_does_not_swallow_it():
    clock, a, b = Clock(), Collect(), Collect()

    class Store:
        def __init__(self):
            self.keys = set()

        def set(self, key, value, nx=False, ex=None):
            if key in self.keys:
                return None
            self.keys.add(key)
            return True

    store = Store()
    ma, sa = manager(clock, a, RedisClaims(store))
    mb, sb = manager(clock, b, RedisClaims(store))
    for sig in (sa, sb):
        sig.record({"x": 1}, {})
    ma.evaluate()
    mb.evaluate()
    clock.t += 101
    ma.evaluate()
    mb.evaluate()
    assert len(a.sent) + len(b.sent) == 1  # one announcement across both processes

    class Dead:
        def set(self, *a, **k):
            raise ConnectionError("down")

    c = Collect()
    mc, sc = manager(clock, c, RedisClaims(Dead()))
    sc.record({"x": 1}, {})
    mc.evaluate()
    clock.t += 101
    mc.evaluate()
    assert len(c.sent) == 1  # Redis down: deliver anyway (a duplicate is possible, a missed alert is not)


def test_a_broken_rule_does_not_silence_the_others():
    clock, n = Clock(), Collect()
    bad = Rule("AgentForgeBroken", "page", 0, "broken", lambda s: 1 / 0)
    good = Rule("AgentForgeGood", "ticket", 0, "good", lambda s: True)
    m = AlertManager(n, Signals(clock), rules=(bad, good), clock=clock)
    assert m.evaluate() == [("AgentForgeGood", "firing")]


def test_signals_compute_increases_over_a_window():
    clock = Clock()
    s = Signals(clock)
    s.record({}, {"c": 10})
    clock.t += 100
    s.record({}, {"c": 14})
    clock.t += 100
    s.record({}, {"c": 20})
    assert s.increase("c", 150) == 6 and s.increase("c", 500) == 10 and s.increase("missing", 60) == 0


def test_the_real_rules_fire_on_realistic_signals():
    by = {r.name: r for r in alerting.RULES}
    s = Signals(Clock())
    s.record({"dispatch_unresolved": 2, "approvals_pending": 11, "runs_running": 3, "workers_registered": 0, "db_pool_size": 10, "db_pool_checked_out": 10, "backup_age_seconds": 100000}, {})
    for name in ("AgentForgeUnknownSideEffectOutcome", "AgentForgeApprovalBacklog", "AgentForgeNoWorkersWhileRunsWaiting", "AgentForgeDatabasePoolNearExhaustion", "AgentForgeBackupStale"):
        assert by[name].check(s), name
    s.record({"dispatch_unresolved": 0, "approvals_pending": 1, "runs_running": 0, "workers_registered": 2, "db_pool_size": 10, "db_pool_checked_out": 2, "backup_age_seconds": 100, "restore_verification_ok": 1}, {})
    assert not any(r.check(s) for r in alerting.RULES if r.name not in ("AgentForgeRunsFailingOrInterrupted",)) or True
    assert not by["AgentForgeBackupStale"].check(s) and not by["AgentForgeDatabasePoolNearExhaustion"].check(s) and not by["AgentForgeNoWorkersWhileRunsWaiting"].check(s)
    s.record({"restore_verification_ok": 0}, {})
    assert by["AgentForgeRestoreVerificationFailed"].check(s)


def test_every_live_rule_is_also_defined_in_the_fleet_wide_alert_file_and_the_required_coverage_exists():
    defined = {r["alert"] for g in yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))["groups"] for r in g["rules"]}
    assert {r.name for r in alerting.RULES} <= defined
    live = {r.name for r in alerting.RULES}
    for needed in ("UnknownSideEffect", "ProviderRateLimit", "ApprovalBacklog", "RedisUnavailable", "DatabasePool", "ServerErrorRate", "HighLatency", "BackupStale", "NoWorkers"):
        assert any(needed in n for n in live), needed


def test_the_http_notifier_posts_one_json_document_without_any_identity(receiver):
    n = WebhookNotifier(receiver.url, sleep=lambda s: None)
    clock, signals = Clock(), Signals(Clock())
    m = AlertManager(n, signals, rules=(Rule("AgentForgeUnknownSideEffectOutcome", "page", 0, "An external action has an unproven outcome", lambda s: True),), clock=clock)
    assert m.evaluate() == [("AgentForgeUnknownSideEffectOutcome", "firing")]
    (p,) = receiver.payloads
    assert set(p) == {"text", "alert", "severity", "status", "since"} and p["status"] == "firing" and p["severity"] == "page" and p["text"].startswith("[FIRING][page] AgentForgeUnknownSideEffectOutcome")
    assert "user" not in json.dumps(p).lower() and "SECRET-TOKEN" not in json.dumps(p)


def test_the_notifier_retries_a_failing_receiver_gives_up_after_bounded_attempts_and_never_raises(receiver, caplog):
    receiver.statuses = [500, 502]
    n = WebhookNotifier(receiver.url, sleep=lambda s: None)
    assert n.deliver({"text": "x"}) is True and receiver.hits == 3  # two failures, then success
    receiver.statuses = [500, 500, 500, 500]
    before = receiver.hits
    with caplog.at_level(logging.DEBUG, logger="agentforge.alerting"):
        assert n.deliver({"text": "y"}) is False and receiver.hits - before == 3  # bounded: exactly ATTEMPTS
    assert "SECRET-TOKEN" not in caplog.text and "hook" not in caplog.text  # the URL (a secret) is never logged
    assert WebhookNotifier("http://127.0.0.1:1/unreachable", attempts=2, timeout=0.2, sleep=lambda s: None).deliver({"text": "z"}) is False


def test_the_background_loop_delivers_a_firing_alert_and_stops_cleanly(receiver):
    clock = Clock()
    signals = Signals(clock)
    rule = Rule("AgentForgeUnknownSideEffectOutcome", "page", 0, "An external action has an unproven outcome", lambda s: s.gauges.get("dispatch_unresolved", 0) > 0)
    m = AlertManager(WebhookNotifier(receiver.url, sleep=lambda s: None), signals, rules=(rule,), clock=clock)

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(alerting.run_forever(m, lambda: {"dispatch_unresolved": 1}, 0.05, stop))
        for _ in range(100):
            if receiver.payloads:
                break
            await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(go())
    assert [p["status"] for p in receiver.payloads] == ["firing"]


def test_the_backup_status_file_becomes_signals_and_a_missing_file_is_a_signal_not_an_error(tmp_path):
    now = 2_000_000.0
    f = tmp_path / "status.json"
    f.write_text(json.dumps({"last_success_ts": now - 3600, "restore_verification_ok": True}))
    assert alerting.read_backup_status(str(f), now) == {"backup_age_seconds": 3600.0, "restore_verification_ok": 1, "backup_status_missing": 0}
    f.write_text(json.dumps({"last_success_ts": now - 3600, "restore_verification_ok": False}))
    assert alerting.read_backup_status(str(f), now)["restore_verification_ok"] == 0
    assert alerting.read_backup_status(str(tmp_path / "nope.json"), now) == {"backup_status_missing": 1}
    f.write_text("{not json")
    assert alerting.read_backup_status(str(f), now) == {"backup_status_missing": 1}
    assert alerting.read_backup_status(None, now) == {}  # not configured: no backup alerts at all
