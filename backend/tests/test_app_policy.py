"""CORS and API-docs policy. Uses app_factory only: no database, Redis, Groq or Telegram."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app_factory import build_app
from config import load_settings

BASE = {"GROQ_API_KEY": "k", "DATABASE_URL": "postgresql://u:p@h/db"}
BACKEND = Path(__file__).resolve().parent.parent


def client(**env):
    app = build_app(load_settings({**BASE, **env}))
    app.add_api_route("/health", lambda: {"status": "ok"}, methods=["GET"])
    return TestClient(app)


def preflight(c, origin, method="POST", headers="content-type"):
    return c.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers},
    )


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_available_in_development(path):
    assert client(APP_ENV="development").get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_disabled_in_production(path):
    assert client(APP_ENV="production").get(path).status_code == 404


def test_docs_disabled_by_default_when_env_unset():
    assert client().get("/docs").status_code == 404


def test_docs_can_be_disabled_in_development():
    assert client(APP_ENV="development", API_DOCS_ENABLED="false").get("/openapi.json").status_code == 404


def test_allowed_origin_preflight_without_credentials():
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    r = preflight(c, "https://app.example.com")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-credentials" not in r.headers


def test_unlisted_origin_rejected():
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    r = preflight(c, "https://evil.example.com")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers


def test_production_default_has_no_cors_access():
    c = client()
    assert "access-control-allow-origin" not in preflight(c, "https://app.example.com").headers
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "https://app.example.com"}).headers


def test_development_allows_localhost_3000_only():
    c = client(APP_ENV="development")
    assert preflight(c, "http://localhost:3000").headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in preflight(c, "http://localhost:3001").headers


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
def test_allowed_methods(method):
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    assert preflight(c, "https://app.example.com", method=method).status_code == 200


@pytest.mark.parametrize("method", ["PUT", "TRACE"])
def test_disallowed_methods(method):
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    assert preflight(c, "https://app.example.com", method=method).status_code == 400


def test_allowed_and_disallowed_headers():
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    assert preflight(c, "https://app.example.com", headers="authorization, content-type").status_code == 200
    assert preflight(c, "https://app.example.com", headers="x-custom").status_code == 400


def test_simple_request_from_unlisted_origin_gets_no_cors_headers():
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    r = c.get("/health", headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 200 and r.json() == {"status": "ok"}  # CORS is enforced by browsers
    assert "access-control-allow-origin" not in r.headers


def test_policy_modules_do_not_import_database_or_agents():
    code = (
        "import sys, config, app_factory;"
        "bad=[m for m in ('memory.long_term','orchestrator','main','telegram_bot','groq','redis') if m in sys.modules];"
        "print(bad); sys.exit(1 if bad else 0)"
    )
    # Inherit the normal environment (a stripped one makes Windows Store Python create stray dirs).
    child_env = {**os.environ, "PYTHONPATH": str(BACKEND), "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, env=child_env)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_workspace_header_is_an_allowed_cors_request_header_and_nothing_wider():
    """M6: a browser sends X-Workspace-Id (the workspace a person selected; the server verifies membership), so the preflight must allow it. Found by the real-browser run."""
    c = client(CORS_ALLOWED_ORIGINS="https://app.example.com")
    r = preflight(c, "https://app.example.com", headers="authorization,content-type,x-workspace-id")
    assert r.status_code == 200 and "x-workspace-id" in r.headers["access-control-allow-headers"].lower()
    assert preflight(c, "https://app.example.com", headers="x-user-email").status_code == 400  # identity headers stay refused
