"""Deployment gate (sprint 4): validates the environment a STAGING or PRODUCTION AgentForge process would start with, BEFORE it starts. Run from backend/ with the venv interpreter and the deployment's real
environment variables (it prints no secret):

    python ../ops/check_config.py [--profile staging|production]

It first runs the application's own validation (``config.load_settings``: every variable is range/shape checked and a bad value refuses to start), then applies the stricter release rules below. Exit code 0 = ok
(warnings may be printed), 1 = at least one blocking finding. It does not contact any server."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from config import ConfigError, load_settings  # noqa: E402


def findings(settings, profile: str) -> list:
    """(severity, message) pairs for a loaded settings object. ``block`` fails the gate."""
    out = []
    if settings.app_env != "production":
        out.append(("block", f"APP_ENV is '{settings.app_env}': a {profile} deployment must run with APP_ENV=production"))
    if settings.demo_mode:
        out.append(("block", "AGENTFORGE_DEMO_MODE is on (the model is a script and the send tool a simulation)"))
    if not settings.auth_config_required or not settings.auth_jwt_public_keys:
        out.append(("block", "AUTH_CONFIG_REQUIRED must be on with AUTH_JWT_PUBLIC_KEYS set (the API would otherwise not verify tokens)"))
    if not settings.allowed_origins or any(not o.startswith("https://") for o in settings.allowed_origins):
        out.append(("block", "CORS_ALLOWED_ORIGINS must be explicit https origins"))
    if not settings.allowed_hosts:
        out.append(("block", "ALLOWED_HOSTS is not set (the Host header is not validated)"))
    if settings.api_docs_enabled:
        out.append(("warn", "API_DOCS_ENABLED exposes /docs: keep it internal"))
    if settings.metrics_token is None:
        out.append(("warn", "METRICS_TOKEN is not set: no /metrics (no scraping, no metric-based alerts)"))
    elif len(settings.metrics_token.get_secret_value()) < 24:
        out.append(("block", "METRICS_TOKEN is shorter than 24 characters"))
    if settings.alert_webhook_url is None:
        out.append(("warn", "ALERT_WEBHOOK_URL is not set: in-process alerting is OFF (rely on Prometheus/Alertmanager using ops/alerts/agentforge-alerts.yml, which must then be deployed)"))
    if settings.backup_status_file is None:
        out.append(("warn", "BACKUP_STATUS_FILE is not set: the backup alerts cannot be evaluated by this process"))
    if not settings.database_url.get_secret_value().startswith(("postgresql://", "postgresql+psycopg2://")):
        out.append(("block", "DATABASE_URL must be PostgreSQL in staging/production"))
    if "sslmode" not in settings.database_url.get_secret_value() and "localhost" not in settings.database_url.get_secret_value() and "127.0.0.1" not in settings.database_url.get_secret_value():
        out.append(("warn", "DATABASE_URL has no sslmode: require TLS to the database"))
    if settings.redis_url.get_secret_value().startswith("redis://localhost") and not settings.redis_sentinels:
        out.append(("warn", "REDIS_URL is the default localhost value"))
    if settings.resend_api_key is not None and settings.email_allowed_domains == ("resend.dev",):
        out.append(("warn", "the email tool is in SANDBOX mode (resend.dev test recipients only)"))
    if settings.resend_api_key is not None and "resend.dev" in settings.email_allowed_domains and len(settings.email_allowed_domains) > 1:
        out.append(("warn", "EMAIL_ALLOWED_DOMAINS mixes the sandbox domain with real ones"))
    if settings.provider_redis_down == "degrade":
        out.append(("warn", "PROVIDER_REDIS_DOWN=degrade is an explicit opt-in: while Redis is down or has failed over the provider concurrency cap is NOT enforced across processes (remove it for the safe default, closed)"))
    if settings.run_execution == "embedded":
        out.append(("warn", "RUN_EXECUTION=embedded: API processes execute runs; use 'api-only' plus worker processes for horizontal scaling"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("staging", "production"), default="production")
    args = ap.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("BLOCK the application's own validation refuses this configuration:")
        for problem in exc.problems:
            print("  -", problem)
        return 1
    blocking = False
    for severity, message in findings(settings, args.profile):
        print(("BLOCK " if severity == "block" else "WARN  ") + message)
        blocking = blocking or severity == "block"
    for warning in settings.warnings:
        print("WARN  " + warning)
    print("CONFIG GATE " + ("FAILED" if blocking else "PASSED") + f" (profile={args.profile})")
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
