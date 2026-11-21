"""Centralised, typed, fail-fast application settings (M0.1 / T0).

Design rules:
- Plain pydantic ``BaseModel`` + explicit environment parsing (no pydantic-settings).
- ``load_settings`` takes an explicit mapping so tests never depend on ``os.environ``.
- Every problem is reported as ``VARIABLE: reason``. **Values are never included**
  in errors, summaries or reprs (secrets are ``SecretStr``).
- Legacy modules (agents, analyzer, voice_handler, short_term, scheduler,
  self_improver, telegram_bot) still read some variables directly; see
  ``tests/test_no_stray_env_access.py`` for the allow-list that must only shrink.
"""

import json
import re
from urllib.parse import urlparse
import os
from collections.abc import Mapping
from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, SecretStr

ENVIRONMENTS = ("development", "test", "production")
DEFAULT_MODEL_NAME = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile was retired by the provider; this one is verified live with tool calling
DEFAULT_REDIS_URL = "redis://localhost:6379"
DEFAULT_DEV_ORIGINS = ("http://localhost:3000",)
DEFAULT_JWT_ISSUER = "agentforge-web"
DEFAULT_JWT_AUDIENCE = "agentforge-api"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


# Sprint 3: per-user limits (requests per minute), overridable as RATE_LIMIT_<NAME> (e.g. RATE_LIMIT_PLAN_CREATE=30). A value outside [1, RATE_LIMIT_MAX] is a configuration error:
# 0 would lock the route for everyone and a huge value is "no limit" in disguise. WebSocket: RATE_LIMIT_WS_CONNECTS / RATE_LIMIT_WS_JOBS.
RATE_LIMIT_DEFAULTS = {"plan_create": 15, "plan_revise": 40, "plan_approve": 40, "run_start": 15, "run_retry": 15, "approval_decide": 40, "reconcile": 20, "agent_run": 30, "improve": 6,
                       "ws_connects": 30, "ws_jobs": 30}
RATE_LIMIT_MAX = 6000
# Provider tuning: name -> (kind, minimum, maximum). Anything else (or out of range) is a configuration error, not silently ignored.
PROVIDER_BOUNDS = {"PROVIDER_MAX_ATTEMPTS": (int, 1, 10), "PROVIDER_BASE_DELAY": (float, 0.01, 60.0), "PROVIDER_MAX_WAIT": (float, 1.0, 600.0), "PROVIDER_BUDGET": (float, 1.0, 1800.0),
                   "PROVIDER_CONCURRENCY": (int, 1, 64), "PROVIDER_GLOBAL_CONCURRENCY": (int, 1, 256), "PROVIDER_ADMISSION_WAIT": (float, 0.0, 300.0), "PROVIDER_LEASE_SECONDS": (float, 30.0, 900.0)}


