import json
import logging

import pytest
from pydantic import ValidationError

from config import ConfigError, load_settings

SENTINEL = "SENTINEL-must-never-appear"
PG = "postgresql://user:pw@db.internal:5432/app"
PUBLIC_PEM = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----\n"


def env(**overrides):
    base = {"GROQ_API_KEY": "k", "DATABASE_URL": PG}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def problems(**overrides):
    with pytest.raises(ConfigError) as exc:
        load_settings(env(**overrides))
    return exc.value.problems


def test_minimal_env_defaults_to_production_and_safe_policy():
    s = load_settings(env())
    assert s.app_env == "production" and s.is_production
    assert s.api_docs_enabled is False
    assert s.allowed_origins == ()
    assert s.model_name == "openai/gpt-oss-120b"
    assert s.redis_url.get_secret_value() == "redis://localhost:6379"
    assert s.telegram_enabled is False
    assert s.auth_config_required is False
    # Production without CORS origins / REDIS_URL is allowed but warned about.
    assert any("CORS_ALLOWED_ORIGINS" in w for w in s.warnings)
    assert any("REDIS_URL" in w for w in s.warnings)


def test_missing_required_settings_are_all_reported():
    with pytest.raises(ConfigError) as exc:
        load_settings({})
    assert exc.value.problems == ["GROQ_API_KEY: required", "DATABASE_URL: required"]


def test_blank_values_count_as_missing():
    assert "GROQ_API_KEY: required" in problems(GROQ_API_KEY="   ")


def test_invalid_app_env():
    assert any(p.startswith("APP_ENV") for p in problems(APP_ENV="staging"))


@pytest.mark.parametrize("url", ["mysql://u:p@h/db", "postgres://u:p@h/db", "nonsense"])
def test_database_url_scheme_rejected(url):
    assert any(p.startswith("DATABASE_URL") for p in problems(DATABASE_URL=url))


def test_postgres_driver_suffix_accepted():
    assert load_settings(env(DATABASE_URL="postgresql+psycopg2://u:p@h/db"))


def test_sqlite_only_allowed_in_test_env():
    sqlite = "sqlite:///tmp/x.db"
    assert load_settings(env(APP_ENV="test", DATABASE_URL=sqlite))
    for app_env in ("production", "development"):
        assert any("sqlite" in p for p in problems(APP_ENV=app_env, DATABASE_URL=sqlite))


def test_redis_url_scheme_validation():
    assert any(p.startswith("REDIS_URL") for p in problems(REDIS_URL="http://x"))
    assert load_settings(env(REDIS_URL="rediss://h:6380/0")).redis_url.get_secret_value() == "rediss://h:6380/0"


def test_cors_defaults_per_environment():
    assert load_settings(env(APP_ENV="development")).allowed_origins == ("http://localhost:3000",)
    assert load_settings(env(APP_ENV="production")).allowed_origins == ()


def test_cors_parsing_normalizes_and_dedupes():
    s = load_settings(env(CORS_ALLOWED_ORIGINS=" https://App.Example.com/ , https://app.example.com,https://other.example.com:8443 ,"))
    assert s.allowed_origins == ("https://app.example.com", "https://other.example.com:8443")


@pytest.mark.parametrize(
    "bad",
    ["*", "null", "app.example.com", "ftp://x.com", "https://x.com/path", "https://x.com?q=1",
     "https://user:pw@x.com", "https://x.com:notaport", "http://x.com", "https://localhost:3000"],
)
def test_cors_rejects_invalid_origins_in_production(bad):
    assert any(p.startswith("CORS_ALLOWED_ORIGINS entry #1") for p in problems(APP_ENV="production", CORS_ALLOWED_ORIGINS=bad))


