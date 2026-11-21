"""Alert evaluation and delivery (sprint 4). The smallest thing that makes the alert rules LIVE without adding a monitoring service: every API process evaluates the rules over its own metrics plus
database/Redis/backup-job signals, keeps a per-alert state machine, and delivers notifications to ONE webhook (Slack-compatible ``text`` plus structured fields; works with Slack/Mattermost incoming webhooks and
with Alertmanager-style receivers that accept JSON). It complements, and does not replace, a Prometheus/Alertmanager deployment: ``ops/alerts/agentforge-alerts.yml`` remains the fleet-wide definition.

STATE MACHINE per alert: inactive -> pending (condition true, waiting its ``for`` period) -> firing (ONE notification) -> resolved (ONE notification when the condition clears). A firing alert is re-announced at most
every ``repeat_after`` seconds (default 4 h). A notification counts as sent only after the receiver answered 2xx; a failed delivery is retried by the next evaluation, never marked sent, and counted
(``agentforge_alert_delivery_failures_total{rule}``). Delivery is bounded (3 attempts, 5 s timeout each) and can never raise into the application.

DEDUPLICATION: with several API processes, each would announce the same alert. Before delivering, a process claims ``alert:<rule>:<state>:<episode>`` in Redis (SET NX EX); the loser treats it as delivered by someone
else. If Redis is down the claim degrades to "deliver anyway": duplicates are possible, a missed alert is not.

LIMITS, stated: metrics-based signals are PER PROCESS (a rate computed by one process sees only its own traffic; the database gauges are global); the evaluator history lives in memory, so a restart forgets pending
timers (an alert re-enters ``pending``); there is no silencing UI, escalation policy or on-call schedule; a webhook receiver outage means alerts are delayed until it returns (and are visible in the metrics
and logs). Messages carry the alert name, severity, a fixed summary and timestamps: never a user, workspace, run, token or secret."""

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

import observability

logger = logging.getLogger("agentforge.alerting")
REPEAT_AFTER = 4 * 3600
ATTEMPTS, ATTEMPT_TIMEOUT = 3, 5.0
HISTORY_SECONDS = 2000


class Signals:
    """What the rules look at: instantaneous gauges plus a short history of counter totals (for increases over a window)."""

    def __init__(self, clock=time.time):
        self.clock, self.gauges, self._history = clock, {}, {}

    def record(self, gauges: dict, totals: dict) -> None:
        now = self.clock()
        self.gauges = dict(gauges)
        for key, value in totals.items():
            h = self._history.setdefault(key, deque())
            h.append((now, value))
            while h and now - h[0][0] > HISTORY_SECONDS:
                h.popleft()

    def increase(self, key: str, window: float) -> float:
        h = self._history.get(key)
        if not h:
            return 0.0
        now = self.clock()
        base = next((v for t, v in h if now - t <= window), h[0][1])  # the oldest sample inside the window (or the oldest we have)
        return max(0.0, h[-1][1] - base)


@dataclass(frozen=True)
class Rule:
    name: str
    severity: str  # page | ticket
    for_seconds: float
    summary: str
    check: object  # callable(Signals) -> bool


def _ratio(a: float, b: float, minimum: float) -> float:
    return a / b if b >= minimum else 0.0


