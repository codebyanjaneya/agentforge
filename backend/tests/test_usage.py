"""M6 / Priority 4: model usage and cost accounting (pricing arithmetic, recorder, per-run API)."""

import copy
import importlib
from datetime import datetime
from decimal import Decimal

import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, AGENT, B, make_plan, papi, start, task, wait  # noqa: F401  (papi is a fixture)


def writers(api, *slugs, model="openai/gpt-oss-120b"):
    """Custom agents (only they report usage: the built-in orchestrator agents return text alone) and the chain of tasks that uses them."""
    for slug in slugs:
        definition = copy.deepcopy(AGENT)
        definition["model"]["name"] = model
        assert api.client.post("/agents", json={"slug": slug, "definition": definition}, headers=api.auth(*A)).status_code == 201
    return [task(s, s, [slugs[i - 1]] if i else []) for i, s in enumerate(slugs)]



def pricing():
    return importlib.import_module("agentplatform.pricing")


# ------------------------------------------------------------------------------------------------ arithmetic

def test_cost_is_exact_decimal_rounded_half_up_once_per_request():
    p = pricing()
    # current version (2026-11-21, re-checked against the provider's model page): $0.15 in / $0.60 out per 1M. 1M + 1M = $0.75 = 750000 micro-dollars
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 1_000_000, 1_000_000) == (750_000, "calculated")
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 0, 0) == (0, "calculated")
    # 3 input tokens * 0.15 = 0.45 micro -> 0 ; 4 * 0.15 = 0.6 -> 1 ; 10 in + 10 out = 1.5 + 6.0 = 7.5 -> 8 (a half is rounded UP, never to even)
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 3, 0)[0] == 0
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 4, 0)[0] == 1
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 10, 10)[0] == 8
    # 20b: $0.075 in / $0.30 out; 5 output tokens = 1.5 micro -> 2; 1 output token = 0.3 -> 0
    assert p.cost_micros("groq", "openai/gpt-oss-20b", 0, 5)[0] == 2
    assert p.cost_micros("groq", "openai/gpt-oss-20b", 0, 1)[0] == 0
    # the previous version keeps its previous price ($0.75 out): rows calculated with it are never recalculated
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 10, 10, "2026-11-19")[0] == 9
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 1_000_000, 1_000_000, "2026-11-19") == (900_000, "calculated")


def test_missing_usage_and_missing_price_are_never_turned_into_a_cost():
    p = pricing()
    for bad in ((None, 5), (5, None), (None, None), (-1, 5), (True, 5), (1.5, 5), ("5", 5)):
        assert p.cost_micros("groq", "openai/gpt-oss-120b", *bad) == (None, "unknown_usage"), bad
    assert p.cost_micros("groq", "no-such-model", 5, 5) == (None, "unknown_pricing")
    assert p.cost_micros("other", "openai/gpt-oss-120b", 5, 5) == (None, "unknown_pricing")


def test_prices_are_versioned_and_an_old_version_is_never_recalculated(monkeypatch):
    p = pricing()
    monkeypatch.setitem(p.PRICES, "2099-01-01", {("groq", "openai/gpt-oss-120b"): (Decimal("1"), Decimal("1"))})
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 1000, 0, "2099-01-01")[0] == 1000
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 1000, 0)[0] == 150  # the current version is unchanged
    assert p.cost_micros("groq", "openai/gpt-oss-120b", 1000, 0, "1999-01-01") == (None, "unknown_pricing")  # an unknown version prices nothing
    assert p.format_usd(0) == "$0" and p.format_usd(900_000) == "$0.90" and p.format_usd(150) == "$0.00015" and p.format_usd(2_500_000) == "$2.50"


# ------------------------------------------------------------------------------------------------ recorder + per-run API

def test_a_run_records_every_request_and_the_api_reports_only_persisted_numbers(papi):
    providers = importlib.import_module("agentplatform.providers")
    papi.provider.usage = providers.Usage(prompt_tokens=1000, completion_tokens=200, cached_tokens=50)
    pid = make_plan(papi, writers(papi, "alpha", "bravo", "charlie"))
    run = start(papi, pid).json()
    assert wait(papi, run["id"])["status"] == "succeeded"
    rows = papi.db.rows("SELECT purpose, run_id IS NOT NULL, task_key, input_tokens, output_tokens, cached_tokens, usage_known, cost_basis FROM model_usage ORDER BY created_at")
    assert [r[0] for r in rows].count("plan") == 1 and [r[0] for r in rows].count("agent") == 3
    body = papi.client.get(f"/plan-runs/{run['id']}/usage", headers=papi.auth(*A))
    assert body.status_code == 200, body.text
    u = body.json()
    assert u["totals"]["requests"] == 4 and u["totals"]["input_tokens"] == 4000 and u["totals"]["output_tokens"] == 800 and u["totals"]["cached_tokens"] == 200
    assert u["planning"]["requests"] == 1 and u["agents"]["requests"] == 3
    assert [t["task"] for t in u["by_task"]] == ["alpha", "bravo", "charlie"] and all(t["requests"] == 1 for t in u["by_task"])
    assert u["pricing"]["current_version"] == pricing().PRICING_VERSION
    # the 3 agent requests use a priced model; the plan request used the configured planner model (the fake provider echoes whatever was asked)
    assert u["agents"]["cost_micros"] == 3 * pricing().cost_micros("groq", "openai/gpt-oss-120b", 1000, 200)[0] and u["agents"]["cost_complete"] is True
    assert u["totals"]["cost_complete"] == (u["planning"]["cost_micros"] is not None) and u["pricing_versions"] == [pricing().PRICING_VERSION]
    assert [m["model"] for m in u["by_model"]] == sorted({r[0] for r in papi.db.rows("SELECT model FROM model_usage")})