def test_cors_allows_http_localhost_outside_production():
    s = load_settings(env(APP_ENV="development", CORS_ALLOWED_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"))
    assert s.allowed_origins == ("http://localhost:3000", "http://127.0.0.1:3000")
    assert problems(APP_ENV="development", CORS_ALLOWED_ORIGINS="*")


def test_docs_defaults_and_production_guard():
    assert load_settings(env(APP_ENV="development")).api_docs_enabled is True
    assert load_settings(env(APP_ENV="test")).api_docs_enabled is True
    assert load_settings(env(APP_ENV="development", API_DOCS_ENABLED="false")).api_docs_enabled is False
    assert any(p.startswith("API_DOCS_ENABLED") for p in problems(APP_ENV="production", API_DOCS_ENABLED="true"))
    assert any(p.startswith("API_DOCS_ENABLED") for p in problems(API_DOCS_ENABLED="maybe"))
    assert load_settings(env(API_DOCS_ENABLED="false")).api_docs_enabled is False


def test_auth_settings_not_enforced_until_t5():
    s = load_settings(env())
    assert s.auth_jwt_public_keys == {}
    assert (s.auth_jwt_issuer, s.auth_jwt_audience) == ("agentforge-web", "agentforge-api")
    # Not required unless AUTH_CONFIG_REQUIRED is on, and only in production.
    assert load_settings(env(AUTH_CONFIG_REQUIRED="true", APP_ENV="development"))
    assert load_settings(env(AUTH_CONFIG_REQUIRED="true", APP_ENV="test"))


def test_auth_keys_required_when_enforced_in_production():
    assert any(p.startswith("AUTH_JWT_PUBLIC_KEYS") for p in problems(AUTH_CONFIG_REQUIRED="true"))


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", "{}", json.dumps({"k1": 5}), json.dumps({"": PUBLIC_PEM}),
     json.dumps({"k1": "just text"}), json.dumps({"k1": "-----BEGIN PUBLIC KEY-----"}),
     json.dumps({"k1": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"}),
     json.dumps({"k1": "-----BEGIN EC PRIVATE KEY-----\nabc\n-----END EC PRIVATE KEY-----"}),
     json.dumps({"k1": PUBLIC_PEM, " k1": PUBLIC_PEM})],
)
def test_auth_keys_structural_validation_rejects(raw):
    # Validated whenever supplied, in any environment.
    assert any(p.startswith("AUTH_JWT_PUBLIC_KEYS") for p in problems(APP_ENV="development", AUTH_JWT_PUBLIC_KEYS=raw))


def test_auth_keys_valid_structure_accepted():
    raw = json.dumps({"k1": PUBLIC_PEM, "k2": PUBLIC_PEM})
    s = load_settings(env(AUTH_CONFIG_REQUIRED="true", AUTH_JWT_PUBLIC_KEYS=raw, AUTH_JWT_ISSUER="iss", AUTH_JWT_AUDIENCE="aud"))
    assert set(s.auth_jwt_public_keys) == {"k1", "k2"}
    assert (s.auth_jwt_issuer, s.auth_jwt_audience) == ("iss", "aud")


def test_settings_are_frozen():
    s = load_settings(env())
    with pytest.raises(ValidationError):
        s.app_env = "development"


def test_secrets_never_leak(caplog, capsys):
    secret_env = env(
        GROQ_API_KEY=f"{SENTINEL}-groq",
        DATABASE_URL=f"postgresql://u:{SENTINEL}-db@h/db",
        REDIS_URL=f"redis://:{SENTINEL}-redis@h:6379",
        TELEGRAM_BOT_TOKEN=f"{SENTINEL}-tg",
        TWILIO_ACCOUNT_SID=f"{SENTINEL}-sid",
        TWILIO_AUTH_TOKEN=f"{SENTINEL}-twilio",
        APP_ENV="development",
    )
    with caplog.at_level(logging.DEBUG):
        s = load_settings(secret_env)
        rendered = [repr(s), str(s), str(s.safe_summary()), str(s.model_dump()), s.model_dump_json()]
        # Invalid configurations that embed secrets in the offending values.
        bad_cases = [
            {**secret_env, "DATABASE_URL": f"mysql://u:{SENTINEL}-bad@h/db"},
            {**secret_env, "REDIS_URL": f"http://:{SENTINEL}-bad@h"},
            {**secret_env, "AUTH_JWT_PUBLIC_KEYS": f"{SENTINEL}-not-json"},
            {**secret_env, "AUTH_JWT_PUBLIC_KEYS": json.dumps({"k": f"-----BEGIN PRIVATE KEY-----{SENTINEL}"})},
            {**secret_env, "APP_ENV": f"{SENTINEL}-env"},
            {**secret_env, "API_DOCS_ENABLED": f"{SENTINEL}-bool"},
            {**secret_env, "CORS_ALLOWED_ORIGINS": f"https://u:{SENTINEL}@x.com"},
        ]
        for case in bad_cases:
            with pytest.raises(ConfigError) as exc:
                load_settings(case)
            rendered.append(str(exc.value))
    out = capsys.readouterr()
    haystack = "\n".join(rendered + [caplog.text, out.out, out.err])
    assert SENTINEL not in haystack
    assert s.groq_api_key.get_secret_value().endswith("-groq")  # secrets are still usable


def test_safe_summary_contains_only_flags_and_counts():
    s = load_settings(env(TELEGRAM_BOT_TOKEN="t", TWILIO_ACCOUNT_SID="a", TWILIO_AUTH_TOKEN="b"))
    assert s.safe_summary() == {
        "env": "production", "docs_enabled": False, "cors_origins": 0, "telegram_enabled": True,
        "twilio_configured": True, "auth_config_required": False, "auth_public_keys": 0,
    }


def test_run_execution_mode_defaults_to_embedded_and_accepts_only_the_two_known_modes():
    """M4-A2: ``embedded`` (the accepted M3-B/M4-A1 behaviour) is the default; ``api-only`` records runs for a separate worker."""
    assert load_settings(env()).run_execution == "embedded"
    assert load_settings(env(RUN_EXECUTION="api-only")).run_execution == "api-only"
    assert load_settings(env(RUN_EXECUTION=" EMBEDDED ")).run_execution == "embedded"
    assert any("RUN_EXECUTION" in p for p in problems(RUN_EXECUTION="worker"))
    assert "run_execution" not in load_settings(env()).safe_summary()  # the log summary is unchanged


# ---------------------------------------------------------------- sprint 3: validated rate-limit and provider configuration

def test_rate_limit_defaults_are_safe_and_complete():
    from config import RATE_LIMIT_DEFAULTS

    s = load_settings(env())
    assert s.rate_limits == RATE_LIMIT_DEFAULTS and set(RATE_LIMIT_DEFAULTS) >= {"plan_create", "run_start", "approval_decide", "reconcile", "agent_run", "ws_connects", "ws_jobs"}
    assert s.provider_tuning == {}


def test_rate_limit_and_provider_overrides_are_applied():
    s = load_settings(env(RATE_LIMIT_PLAN_CREATE="25", RATE_LIMIT_WS_JOBS="5", PROVIDER_GLOBAL_CONCURRENCY="4", PROVIDER_ADMISSION_WAIT="10"))
    assert s.rate_limits["plan_create"] == 25 and s.rate_limits["ws_jobs"] == 5 and s.rate_limits["run_start"] == 15  # untouched ones keep their default
    assert s.provider_tuning == {"PROVIDER_GLOBAL_CONCURRENCY": "4", "PROVIDER_ADMISSION_WAIT": "10"}


@pytest.mark.parametrize("name,value", [("RATE_LIMIT_PLAN_CREATE", "0"), ("RATE_LIMIT_PLAN_CREATE", "-3"), ("RATE_LIMIT_RUN_START", "100000"), ("RATE_LIMIT_AGENT_RUN", "lots"), ("RATE_LIMIT_WS_CONNECTS", "1.5"),
                                         ("PROVIDER_MAX_ATTEMPTS", "0"), ("PROVIDER_MAX_ATTEMPTS", "99"), ("PROVIDER_CONCURRENCY", "-1"), ("PROVIDER_GLOBAL_CONCURRENCY", "100000"),
                                         ("PROVIDER_BASE_DELAY", "0"), ("PROVIDER_BUDGET", "nan"), ("PROVIDER_ADMISSION_WAIT", "-1")])
def test_invalid_or_dangerous_values_are_refused_by_name_without_echoing_the_value(name, value):
    found = problems(**{name: value})
    assert any(p.startswith(name + ":") for p in found)
    if len(value) > 3:  # short values ("0") legitimately occur inside the bounds text
        assert all(value not in p for p in found)  # the offending value is never echoed


# ---------------------------------------------------------------- sprint 4: the real email tool is opt-in, sandboxed by default, and its secret never echoes

def test_the_email_tool_is_off_by_default_and_sandboxed_when_enabled():
    s = load_settings(env())
    assert s.resend_api_key is None and s.email_from is None and s.email_allowed_domains == ("resend.dev",)
    on = load_settings(env(RESEND_API_KEY="re_test_key", EMAIL_FROM="AgentForge <sandbox@resend.dev>"))
    assert on.resend_api_key.get_secret_value() == "re_test_key" and on.email_allowed_domains == ("resend.dev",)
    assert "re_test_key" not in repr(on) and "re_test_key" not in str(on.model_dump())  # a SecretStr: never printed


def test_enabling_real_recipients_is_an_explicit_validated_choice():
    s = load_settings(env(RESEND_API_KEY="k", EMAIL_FROM="a@b.co", EMAIL_ALLOWED_DOMAINS="Example.com, mail.example.org"))
    assert s.email_allowed_domains == ("example.com", "mail.example.org")


@pytest.mark.parametrize("overrides,name", [(dict(RESEND_API_KEY="k"), "EMAIL_FROM"), (dict(RESEND_API_KEY="k", EMAIL_FROM="not an address"), "EMAIL_FROM"),
                                           (dict(RESEND_API_KEY="k", EMAIL_FROM="a@b.co\r\nBcc: x@y.co"), "EMAIL_FROM"), (dict(EMAIL_ALLOWED_DOMAINS="*"), "EMAIL_ALLOWED_DOMAINS"),
                                           (dict(EMAIL_ALLOWED_DOMAINS=",,"), "EMAIL_ALLOWED_DOMAINS")])
def test_invalid_email_configuration_is_refused_by_name(overrides, name):
    found = problems(**overrides)
    assert any(p.startswith(name + ":") for p in found)
    assert all("RESEND" not in p or "k" != p for p in found)


def test_alert_webhook_configuration_is_validated_and_secret():
    s = load_settings(env(ALERT_WEBHOOK_URL="https://hooks.example.com/services/T000/B000/XXXX", ALERT_EVAL_INTERVAL="60", BACKUP_STATUS_FILE="/var/lib/agentforge/backup-status.json"))
    assert s.alert_eval_interval == 60.0 and s.backup_status_file.endswith("backup-status.json")
    assert "XXXX" not in repr(s) and "XXXX" not in str(s.model_dump())  # the webhook URL embeds a token: never printed
    assert load_settings(env()).alert_webhook_url is None  # no webhook = the rules stay definitions, nothing is evaluated live


@pytest.mark.parametrize("overrides", [dict(ALERT_WEBHOOK_URL="http://hooks.example.com/x"), dict(ALERT_WEBHOOK_URL="ftp://x"), dict(ALERT_EVAL_INTERVAL="1"), dict(ALERT_EVAL_INTERVAL="99999"),
                                       dict(ALERT_EVAL_INTERVAL="soon")])
def test_invalid_alert_configuration_is_refused(overrides):
    name = next(iter(overrides))
    assert any(p.startswith(name + ":") for p in problems(**overrides))


def test_plain_http_alert_webhooks_are_allowed_only_to_loopback_outside_production():
    assert load_settings(env(APP_ENV="development", ALERT_WEBHOOK_URL="http://127.0.0.1:9/hook")).alert_webhook_url is not None
    assert any(p.startswith("ALERT_WEBHOOK_URL:") for p in problems(ALERT_WEBHOOK_URL="http://127.0.0.1:9/hook"))  # production is the default environment


def test_sentinel_configuration_is_validated():
    s = load_settings(env(REDIS_SENTINELS="s1.internal:26379, s2.internal:26379", REDIS_SENTINEL_MASTER="agentforge"))
    assert s.redis_sentinels == (("s1.internal", 26379), ("s2.internal", 26379)) and s.redis_sentinel_master == "agentforge"
    assert load_settings(env()).redis_sentinels == ()
    for bad in (dict(REDIS_SENTINELS="s1.internal"), dict(REDIS_SENTINELS="s1:notaport", REDIS_SENTINEL_MASTER="m"), dict(REDIS_SENTINELS="s1:26379"), dict(REDIS_SENTINEL_MASTER="m"),
                dict(REDIS_SENTINELS="s1:70000", REDIS_SENTINEL_MASTER="m")):
        assert any(p.startswith(("REDIS_SENTINELS:", "REDIS_SENTINEL_MASTER:")) for p in problems(**bad)), bad


def test_the_redis_client_factory_builds_without_connecting():
    import redis_client

    single = redis_client.build("redis://127.0.0.1:1/3")
    assert single.connection_pool.connection_kwargs["db"] == 3
    managed = redis_client.build("redis://:pw@127.0.0.1:1/2", (("127.0.0.1", 1),), "agentforge")  # a Sentinel-managed master: nothing is contacted until first use
    assert type(managed.connection_pool).__name__ == "SentinelConnectionPool"
    with pytest.raises(ValueError):
        redis_client.build("redis://127.0.0.1:1/0", (("127.0.0.1", 1),), None)


@pytest.mark.parametrize("name,value", [("PROVIDER_LEASE_SECONDS", "5"), ("PROVIDER_LEASE_SECONDS", "100000"), ("PROVIDER_LEASE_SECONDS", "soon")])
def test_the_provider_lease_is_range_validated_and_defaults_to_one_call_plus_margin(name, value):
    assert any(p.startswith(name + ":") for p in problems(**{name: value}))
    from agentplatform.resilience import RetryPolicy

    assert RetryPolicy().lease_seconds == 90.0 and RetryPolicy.from_env({"PROVIDER_LEASE_SECONDS": "120"}).lease_seconds == 120.0
