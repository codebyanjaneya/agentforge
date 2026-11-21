"""Sprint 3: the alert rules are real. Every rule is well formed and every metric it queries is either emitted by the application (the HELP table, the scrape-time gauges) or explicitly listed as published
by an EXTERNAL job (the backup/restore jobs, Prometheus's own `up`). Rules are not live: no monitoring backend is configured here; this test only keeps them from rotting against the code."""

import re
from pathlib import Path

import yaml

import observability

RULES = Path(__file__).resolve().parents[2] / "ops" / "alerts" / "agentforge-alerts.yml"
GAUGES = {"runs_running", "dispatch_unresolved", "approvals_pending", "workers_registered", "db_pool_checked_out", "db_pool_size", "provider_global_cap_enforced", "rate_limit_shared_enforced"}
EXTERNAL = {"up", "agentforge_backup_last_success_timestamp_seconds", "agentforge_restore_verification_ok"}
FORBIDDEN_LABELS = ("user", "email", "token", "run_id", "workspace", "jti")


def known_metrics():
    names = set(observability.HELP) | {"agentforge_" + g for g in GAUGES}
    for n in list(names):
        names |= {n + "_bucket", n + "_sum", n + "_count"}
    return names | EXTERNAL


def rules():
    doc = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_every_rule_is_well_formed():
    found = rules()
    assert len(found) >= 15
    names = [r["alert"] for r in found]
    assert len(names) == len(set(names))
    for r in found:
        assert r["alert"].startswith("AgentForge") and r["expr"].strip() and "for" in r
        assert r["labels"]["severity"] in ("page", "ticket")
        assert r["annotations"]["summary"] and r["annotations"]["runbook"].startswith("docs/operations.md#")


def test_every_queried_metric_exists_in_the_code_or_is_declared_external():
    known, missing = known_metrics(), []
    for r in rules():
        for metric in re.findall(r"\b(agentforge_[a-z0-9_]+|up)\b(?=\s*[\{\[\)\s,/<>=!]|$)", r["expr"]):
            if metric not in known:
                missing.append((r["alert"], metric))
    assert missing == []


def test_the_required_coverage_is_present_and_no_label_can_carry_an_identity():
    text = RULES.read_text(encoding="utf-8")
    for needed in ("UnknownSideEffect", "ProviderRateLimitExhausted", "ApprovalBacklog", "RedisUnavailable", "DatabasePool", "ServerErrorRate", "HighLatency", "BackupStale", "NoWorkers", "NotReady"):
        assert needed in text, needed
    for r in rules():
        for label in re.findall(r"by\s*\(([^)]*)\)", r["expr"]):
            assert not any(f in label for f in FORBIDDEN_LABELS)
        assert not re.search(r"\{[^}]*\b(%s)\b\s*[=!~]" % "|".join(FORBIDDEN_LABELS), r["expr"])


def test_the_emitted_vocabulary_has_no_identity_labels():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    for name, text in observability.HELP.items():
        assert not any(f in name for f in ("user", "email", "jti"))  # ('tokens' here are model tokens, a counter name, not a credential)
