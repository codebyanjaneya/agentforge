"""Sprint 4, workstream C: application-level edge protections (defence in depth behind the reverse proxy): the request-size limit and the Host allowlist, plus their validated configuration. The proxy is the
first line (ops/nginx/agentforge.conf, exercised by ops/edge_drill.sh); these tests prove the application refuses on its own too."""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app_factory import build_app
from config import ConfigError, load_settings

BASE = {"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app", "APP_ENV": "development"}


def client(**env):
    app = build_app(load_settings({**BASE, **env}))
    calls = {"n": 0}

    @app.post("/echo")
    async def echo(request: Request):
        calls["n"] += 1
        body = await request.body()
        return {"size": len(body)}

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return TestClient(app), calls


def test_a_declared_oversize_body_is_refused_with_413_before_the_application_reads_it():
    c, calls = client(MAX_REQUEST_BYTES="2048")
    r = c.post("/echo", content=b"x" * 5000)
    assert r.status_code == 413 and r.json() == {"detail": {"code": "request_too_large", "message": "The request body is too large."}} and calls["n"] == 0
    assert c.post("/echo", content=b"x" * 2048).json() == {"size": 2048}  # exactly the limit is fine
    assert c.get("/ping").status_code == 200


def test_a_streamed_body_without_content_length_is_counted_and_refused_when_it_crosses_the_limit():
    c, calls = client(MAX_REQUEST_BYTES="2048")

    def chunks():
        for _ in range(10):
            yield b"y" * 1000  # chunked transfer: no Content-Length

    r = c.post("/echo", content=chunks())
    assert r.status_code == 413 and r.json()["detail"]["code"] == "request_too_large"
    small = c.post("/echo", content=(b"z" * 500 for _ in range(2)))
    assert small.status_code == 200 and small.json() == {"size": 1000}


def test_the_default_limit_is_one_mebibyte():
    c, _ = client()
    assert c.post("/echo", content=b"a" * (1_048_576 + 1)).status_code == 413
    assert c.post("/echo", content=b"a" * 100_000).status_code == 200


def test_the_host_allowlist_refuses_other_hosts_and_supports_wildcards():
    c, calls = client(ALLOWED_HOSTS="api.example.com,*.internal.example.org")
    assert c.get("/ping", headers={"host": "api.example.com"}).status_code == 200
    assert c.get("/ping", headers={"host": "svc.internal.example.org"}).status_code == 200
    bad = c.get("/ping", headers={"host": "evil.example.net"})
    assert bad.status_code == 400 and "evil" not in bad.text.lower().replace("invalid host header", "")
    assert c.get("/ping", headers={"host": "api.example.com.evil.net"}).status_code == 400


def test_no_allowlist_means_no_host_check_and_production_warns_about_it():
    c, _ = client()
    assert c.get("/ping", headers={"host": "anything.example"}).status_code == 200
    warned = load_settings({"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app"})
    assert any("ALLOWED_HOSTS" in w for w in warned.warnings)
    assert not any("ALLOWED_HOSTS" in w for w in load_settings({"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:pw@db.internal/app", "ALLOWED_HOSTS": "api.example.com"}).warnings)


@pytest.mark.parametrize("env,name", [(dict(MAX_REQUEST_BYTES="10"), "MAX_REQUEST_BYTES"), (dict(MAX_REQUEST_BYTES="99999999999"), "MAX_REQUEST_BYTES"), (dict(MAX_REQUEST_BYTES="big"), "MAX_REQUEST_BYTES"),
                                      (dict(ALLOWED_HOSTS="*"), "ALLOWED_HOSTS"), (dict(ALLOWED_HOSTS="a b"), "ALLOWED_HOSTS"), (dict(ALLOWED_HOSTS=","), "ALLOWED_HOSTS")])
def test_invalid_edge_configuration_is_refused_by_name(env, name):
    with pytest.raises(ConfigError) as exc:
        load_settings({**BASE, **env})
    assert any(p.startswith(name + ":") for p in exc.value.problems)
