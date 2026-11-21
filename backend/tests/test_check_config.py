"""Sprint 4: the deployment gate ``ops/check_config.py`` blocks an unsafe staging/production environment and passes a safe one, and never prints a secret."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "check_config.py"
PEM = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----\n"
GOOD = {"APP_ENV": "production", "GROQ_API_KEY": "k-secret-groq", "DATABASE_URL": "postgresql://u:pw-secret@db.internal:5432/app?sslmode=require", "REDIS_URL": "redis://redis.internal:6379",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com", "ALLOWED_HOSTS": "api.example.com", "AUTH_CONFIG_REQUIRED": "1", "AUTH_JWT_PUBLIC_KEYS": json.dumps({"k1": PEM}),
        "METRICS_TOKEN": "m" * 32, "PROVIDER_REDIS_DOWN": "closed", "RUN_EXECUTION": "api-only", "ALERT_WEBHOOK_URL": "https://hooks.example.com/services/SECRET-WEBHOOK-TOKEN",
        "BACKUP_STATUS_FILE": "/var/lib/agentforge/backup-status.json"}


def run(monkeypatch, capsys, env, profile="production"):
    for k in list(__import__("os").environ):
        if k in GOOD or k in ("AGENTFORGE_DEMO_MODE", "API_DOCS_ENABLED", "RESEND_API_KEY", "EMAIL_FROM"):
            monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    spec = importlib.util.spec_from_file_location("check_config", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    code = mod.main(["--profile", profile])
    return code, capsys.readouterr().out


def test_a_safe_production_environment_passes_and_prints_no_secret(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, GOOD)
    assert code == 0 and "CONFIG GATE PASSED" in out
    for secret in ("pw-secret", "k-secret-groq", "SECRET-WEBHOOK-TOKEN", "m" * 32):
        assert secret not in out


@pytest.mark.parametrize("change,expected", [({"APP_ENV": "development"}, "APP_ENV"), ({"AGENTFORGE_DEMO_MODE": "1"}, "AGENTFORGE_DEMO_MODE"), ({"AUTH_CONFIG_REQUIRED": "0"}, "AUTH_CONFIG_REQUIRED"),
                                             ({"CORS_ALLOWED_ORIGINS": "http://app.example.com"}, "CORS_ALLOWED_ORIGINS"), ({"ALLOWED_HOSTS": ""}, "ALLOWED_HOSTS"),
                                             ({"METRICS_TOKEN": "short"}, "METRICS_TOKEN"), ({"DATABASE_URL": "sqlite:///x.db"}, "DATABASE_URL")])
def test_unsafe_settings_block_the_gate(monkeypatch, capsys, change, expected):
    env = {**GOOD, **change}
    env = {k: v for k, v in env.items() if v != ""}
    code, out = run(monkeypatch, capsys, env)
    assert code == 1 and ("CONFIG GATE FAILED" in out or "refuses this configuration" in out) and expected in out  # the gate's own rule OR the application's own validation: either way it blocks


def test_an_invalid_value_is_reported_by_the_applications_own_validation(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, {**GOOD, "RATE_LIMIT_PLAN_CREATE": "0"})
    assert code == 1 and "RATE_LIMIT_PLAN_CREATE" in out


def test_warnings_do_not_block_but_are_shown(monkeypatch, capsys):
    env = {**GOOD, "PROVIDER_REDIS_DOWN": "degrade", "RUN_EXECUTION": "embedded"}
    env.pop("ALERT_WEBHOOK_URL")
    code, out = run(monkeypatch, capsys, env)
    assert code == 0 and "WARN" in out and "degrade" in out and "ALERT_WEBHOOK_URL" in out


def test_the_staging_template_stays_valid_against_the_applications_validation_and_the_gate(monkeypatch, capsys):
    """ops/staging.env.example is documentation that must not rot: with its placeholders filled by dummy values it has to pass both the application's own validation and the deployment gate."""
    import re

    template = (Path(__file__).resolve().parents[2] / "ops" / "staging.env.example").read_text(encoding="utf-8")
    env = {}
    for line in template.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = re.sub(r"\s+#.*$", "", value)  # an inline comment
        env[key] = re.sub(r"<[^>]*>", "placeholdervalueplaceholdervalue", value)
    env["AUTH_JWT_PUBLIC_KEYS"] = json.dumps({"staging-1": PEM})  # a real value is JSON with a real PEM; the placeholder text above only documents the shape
    code, out = run(monkeypatch, capsys, env, profile="staging")
    assert code == 0, out