RULES = (
    Rule("AgentForgeUnknownSideEffectOutcome", "page", 900, "An external action has an unproven outcome and nobody has reconciled it", lambda s: s.gauges.get("dispatch_unresolved", 0) > 0),
    Rule("AgentForgeApprovalBacklog", "ticket", 1800, "More than 10 approvals have been waiting for a person", lambda s: s.gauges.get("approvals_pending", 0) > 10),
    Rule("AgentForgeNoWorkersWhileRunsWaiting", "page", 600, "Runs are in progress but no worker process is registered", lambda s: s.gauges.get("runs_running", 0) > 0 and s.gauges.get("workers_registered", 1) == 0),
    Rule("AgentForgeRunsFailingOrInterrupted", "ticket", 900, "More than 30% of finished runs ended failed, partial or interrupted",
         lambda s: _ratio(s.increase("runs_bad", 1800), s.increase("runs_all", 1800), 5) > 0.3),
    Rule("AgentForgeProviderRateLimitExhausted", "ticket", 0, "A model request exhausted its rate-limit retry budget", lambda s: s.increase("provider_exhausted", 900) > 0),
    Rule("AgentForgeProviderCapNotEnforced", "page", 60, "The global model-provider concurrency cap is NOT being enforced (Redis outage, failover quarantine, or explicit degrade mode)", lambda s: s.gauges.get("provider_global_cap_enforced", 1) == 0),
    Rule("AgentForgeRedisUnavailable", "page", 300, "Redis calls are failing: rate limits and provider coordination are degraded or failing closed", lambda s: s.increase("redis_unavailable", 300) > 0),
    Rule("AgentForgeDatabasePoolNearExhaustion", "page", 300, "More than 90% of this instance's database connections are checked out",
         lambda s: s.gauges.get("db_pool_size", 0) > 0 and s.gauges.get("db_pool_checked_out", 0) / s.gauges["db_pool_size"] > 0.9),
    Rule("AgentForgeHighServerErrorRate", "page", 600, "More than 5% of API requests are failing with 5xx", lambda s: _ratio(s.increase("http_5xx", 300), s.increase("http_all", 300), 20) > 0.05),
    Rule("AgentForgeHighLatency", "ticket", 600, "More than 5% of API requests take longer than 5 seconds", lambda s: _ratio(s.increase("http_slow", 300), s.increase("http_all", 300), 20) > 0.05),
    Rule("AgentForgeBackupStale", "page", 0, "No successful database backup recorded in the last 25 hours (or no backup status at all)", lambda s: s.gauges.get("backup_age_seconds", 0) > 90000 or s.gauges.get("backup_status_missing", 0) == 1),
    Rule("AgentForgeRestoreVerificationFailed", "page", 0, "The last restore verification of a backup failed", lambda s: s.gauges.get("restore_verification_ok", 1) == 0),
)