def test_a_provider_that_reports_no_usage_yields_unknown_usage_not_zero(papi):
    pid = make_plan(papi, writers(papi, "alpha"))
    run = start(papi, pid).json()
    assert wait(papi, run["id"])["status"] == "succeeded"
    u = papi.client.get(f"/plan-runs/{run['id']}/usage", headers=papi.auth(*A)).json()
    assert u["totals"]["requests"] >= 1 and u["totals"]["requests_without_usage"] == u["totals"]["requests"]
    assert u["totals"]["cost_micros"] is None and u["totals"]["cost_complete"] is False and u["totals"]["input_tokens"] == 0
    assert set(r[0] for r in papi.db.rows("SELECT cost_basis FROM model_usage")) == {"unknown_usage"}


def test_a_priced_model_gets_a_calculated_cost_with_its_pricing_version(papi):
    providers = importlib.import_module("agentplatform.providers")
    papi.provider.usage = providers.Usage(prompt_tokens=2000, completion_tokens=1000)
    pid = make_plan(papi, writers(papi, "alpha"))
    run = start(papi, pid).json()
    wait(papi, run["id"])
    # reprice nothing: re-read what the recorder stored for this model
    rows = papi.db.rows("SELECT provider, model, cost_basis, cost_micros, pricing_version FROM model_usage")
    for provider, model, basis, micros, version in rows:
        expected = pricing().cost_micros(provider, model, 2000, 1000)
        assert (micros, basis) == expected and (version == pricing().PRICING_VERSION) == (basis == "calculated")


def test_usage_is_scoped_to_the_run_owner_and_unknown_runs_are_404(papi):
    pid = make_plan(papi, writers(papi, "alpha"))
    run = start(papi, pid).json()
    wait(papi, run["id"])
    papi.provision(*B)
    assert papi.client.get(f"/plan-runs/{run['id']}/usage", headers=papi.auth(*B)).status_code == 404
    assert papi.client.get("/plan-runs/00000000-0000-0000-0000-000000000000/usage", headers=papi.auth(*A)).status_code == 404
    assert papi.client.get(f"/plan-runs/{run['id']}/usage").status_code == 401


def test_recording_never_fails_the_work(papi, caplog):
    usage = importlib.import_module("agentplatform.usage")
    rec = usage.UsageRecorder(lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    rec.record(object(), purpose="agent", provider="groq", model="m", usage=None)  # a bad scope AND a bad session factory: swallowed
    assert all("db down" not in r.getMessage() for r in caplog.records)  # only the exception type is logged


def test_provider_events_are_scoped_ordered_and_summarised_by_current_task_state(papi):
    pid = make_plan(papi, writers(papi, "alpha", "bravo"))
    run = start(papi, pid).json()
    wait(papi, run["id"])
    uid = papi.db.scalar("SELECT user_id FROM plan_runs")
    wid = papi.db.scalar("SELECT workspace_id FROM plan_runs")
    rid = papi.db.scalar("SELECT id FROM plan_runs")
    rows = [("bravo", "waiting", 1, "rate_limited", 1500, "2026-11-19 10:00:00.000001"), ("bravo", "retrying", 2, "rate_limited", None, "2026-11-19 10:00:02.000001"),
            ("alpha", "exhausted", 4, "rate_limited", None, "2026-11-19 10:00:03.000001"), ("bravo", "recovered", 2, None, None, "2026-11-19 10:00:04.000001")]
    for i, (task, kind, attempt, code, wait_ms, at) in enumerate(rows):
        papi.db.run("INSERT INTO provider_events (id, user_id, workspace_id, run_id, task_key, provider, kind, attempt, error_code, wait_ms, created_at) VALUES "
                    f"('{'0' * 31}{i}','{uid}','{wid}','{rid}','{task}','groq','{kind}',{attempt},{'NULL' if code is None else repr(code)},{'NULL' if wait_ms is None else wait_ms},'{at}')")
    body = papi.client.get(f"/plan-runs/{run['id']}/provider-events", headers=papi.auth(*A)).json()
    assert [e["kind"] for e in body["items"]] == ["waiting", "retrying", "exhausted", "recovered"]
    assert body["by_task"] == {"bravo": {"state": "recovered", "attempt": 2, "error_code": None, "wait_ms": None}, "alpha": {"state": "exhausted", "attempt": 4, "error_code": "rate_limited", "wait_ms": None}}
    papi.provision(*B)
    assert papi.client.get(f"/plan-runs/{run['id']}/provider-events", headers=papi.auth(*B)).status_code == 404
    assert papi.client.get(f"/plan-runs/{run['id']}/provider-events").status_code == 401
    with __import__("pytest").raises(Exception):
        papi.db.run("DELETE FROM provider_events")  # append-only
