"""Sprint 2, P3: metrics (bounded labels, Prometheus text), correlated JSON logs that never carry secrets, the protected /metrics endpoint, and the real instrumentation points firing on a real run."""

import io
import json
import logging

import pytest
from pydantic import SecretStr

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.auth_support import auth_db  # noqa: F401,F811


import observability as ob


@pytest.fixture(autouse=True)
def clean():
    ob.reset()
    yield
    ob.reset()


def test_counters_and_histograms_render_in_the_prometheus_text_format():
    ob.inc("runs_finished_total", status="succeeded", code="none")
    ob.inc("runs_finished_total", status="succeeded", code="none")
    ob.observe("approval_wait_seconds", 3.0)
    text = ob.render({"runs_running": 2})
    assert 'agentforge_runs_finished_total{code="none",status="succeeded"} 2' in text
    assert 'agentforge_approval_wait_seconds_bucket{le="5"} 1' in text and "agentforge_approval_wait_seconds_count 1" in text
    assert "agentforge_runs_running 2" in text and "# TYPE agentforge_runs_finished_total counter" in text


def test_recording_never_raises_and_label_values_are_truncated():
    ob.inc("x", **{"a": object()})  # an unrenderable value is simply stringified
    ob.inc("y", note="z" * 500)
    assert all(len(v) <= 48 for (_, labels) in ob._counters for _, v in labels)


def test_json_logs_carry_correlation_ids_and_never_secrets():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ob.JsonFormatter())
    handler.addFilter(ob.ContextFilter())
    log = logging.getLogger("agentforge.test_obs")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        with ob.bind(workspace="w1", run="r1", task="t1", execution="e1", ignored="nope"):
            log.info("calling the provider with Authorization: Bearer abc.def.ghi and api_key=sk-12345")
        log.info("outside the block")
    finally:
        log.removeHandler(handler)
    first, second = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert (first["workspace"], first["run"], first["task"], first["execution"]) == ("w1", "r1", "t1", "e1") and "ignored" not in first
    assert "abc.def.ghi" not in first["msg"] and "sk-12345" not in first["msg"] and "[redacted]" in first["msg"]
    assert "run" not in second  # the context ended with the block


def test_metrics_endpoint_is_disabled_without_a_token_and_hides_whether_the_token_was_wrong(api, monkeypatch):
    import main

    assert api.client.get("/metrics").status_code == 404  # no METRICS_TOKEN configured: the endpoint does not exist
    monkeypatch.setattr(main, "settings", main.settings.model_copy(update={"metrics_token": SecretStr("s3cret-token")}))
    assert api.client.get("/metrics").status_code == 404 and api.client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 404
    import sys

    sys.modules["observability"].inc("runs_finished_total", status="failed", code="timeout")  # the instance the app under test uses
    r = api.client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"})
    assert r.status_code == 200 and 'status="failed"' in r.text
    assert "s3cret-token" not in r.text


def test_http_metrics_use_the_route_template_never_the_raw_path_or_identity(api):
    import sys

    sys.modules["observability"].reset()
    api.client.get("/definitely/not/a/route/" + "x" * 40)
    api.client.get("/plans", headers=api.auth("user-a", "a@example.com"))
    api.client.get("/health")
    text = sys.modules["observability"].render()
    assert 'agentforge_http_requests_total{route="unmatched",status="4xx"} 1' in text  # a scanner probing random paths is ONE label value
    assert 'route="/plans"' in text and "definitely" not in text and "user-a" not in text and "a@example.com" not in text
    assert 'route="/health"' not in text  # probes do not drown the signal