class ConfigError(Exception):
    """Invalid configuration. Lists variable names and reasons, never values."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(
            "Invalid configuration:\n" + "\n".join(f" - {p}" for p in self.problems)
        )


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    app_env: Literal["development", "test", "production"]
    groq_api_key: SecretStr
    model_name: str
    database_url: SecretStr
    redis_url: SecretStr
    telegram_bot_token: SecretStr | None = None
    twilio_account_sid: SecretStr = SecretStr("")
    twilio_auth_token: SecretStr = SecretStr("")
    allowed_origins: tuple[str, ...] = ()
    api_docs_enabled: bool = False
    # Enforcement of signing-key presence waits for T5; parsing is validated now.
    auth_config_required: bool = False
    auth_jwt_issuer: str = DEFAULT_JWT_ISSUER
    auth_jwt_audience: str = DEFAULT_JWT_AUDIENCE
    auth_jwt_public_keys: dict[str, str] = {}
    # M4-A2: who executes plan runs. "embedded" (default): this API process drives the runs it creates (the accepted M3-B/M4-A1
    # behaviour). "api-only": the API only records runs as unclaimed; a separate worker process (``python worker.py``) claims them.
    run_execution: Literal["embedded", "api-only"] = "embedded"
    # DEMO MODE (never in production): the model is a SCRIPTED stand-in and the sending tool is a SIMULATION. No model key is needed. See agentplatform/demo.py.
    demo_mode: bool = False
    # Sprint 2: METRICS_TOKEN enables GET /metrics (bearer-protected; unset = no such endpoint). LOG_FORMAT=json prints one JSON object per log line.
    metrics_token: SecretStr | None = None
    # Sprint 4: the real email tool (email.send@1, Resend). It exists only when RESEND_API_KEY is set; EMAIL_FROM is then required; recipients are limited to EMAIL_ALLOWED_DOMAINS (default: resend.dev, the
    # provider's TEST addresses). A deployment must opt in to real recipients explicitly.
    resend_api_key: SecretStr | None = None
    email_from: str | None = None
    email_allowed_domains: tuple[str, ...] = ("resend.dev",)
    alert_webhook_url: SecretStr | None = None  # ALERT_WEBHOOK_URL: https (loopback http outside production), a secret (webhooks embed tokens)
    alert_eval_interval: float = 30.0  # ALERT_EVAL_INTERVAL seconds, 5..3600
    backup_status_file: str | None = None  # BACKUP_STATUS_FILE: written by ops/backup_job.py
    redis_sentinels: tuple[tuple[str, int], ...] = ()  # REDIS_SENTINELS=host:port,host:port (+ REDIS_SENTINEL_MASTER): rate limits and provider coordination follow a Sentinel failover
    redis_sentinel_master: str | None = None
    max_request_bytes: int = 1_048_576  # MAX_REQUEST_BYTES: request bodies above this are refused with 413 (the proxy should refuse first)
    allowed_hosts: tuple[str, ...] = ()  # ALLOWED_HOSTS=api.example.com,*.example.com: Host-header allowlist (strongly recommended in production)
    provider_redis_down: Literal["degrade", "closed"] = "closed"  # PROVIDER_REDIS_DOWN: what provider admission does while the shared accounting cannot be trusted (Redis down, or moved/restarted). closed = refuse (DEFAULT, the global cap is never silently dropped); degrade = explicit opt-in to per-process limits only
    rate_limits: dict[str, int] = {}  # validated per-user limits per minute (defaults merged in)
    provider_tuning: dict[str, str] = {}  # PROVIDER_MAX_ATTEMPTS / _BASE_DELAY / _MAX_WAIT / _BUDGET / _CONCURRENCY, as given
    log_format: Literal["text", "json"] = "text"
    warnings: tuple[str, ...] = ()

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def telegram_enabled(self) -> bool:
        return self.telegram_bot_token is not None

    def safe_summary(self) -> dict:
        """Booleans/counts only: safe to log."""
        return {
            "env": self.app_env,
            "docs_enabled": self.api_docs_enabled,
            "cors_origins": len(self.allowed_origins),
            "telegram_enabled": self.telegram_enabled,
            "twilio_configured": bool(
                self.twilio_account_sid.get_secret_value()
                and self.twilio_auth_token.get_secret_value()
            ),
            "auth_config_required": self.auth_config_required,
            "auth_public_keys": len(self.auth_jwt_public_keys),
        }


def _scheme(url: str) -> str:
    """Base scheme without driver suffix (``postgresql+psycopg2`` -> ``postgresql``)."""
    if ":" not in url:
        return ""
    return url.split(":", 1)[0].lower().split("+", 1)[0]


def _normalize_origin(raw: str, production: bool) -> tuple[str | None, str | None]:
    """Return (origin, None) or (None, reason). The value itself is never echoed."""
    from urllib.parse import urlsplit

    if raw in ("*", "null"):
        return None, "wildcard/null origins are not allowed"
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError on an invalid port
    except ValueError:
        return None, "is not a valid origin"
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None, "must be http(s)://host[:port]"
    if "@" in parts.netloc:
        return None, "must not contain credentials"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None, "must not contain a path, query or fragment"
    host = (parts.hostname or "").lower()
    host = f"[{host}]" if ":" in host else host
    if production and (parts.scheme != "https" or host in _LOCAL_HOSTS):
        return None, "must be a non-local https origin in production"
    return f"{parts.scheme}://{parts.netloc.lower()}", None


def _parse_public_keys(raw: str, problems: list[str]) -> dict[str, str]:
    """Structural validation only. Cryptographic (EC P-256) parsing arrives in T5."""
    name = "AUTH_JWT_PUBLIC_KEYS"
    try:
        data = json.loads(raw)
    except ValueError:
        problems.append(f"{name}: must be a JSON object mapping key id to PEM public key")
        return {}
    if not isinstance(data, dict) or not data:
        problems.append(f"{name}: must be a non-empty JSON object (key id -> PEM)")
        return {}
    keys: dict[str, str] = {}
    for kid, pem in data.items():
        label = f"{name}[{kid!r}]" if isinstance(kid, str) and kid.strip() else name
        if not isinstance(kid, str) or not kid.strip():
            problems.append(f"{name}: key ids must be non-empty strings")
        elif not isinstance(pem, str):
            problems.append(f"{label}: value must be a PEM string")
        elif "PRIVATE KEY" in pem.upper():
            problems.append(f"{label}: private key material is not allowed; supply public keys only")
        elif "-----BEGIN PUBLIC KEY-----" not in pem or "-----END PUBLIC KEY-----" not in pem:
            problems.append(f"{label}: must be a PEM 'PUBLIC KEY' block")
        else:
            keys[kid.strip()] = pem
    if len({k.strip() for k in data if isinstance(k, str)}) != len([k for k in data if isinstance(k, str)]):
        problems.append(f"{name}: duplicate key ids after trimming")
    return keys


def _check_database_url(url: str | None, app_env: str, problems: list[str]) -> None:
    if url is None:
        problems.append("DATABASE_URL: required")
        return
    scheme = _scheme(url)
    if scheme == "sqlite" and app_env != "test":
        problems.append("DATABASE_URL: sqlite is only allowed when APP_ENV=test")
    elif scheme not in ("postgresql", "sqlite"):
        problems.append("DATABASE_URL: must use the postgresql:// scheme")


def get_database_url(environ: Mapping[str, str] | None = None) -> SecretStr:
    """DATABASE_URL only, with the same validation as load_settings (for tooling such as
    Alembic that must not require GROQ_API_KEY or other app settings). Never cached."""
    env = os.environ if environ is None else environ
    app_env = ((env.get("APP_ENV") or "").strip() or "production").lower()
    if app_env not in ENVIRONMENTS:
        raise ConfigError([f"APP_ENV: must be one of {', '.join(ENVIRONMENTS)}"])
    url = (env.get("DATABASE_URL") or "").strip() or None
    problems: list[str] = []
    _check_database_url(url, app_env, problems)
    if problems:
        raise ConfigError(problems)
    return SecretStr(url)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    problems: list[str] = []
    warnings: list[str] = []

    def get(name: str) -> str | None:
        value = env.get(name)
        value = value.strip() if value is not None else ""
        return value or None

    def get_bool(name: str, default: bool) -> bool:
        raw = get(name)
        if raw is None:
            return default
        if raw.lower() in _TRUE:
            return True
        if raw.lower() in _FALSE:
            return False
        problems.append(f"{name}: must be one of true/false/1/0/yes/no/on/off")
        return default

    # APP_ENV: unset means production (fail-safe).
    app_env = (get("APP_ENV") or "production").lower()
    if app_env not in ENVIRONMENTS:
        problems.append(f"APP_ENV: must be one of {', '.join(ENVIRONMENTS)}")
        app_env = "production"
    production = app_env == "production"

    demo_mode = get_bool("AGENTFORGE_DEMO_MODE", False)
    if demo_mode and production:
        problems.append("AGENTFORGE_DEMO_MODE: not allowed in production (it replaces the model with a script)")
        demo_mode = False
    groq_api_key = get("GROQ_API_KEY")
    if groq_api_key is None and demo_mode:
        groq_api_key = "demo-mode-no-model-key"  # a placeholder, not a credential: the scripted model never contacts a provider
    if groq_api_key is None:
        problems.append("GROQ_API_KEY: required")

    database_url = get("DATABASE_URL")
    _check_database_url(database_url, app_env, problems)

    redis_url = get("REDIS_URL")
    if redis_url is None:
        redis_url = DEFAULT_REDIS_URL
        if production:
            warnings.append("REDIS_URL not set; using the default local Redis URL.")
    elif _scheme(redis_url) not in ("redis", "rediss", "unix"):
        problems.append("REDIS_URL: must use the redis://, rediss:// or unix:// scheme")

    # CORS: explicit list only; credentials are never enabled (Bearer-token design).
    raw_origins = get("CORS_ALLOWED_ORIGINS")
    origins: list[str] = []
    if raw_origins is None:
        origins = [] if production else list(DEFAULT_DEV_ORIGINS)
        if production:
            warnings.append("CORS_ALLOWED_ORIGINS not set; no cross-origin browser access is allowed.")
    else:
        entries = [e.strip() for e in raw_origins.split(",") if e.strip()]
        for index, entry in enumerate(entries, start=1):
            origin, reason = _normalize_origin(entry, production)
            if reason:
                problems.append(f"CORS_ALLOWED_ORIGINS entry #{index}: {reason}")
            elif origin not in origins:
                origins.append(origin)

    docs_enabled = get_bool("API_DOCS_ENABLED", default=not production)
    if production and docs_enabled:
        problems.append("API_DOCS_ENABLED: must not be enabled when APP_ENV=production")

    auth_required = get_bool("AUTH_CONFIG_REQUIRED", default=False)
    issuer = get("AUTH_JWT_ISSUER") or DEFAULT_JWT_ISSUER
    audience = get("AUTH_JWT_AUDIENCE") or DEFAULT_JWT_AUDIENCE
    raw_keys = get("AUTH_JWT_PUBLIC_KEYS")
    public_keys: dict[str, str] = {}
    if raw_keys is not None:
        public_keys = _parse_public_keys(raw_keys, problems)
    elif auth_required and production:
        problems.append("AUTH_JWT_PUBLIC_KEYS: required when AUTH_CONFIG_REQUIRED is enabled in production")

    run_execution = (get("RUN_EXECUTION") or "embedded").lower()
    if run_execution not in ("embedded", "api-only"):
        problems.append("RUN_EXECUTION: must be 'embedded' or 'api-only'")
        run_execution = "embedded"

    rate_limits = dict(RATE_LIMIT_DEFAULTS)
    for name in RATE_LIMIT_DEFAULTS:
        raw = get("RATE_LIMIT_" + name.upper())
        if raw is None:
            continue
        try:
            value = int(raw)
            if not 1 <= value <= RATE_LIMIT_MAX:
                raise ValueError
            rate_limits[name] = value
        except ValueError:
            problems.append(f"RATE_LIMIT_{name.upper()}: must be a whole number from 1 to {RATE_LIMIT_MAX}")
    tuning = {}
    for name, (kind, low, high) in PROVIDER_BOUNDS.items():
        raw = get(name)
        if raw is None:
            continue
        try:
            value = kind(raw)
            if not low <= value <= high:
                raise ValueError
            tuning[name] = raw
        except ValueError:
            problems.append(f"{name}: must be a {kind.__name__} from {low} to {high}")
    resend_key = get("RESEND_API_KEY")
    email_from = get("EMAIL_FROM")
    email_domains = ("resend.dev",)
    raw_domains = get("EMAIL_ALLOWED_DOMAINS")
    if raw_domains is not None:
        parsed = tuple(d.strip().lower() for d in raw_domains.split(",") if d.strip())
        if not parsed or not all(re.fullmatch(r"[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+", d) for d in parsed):
            problems.append("EMAIL_ALLOWED_DOMAINS: must be a comma-separated list of domain names")
        else:
            email_domains = parsed
    if resend_key is not None:
        if email_from is None or not re.fullmatch(r"[^<>\r\n]{0,80}<[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}>|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}", email_from):
            problems.append("EMAIL_FROM: required with RESEND_API_KEY, as 'Name <address@domain>' or 'address@domain'")
        if production and "resend.dev" in email_domains and len(email_domains) == 1:
            warnings.append("The email tool is in SANDBOX mode (recipients limited to resend.dev test addresses); set EMAIL_ALLOWED_DOMAINS to send to real recipients.")
    alert_url = get("ALERT_WEBHOOK_URL")
    if alert_url is not None:
        scheme, host = _scheme(alert_url), (urlparse(alert_url).hostname or "")
        if scheme != "https" and not (scheme == "http" and host in ("127.0.0.1", "localhost") and not production):
            problems.append("ALERT_WEBHOOK_URL: must be an https URL (http only to loopback outside production)")
    alert_interval = 30.0
    raw_interval = get("ALERT_EVAL_INTERVAL")
    if raw_interval is not None:
        try:
            alert_interval = float(raw_interval)
            if not 5.0 <= alert_interval <= 3600.0:
                raise ValueError
        except ValueError:
            problems.append("ALERT_EVAL_INTERVAL: must be a number from 5 to 3600 seconds")
            alert_interval = 30.0
    sentinels, sentinel_master = (), get("REDIS_SENTINEL_MASTER")
    raw_sentinels = get("REDIS_SENTINELS")
    if raw_sentinels is not None:
        parsed_s = []
        for part in raw_sentinels.split(","):
            host, _, port = part.strip().rpartition(":")
            if not host or not port.isdigit() or not 1 <= int(port) <= 65535 or not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}", host):
                parsed_s = []
                break
            parsed_s.append((host, int(port)))
        if not parsed_s:
            problems.append("REDIS_SENTINELS: must be a comma-separated list of host:port")
        else:
            sentinels = tuple(parsed_s)
            if not sentinel_master:
                problems.append("REDIS_SENTINEL_MASTER: required with REDIS_SENTINELS")
    elif sentinel_master:
        problems.append("REDIS_SENTINEL_MASTER: set without REDIS_SENTINELS")
    max_request_bytes = 1_048_576
    raw_max = get("MAX_REQUEST_BYTES")
    if raw_max is not None:
        try:
            max_request_bytes = int(raw_max)
            if not 1024 <= max_request_bytes <= 33_554_432:
                raise ValueError
        except ValueError:
            problems.append("MAX_REQUEST_BYTES: must be a whole number from 1024 to 33554432")
            max_request_bytes = 1_048_576
    allowed_hosts = ()
    raw_hosts = get("ALLOWED_HOSTS")
    if raw_hosts is not None:
        parsed_h = tuple(h.strip().lower() for h in raw_hosts.split(",") if h.strip())
        if not parsed_h or not all(re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)*(:[0-9]{1,5})?", h) for h in parsed_h):
            problems.append("ALLOWED_HOSTS: must be a comma-separated list of host names (optionally *.domain or host:port)")
        else:
            allowed_hosts = parsed_h
    elif production:
        warnings.append("ALLOWED_HOSTS is not set: the Host header is not validated (set it to this API's public host names).")
    provider_redis_down = (get("PROVIDER_REDIS_DOWN") or "closed").lower()
    if provider_redis_down not in ("degrade", "closed"):
        problems.append("PROVIDER_REDIS_DOWN: must be 'degrade' or 'closed'")
        provider_redis_down = "closed"
    if provider_redis_down == "degrade":
        warnings.append("PROVIDER_REDIS_DOWN=degrade is an EXPLICIT opt-in: while Redis is unreachable (or has failed over) the global provider concurrency cap is NOT enforced; only per-process limits apply.")
    log_format = (get("LOG_FORMAT") or "text").lower()
    if log_format not in ("text", "json"):
        problems.append("LOG_FORMAT: must be 'text' or 'json'")
        log_format = "text"

    if problems:
        raise ConfigError(problems)

    return Settings(
        app_env=app_env,
        groq_api_key=SecretStr(groq_api_key),
        model_name=get("MODEL_NAME") or DEFAULT_MODEL_NAME,
        database_url=SecretStr(database_url),
        redis_url=SecretStr(redis_url),
        telegram_bot_token=SecretStr(get("TELEGRAM_BOT_TOKEN")) if get("TELEGRAM_BOT_TOKEN") else None,
        twilio_account_sid=SecretStr(get("TWILIO_ACCOUNT_SID") or ""),
        twilio_auth_token=SecretStr(get("TWILIO_AUTH_TOKEN") or ""),
        allowed_origins=tuple(origins),
        api_docs_enabled=docs_enabled,
        auth_config_required=auth_required,
        auth_jwt_issuer=issuer,
        auth_jwt_audience=audience,
        auth_jwt_public_keys=public_keys,
        run_execution=run_execution,
        demo_mode=demo_mode,
        metrics_token=SecretStr(get("METRICS_TOKEN")) if get("METRICS_TOKEN") else None,
        log_format=log_format,
        rate_limits=rate_limits,
        provider_redis_down=provider_redis_down,
        max_request_bytes=max_request_bytes,
        allowed_hosts=allowed_hosts,
        redis_sentinels=sentinels,
        redis_sentinel_master=sentinel_master,
        alert_webhook_url=SecretStr(alert_url) if alert_url else None,
        alert_eval_interval=alert_interval,
        backup_status_file=get("BACKUP_STATUS_FILE"),
        resend_api_key=SecretStr(resend_key) if resend_key else None,
        email_from=email_from,
        email_allowed_domains=email_domains,
        provider_tuning=tuning,
        warnings=tuple(warnings),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Honors PYTHON_DOTENV_DISABLED; never overrides real env vars."""
    load_dotenv(override=False)
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
