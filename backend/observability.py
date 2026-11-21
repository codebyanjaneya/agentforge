"""Observability baseline (sprint 2, P3): bounded-cardinality metrics, correlated structured logs.

METRICS are in-process counters/histograms rendered in the Prometheus text format at ``GET /metrics`` (enabled only when ``METRICS_TOKEN`` is configured; bearer-protected). They are PER PROCESS: a scraper
must scrape every API/worker process, and gauges that describe shared state (running runs, unknown outcomes) are read from the database at scrape time so they are the same whichever process answers.
Labels are a fixed vocabulary (status, error code, decision, provider event kind, provider, model): never a user id, workspace id, run id or any free text. Recording never raises.

LOGS: ``bind(...)`` puts workspace / plan / run / task / execution / invocation / provider-request ids in a context variable; ``ContextFilter`` copies them onto every log record and ``JsonFormatter``
(LOG_FORMAT=json) prints one JSON object per line. Messages are the existing log messages, which never carry tokens, credentials or tool arguments; ``redact`` is applied to the message as a second line
of defence.
"""

import contextvars
import json
import logging
import re
import threading
from contextlib import contextmanager

_lock = threading.Lock()
_counters: dict = {}
_hist: dict = {}
BUCKETS = (0.05, 0.25, 1, 5, 30, 120, 600, 3600)
HELP = {
    "agentforge_runs_finished_total": "Plan runs that reached a final status.",
    "agentforge_tasks_finished_total": "Plan tasks that reached a final status.",
    "agentforge_tool_invocations_total": "Tool invocations by final status and error code.",
    "agentforge_dispatch_unknown_total": "Side-effecting dispatches whose outcome could not be proven.",
    "agentforge_approval_decisions_total": "Human approval decisions.",
    "agentforge_approval_wait_seconds": "Time from an approval request to its decision.",
    "agentforge_provider_events_total": "Model provider waits, retries, recoveries and exhaustions by kind.",
    "agentforge_provider_wait_seconds": "Time slept waiting to retry a provider request.",
    "agentforge_model_tokens_total": "Model tokens by provider, model and direction.",
    "agentforge_model_cost_micros_total": "Calculated model cost in millionths of a dollar.",
    "agentforge_lease_takeovers_total": "Run leases taken over from an expired owner.",
    "agentforge_rate_limited_total": "Requests refused by a rate limit, by bucket.",
    "agentforge_http_requests_total": "HTTP requests by route template (never the raw path) and status class.",
    "agentforge_http_request_seconds": "HTTP request latency by route template.",
    "agentforge_readiness_failures_total": "Readiness probes that failed (database unreachable).",
    "agentforge_alert_notifications_total": "Alert notifications delivered, by status (firing/resolved) and severity.",
    "agentforge_alert_delivery_failures_total": "Alert notifications that could not be delivered (kept pending and retried), by rule.",
    "agentforge_provider_admission_total": "Provider admissions by decision (granted/full/refused/degraded) and reason (ok, cap_reached, store_unavailable, server_moved, replica_ack_missing).",
    "agentforge_redis_unavailable_total": "Redis calls that failed (feature, mode): the feature degraded to per-process limits or failed closed.",
}


def _key(name, labels):
    return (name, tuple(sorted((k, str(v)[:48]) for k, v in labels.items())))


def inc(name: str, value: float = 1, **labels) -> None:
    try:
        with _lock:
            k = _key("agentforge_" + name, labels)
            _counters[k] = _counters.get(k, 0) + value
    except Exception:  # noqa: BLE001 - metrics must never break the work
        pass


def observe(name: str, seconds: float, **labels) -> None:
    try:
        with _lock:
            k = _key("agentforge_" + name, labels)
            h = _hist.setdefault(k, [[0] * len(BUCKETS), 0, 0.0])
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[0][i] += 1
            h[1] += 1
            h[2] += seconds
    except Exception:  # noqa: BLE001
        pass


def total(name: str, **labels) -> float:
    """Sum of a counter over every label set that contains the given labels (e.g. ``total("runs_finished_total", status="failed")``)."""
    full, want = "agentforge_" + name, {k: str(v) for k, v in labels.items()}
    with _lock:
        return float(sum(v for (n, lab), v in _counters.items() if n == full and all(dict(lab).get(k) == x for k, x in want.items())))


def histogram_count(name: str) -> float:
    full = "agentforge_" + name
    with _lock:
        return float(sum(h[1] for (n, _), h in _hist.items() if n == full))


def histogram_le(name: str, le: float) -> float:
    """Observations at or below ``le`` (one of BUCKETS) across every label set."""
    full, i = "agentforge_" + name, BUCKETS.index(le)
    with _lock:
        return float(sum(h[0][i] for (n, _), h in _hist.items() if n == full))


def reset() -> None:
    with _lock:
        _counters.clear()
        _hist.clear()


def _fmt(labels, extra=()):
    items = list(labels) + list(extra)
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}" if items else ""


def render(gauges: dict | None = None) -> str:
    """The Prometheus text exposition of every counter, histogram and the given gauges ({name: value})."""
    lines = []
    with _lock:
        counters, hist = dict(_counters), {k: [list(v[0]), v[1], v[2]] for k, v in _hist.items()}
    seen = set()
    for (name, labels), value in sorted(counters.items()):
        if name not in seen:
            lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
            seen.add(name)
        lines.append(f"{name}{_fmt(labels)} {value}")
    for (name, labels), (buckets, count, total) in sorted(hist.items()):
        if name not in seen:
            lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} histogram"]
            seen.add(name)
        for b, n in zip(BUCKETS, buckets):
            lines.append(f"{name}_bucket{_fmt(labels, [('le', b)])} {n}")
        lines += [f'{name}_bucket{_fmt(labels, [("le", "+Inf")])} {count}', f"{name}_sum{_fmt(labels)} {total}", f"{name}_count{_fmt(labels)} {count}"]
    for name, value in sorted((gauges or {}).items()):
        lines += [f"# TYPE agentforge_{name} gauge", f"agentforge_{name} {value}"]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------------ correlated logs

_context: contextvars.ContextVar = contextvars.ContextVar("agentforge_log_context", default={})
FIELDS = ("workspace", "plan", "run", "task", "execution", "invocation", "provider_request", "approval", "dispatch")
_SECRET = re.compile(r"(?i)(authorization[\"'\s:=]+(?:bearer\s+)?[^\s\"',}]+|bearer\s+[a-z0-9._~+/=-]+|(?:api[_-]?key|token|secret|password)[\"'\s:=]+[^\s\"',}]+)")


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


@contextmanager
def bind(**ids):
    """Attach correlation ids to every log line emitted inside the block (and inside tasks/threads that copy the context)."""
    current = {**_context.get(), **{k: str(v) for k, v in ids.items() if k in FIELDS and v is not None}}
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def set_context(**ids) -> None:
    """Like ``bind`` without a block: for the start of an asyncio task / worker thread, whose context is its own and ends with it."""
    _context.set({**_context.get(), **{k: str(v) for k, v in ids.items() if k in FIELDS and v is not None}})


class ContextFilter(logging.Filter):
    def filter(self, record):
        for k, v in _context.get().items():
            setattr(record, k, v)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname, "logger": record.name, "msg": redact(record.getMessage())}
        out.update({k: getattr(record, k) for k in FIELDS if hasattr(record, k)})
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__  # the type only: a traceback can carry argument values
        return json.dumps(out)


def configure_logging(fmt: str) -> None:
    root = logging.getLogger()
    if not root.handlers and fmt == "json":
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(ContextFilter())
        if fmt == "json":
            handler.setFormatter(JsonFormatter())
