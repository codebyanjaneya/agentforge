"""The one-shot live-model harness (ops/live_model_check.py): its safety properties and its free ``--dry-run``. No provider call, no key, no network is involved here."""

import json
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND.parent / "ops" / "live_model_check.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=BACKEND, capture_output=True, text=True, timeout=180)


def test_it_refuses_to_spend_without_the_explicit_flag():
    out = run()
    assert out.returncode == 2 and "refusing" in out.stdout and "--yes-spend-one-bounded-run" in out.stdout


def test_the_dry_run_proves_the_harness_limits_ledger_and_evidence_without_any_provider():
    out = run("--dry-run")
    assert out.returncode == 0, out.stderr[-800:]
    evidence = json.loads(out.stdout)
    assert all(evidence["checks"].values()) and evidence["provider_requests_made"] == 2 and evidence["side_effecting_dispatches"] == 0
    assert evidence["limits"]["max_provider_requests"] == 3 and evidence["limits"]["attempts_per_request"] == 1 and evidence["limits"]["parallel_calls"] == 1
    assert [i["tool"] for i in evidence["tool_invocations"]] == ["math.calculate"] and "391" in evidence["task_result"]
    assert "GROQ_API_KEY" not in out.stdout and "gsk_" not in out.stdout + out.stderr