class WebhookNotifier:
    """POSTs one JSON document to the configured webhook. ``deliver`` returns True only on a 2xx answer; it never raises. The URL is a secret (many webhooks embed a token): it is never logged."""

    def __init__(self, url: str, *, attempts: int = ATTEMPTS, timeout: float = ATTEMPT_TIMEOUT, sleep=time.sleep):
        self._url, self.attempts, self.timeout, self._sleep = url, attempts, timeout, sleep

    def deliver(self, payload: dict) -> bool:
        data = json.dumps(payload).encode()
        for attempt in range(self.attempts):
            try:
                request = urllib.request.Request(self._url, data=data, method="POST", headers={"Content-Type": "application/json", "User-Agent": "agentforge-alerts/1"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if 200 <= response.status < 300:
                        return True
            except Exception as exc:  # noqa: BLE001 - delivery must never raise into the application
                logger.warning("alert delivery attempt %d failed (%s)", attempt + 1, type(exc).__name__)  # the type only: never the URL or the body
            if attempt + 1 < self.attempts:
                self._sleep(0.5 * 2 ** attempt)
        return False


class RedisClaims:
    """Cross-process de-duplication of notifications (SET NX EX)."""

    def __init__(self, client):
        self.client = client

    def claim(self, key: str, ttl: int = 600) -> bool:
        if self.client is None:
            return True
        try:
            return bool(self.client.set(key, "1", nx=True, ex=ttl))
        except Exception:  # noqa: BLE001 - Redis down: deliver anyway (a duplicate is better than a missed alert)
            return True


class AlertManager:
    def __init__(self, notifier, signals: Signals, *, rules=RULES, claims=None, clock=time.time, repeat_after: float = REPEAT_AFTER):
        self.notifier, self.signals, self.rules, self.claims, self.clock, self.repeat_after = notifier, signals, rules, claims, clock, repeat_after
        self.state = {r.name: {"since": None, "firing": False, "last_notified": 0.0, "pending": None, "episode": 0} for r in rules}

    def _payload(self, rule: Rule, status: str, since):
        stamp = datetime.fromtimestamp(since or self.clock(), tz=timezone.utc).isoformat(timespec="seconds")
        return {"text": f"[{status.upper()}][{rule.severity}] {rule.name}: {rule.summary}", "alert": rule.name, "severity": rule.severity, "status": status, "since": stamp}

    def evaluate(self) -> list:
        """One pass over every rule. Returns the notifications delivered in this pass (for tests and logs)."""
        now, delivered = self.clock(), []
        for rule in self.rules:
            st = self.state[rule.name]
            try:
                active = bool(rule.check(self.signals))
            except Exception:  # noqa: BLE001 - a broken rule must not silence the others
                logger.exception("alert rule failed: %s", rule.name)
                continue
            if active:
                st["since"] = st["since"] or now
                if not st["firing"] and now - st["since"] >= rule.for_seconds:
                    st.update(firing=True, pending="firing", episode=st["episode"] + 1)
                elif st["firing"] and st["pending"] is None and now - st["last_notified"] >= self.repeat_after:
                    st["pending"] = "firing"  # still firing: a reminder, at most every repeat_after
            else:
                st["since"] = None
                if st["firing"]:
                    st.update(firing=False, pending="resolved")
            if st["pending"]:
                status = st["pending"]
                if self.claims is None or self.claims.claim(f"alert:{rule.name}:{status}:{st['episode']}"):
                    ok = self.notifier.deliver(self._payload(rule, status, st["since"] if status == "firing" else None))
                else:
                    ok = True  # another process announced it
                if ok:
                    st["last_notified"], st["pending"] = now, None
                    observability.inc("alert_notifications_total", status=status, severity=rule.severity)
                    delivered.append((rule.name, status))
                else:
                    observability.inc("alert_delivery_failures_total", rule=rule.name)  # kept pending: the next pass retries
        return delivered

    def firing(self) -> list:
        return [n for n, s in self.state.items() if s["firing"]]


# ------------------------------------------------------------------------------------------------ signals from the running application

def read_backup_status(path: str | None, now: float) -> dict:
    """The backup job's status file (``ops/backup_job.py``): {"last_success_ts": float, "restore_verification_ok": bool}. Missing/unreadable = a signal, never an exception."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {"backup_age_seconds": max(0.0, now - float(data["last_success_ts"])), "restore_verification_ok": 1 if data.get("restore_verification_ok") else 0, "backup_status_missing": 0}
    except Exception:  # noqa: BLE001
        return {"backup_status_missing": 1}


def counter_totals() -> dict:
    t = observability.total
    return {
        "runs_all": t("runs_finished_total"), "runs_bad": sum(t("runs_finished_total", status=s) for s in ("failed", "partial", "interrupted")),
        "provider_exhausted": t("provider_events_total", kind="exhausted"), "redis_unavailable": t("redis_unavailable_total"),
        "http_all": t("http_requests_total"), "http_5xx": t("http_requests_total", status="5xx"),
        "http_slow": max(0.0, observability.histogram_count("http_request_seconds") - observability.histogram_le("http_request_seconds", 5)),
    }


async def run_forever(manager: AlertManager, gather_gauges, interval: float, stop: asyncio.Event) -> None:
    """The background loop: gather signals (blocking calls in a thread), evaluate, deliver. Never raises, stops when ``stop`` is set."""
    while not stop.is_set():
        try:
            gauges = await asyncio.to_thread(gather_gauges)
            manager.signals.record(gauges, counter_totals())
            await asyncio.to_thread(manager.evaluate)
        except Exception:  # noqa: BLE001
            logger.exception("alert evaluation failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
