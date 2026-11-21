"""M4-B: the versioned tool registry, the five first tools, durable invocation, recovery and the agent tool loop.

Service level on a migrated disposable SQLite database; a scripted provider stands in for a model (no network, no credential). A live provider's
function calling is NOT exercised here: the Groq adapter is covered with a fake SDK client in test_providers-style tests below."""

import asyncio
import json
import threading
import time
import uuid

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import DatabaseError

from agentplatform.execution import MAX_TOOL_CALLS, MAX_TOOL_ROUNDS, ExecutionService, ExecutionStore, Limits
from agentplatform.plan_runs import PlanRunService, PlanRunStore, RunRegistry
from agentplatform.providers import GroqProvider, Message, ProviderRegistry, ProviderRequest, ProviderResponse, ToolCall, ToolDefinition
from agentplatform.run_leases import Fence, LeaseLost
from agentplatform.schema import AgentDefinition
from agentplatform.service import ResolvedAgent
from agentplatform.tools import ToolError, ToolInputError, ToolPolicy, ToolRegistry, ToolSpec, default_registry
from agentplatform.tools.builtin import (
    CalculateIn, DateMathIn, SummarizeCsvIn, TextAnalyzeIn, UnitEconomicsIn, analyze_text, calculate, date_math, summarize_csv, unit_economics)
from agentplatform.tools.invoker import InvocationContext, InvocationNotFound, ToolInvoker, ToolRunContext, ToolStore, close_open
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.api_support import FakeRedis
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import CHAIN, definition, go, make_plan, statuses, task, world  # noqa: F401  (world is a fixture)
from tests.test_run_recovery import FAST, expire
from tests.test_authz_memory import team_workspace
from authz.workspaces import authorize_memory_scope

BUILTIN = ResolvedAgent("builtin", "research", None, None, None)


class In(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int


def spec(**kw):
    base = dict(id="test.echo", version=1, title="Echo", summary="Echoes n.", description="Echoes the number.", category="Test", input_model=In, output_model=Out)
    base.update(kw)
    return ToolSpec(**base)


class _TestRefs:
    """An allowlist for TEST tools only (``test.*``); the real policy is an explicit list of exact ``id@version`` refs (agentplatform/tools/policy.py)."""

    def __contains__(self, ref):
        return isinstance(ref, str) and ref.startswith("test.")

    def __iter__(self):
        return iter(())


def open_registry():
    return ToolRegistry(ToolPolicy(auto_run=_TestRefs()))


class Scripted:
    """A model stand-in: returns the queued responses in order and records every request it receives."""

    name = "groq"

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    async def complete(self, request):
        self.requests.append(request)
        if not self.responses:
            return ProviderResponse(text="done", model="m")
        return self.responses.pop(0)


def call(name, args, id="c1"):
    return ToolCall(id, name, args if isinstance(args, str) else json.dumps(args))


def asks(*calls, text=""):
    return ProviderResponse(text=text, model="m", tool_calls=tuple(calls))


def final(text="the answer"):
    return ProviderResponse(text=text, model="m")


def service(world, provider, registry=None):
    invoker = ToolInvoker(ToolStore(world["sf"]), registry or default_registry())
    return ExecutionService(world["sf"], orchestrator=world["orch"], providers=ProviderRegistry({"groq": lambda: provider}), short_term=ShortTermMemory(client=FakeRedis()),
                            long_term=MemoryRepository(world["sf"]), limits=Limits(total=8, per_user=3), tools=invoker), invoker


def make_agent(world, tools=("math.calculate",), slug="analyst"):
    world["agents"].create(world["sa"], slug, definition(allowed_tools=list(tools)))
    return slug


def rows(world, sql="SELECT tool_id, tool_version, seq, status, error_code FROM tool_invocations ORDER BY seq"):
    return world["db"].rows(sql)


def context(world, tools, run=None):
    eid = ExecutionStore(world["sf"]).start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    return InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={t.wire_name: t for t in tools}, run=run)


def invoke(world, invoker, ctx, name, args, seq=1):
    return asyncio.run(invoker.invoke(ctx, name, args, seq))


# ------------------------------------------------------------------------------------------------ the registry

def test_versions_are_immutable_facts_and_resolution_prefers_the_newest_active_version():
    registry = ToolRegistry()
    one, two, three = (registry.register(spec(version=v, lifecycle=life), lambda a: Out(n=a.n)) for v, life in ((1, "active"), (2, "active"), (3, "disabled")))
    assert registry.get("test.echo") is two  # newest ACTIVE
    assert registry.get("test.echo", 1) is one and registry.get("test.echo", 3) is three and registry.get("test.echo", 9) is None and registry.get("nope.nope") is None
    with pytest.raises(ValueError):
        registry.register(spec(version=2, title="Changed"), lambda a: Out(n=a.n))  # a changed tool is a NEW version, never an edit
    only_old = ToolRegistry()
    only_old.register(spec(lifecycle="deprecated"), lambda a: Out(n=1))
    only_old.register(spec(version=2, lifecycle="disabled"), lambda a: Out(n=1))
    assert only_old.get("test.echo").version == 1  # a deprecated version still resolves; a disabled one never does by default
    assert [s.ref for s in registry.list_specs()] == ["test.echo@3", "test.echo@2", "test.echo@1"] and [s.ref for s in registry.list_specs(include_disabled=False)] == ["test.echo@2", "test.echo@1"]
    assert one.wire_name == "test__echo__v1" and one.ref == "test.echo@1"


@pytest.mark.parametrize("bad", [dict(id="Math.calculate"), dict(id="calculate"), dict(id="a.b"), dict(id="x.y z"), dict(version=0), dict(version=True), dict(risk="root"), dict(side_effect="maybe"),
                                 dict(lifecycle="beta"), dict(timeout_seconds=0), dict(timeout_seconds=99), dict(max_output_bytes=10**7)])
def test_a_malformed_spec_is_refused(bad):
    with pytest.raises(ValueError):
        spec(**bad)


def test_tools_must_be_trusted_code_with_strict_models():
    class Loose(BaseModel):
        n: int  # extra fields would be accepted

    with pytest.raises(ValueError):
        spec(input_model=Loose)
    with pytest.raises(ValueError):
        spec(input_model=dict)
    with pytest.raises(ValueError):
        ToolRegistry().register(spec(), "print('not a function')")  # model- or user-supplied "code" can never be a handler


def test_the_policy_is_an_allowlist_of_exact_versions_and_metadata_alone_never_grants_permission():
    policy = ToolPolicy(auto_run=frozenset({"test.echo@1"}))
    registry = ToolRegistry(policy)
    registry.register(spec(), lambda a: Out(n=a.n))  # listed, read-only, no side effect
    registry.register(spec(version=2), lambda a: Out(n=a.n))  # a NEW version of a listed tool: not listed
    registry.register(spec(id="test.claims_safe"), lambda a: Out(n=a.n))  # claims read/none but nobody listed it: a deliberately MISLABELLED tool
    registry.register(spec(id="test.mutate", risk="write", side_effect="non_idempotent"), lambda a: Out(n=a.n))
    registry.register(spec(id="test.off", lifecycle="disabled"), lambda a: Out(n=a.n))
    assert [policy.decide(registry.get("test.echo", v)) for v in (1, 2)] == ["allow", "requires_approval"]
    assert policy.decide(registry.get("test.claims_safe")) == "requires_approval" and policy.decide(registry.get("test.mutate")) == "requires_approval"
    assert registry.executable(registry.get("test.echo", 1)) and not registry.executable(registry.get("test.echo", 2)) and not registry.executable(registry.get("test.claims_safe"))
    assert not registry.executable(registry.get("test.mutate")) and not registry.executable(registry.get("test.off", 1))
    assert registry.gated(registry.get("test.claims_safe")) and registry.gated(registry.get("test.echo", 2)) and not registry.gated(registry.get("test.off", 1))
    listed_but_sensitive = ToolRegistry(ToolPolicy(auto_run=frozenset({"test.mutate@1"})))  # listing a tool whose declared class is NOT read-only/none still does not allow it
    listed_but_sensitive.register(spec(id="test.mutate", risk="write", side_effect="non_idempotent"), lambda a: Out(n=a.n))
    assert not listed_but_sensitive.executable(listed_but_sensitive.get("test.mutate"))
    assert ToolPolicy().decide(registry.get("test.echo", 1)) == "requires_approval"  # the default policy trusts nothing
    assert registry.pin("test.echo@1") == "test.echo@1"
    for refused in ("test.mutate", "test.off@1", "test.echo@3", "test.echo@x", "nope.tool", "", "a" * 60, 7, "test.claims_safe"):
        with pytest.raises(ValueError):
            registry.pin(refused)  # not allowlisted tools cannot be granted by this registry (it does not allow gated grants)
    assert policy.fingerprint == ToolPolicy(auto_run=frozenset({"test.echo@1"})).fingerprint != ToolPolicy(auto_run=frozenset({"test.echo@1", "test.echo@2"})).fingerprint


def test_the_default_registry_has_the_six_real_tools_all_executable(monkeypatch):
    from agentplatform.tools import holidays

    # the sixth is the one REAL network integration (M6): its example is run against a local stand-in here, never the internet (see test_holidays_tool.py)
    monkeypatch.setattr(holidays, "fetch", lambda url, **kw: b'[{"date": "2026-12-25", "localName": "Christmas Day", "name": "Christmas Day", "global": true}]')
    registry = default_registry()
    assert [s.ref for s in registry.list_specs()] == ["math.calculate@1", "data.summarize_csv@1", "finance.unit_economics@1", "calendar.public_holidays@1", "time.date_math@1", "text.analyze@1"]
    for s in registry.list_specs():
        assert registry.executable(s) and s.risk == "read" and s.side_effect == "none" and s.limitations and s.example and s.input_schema()["properties"]
        s.input_model.model_validate(s.example)  # the documented example is a valid input
        registry.handler(s)(s.input_model.model_validate(s.example))  # and it really runs
    assert default_registry() is default_registry()


# ------------------------------------------------------------------------------------------------ the tools themselves

def test_calculate_is_exact_and_refuses_everything_that_is_not_arithmetic():
    assert calculate(CalculateIn(expression="(1200 * 0.85 - 300) / 12")).value == 60.0
    assert calculate(CalculateIn(expression="2^10 + sqrt(144) - max(1, 2, 3)")).value == 1033.0
    assert calculate(CalculateIn(expression="10 % 4 + 7 // 2 + round(2.567, 1)")).formatted == "7.6"
    for hostile in ("__import__('os').system('echo x')", "().__class__", "open('f')", "x + 1", "[1, 2]", "1; 2", "lambda: 1", "abs(1, 2, 3, 4)", "True + 1", "'a' * 3", "2 ** 100000",
                    "1 / 0", "sqrt(-1)", "9" * 40, "-" * 150 + "1", "1+" * 50 + "1", "pi.real", "e ** 99", "1e308 * 10", "print(1)", "a.b", "1 if 1 else 2", "-"):
        with pytest.raises(ToolInputError):
            calculate(CalculateIn(expression=hostile))


def test_unit_economics_matches_the_published_formulas():
    out = unit_economics(UnitEconomicsIn(monthly_price=49, gross_margin_pct=80, monthly_churn_pct=3, cac=400))
    assert (out.gross_profit_per_month, out.expected_lifetime_months, out.ltv, out.ltv_to_cac, out.cac_payback_months, out.assessment) == (39.2, 33.33, 1306.67, 3.27, 10.2, "healthy")
    assert unit_economics(UnitEconomicsIn(monthly_price=10, gross_margin_pct=50, monthly_churn_pct=10, cac=100)).assessment == "unprofitable"
    assert unit_economics(UnitEconomicsIn(monthly_price=100, gross_margin_pct=100, monthly_churn_pct=10, cac=500)).assessment == "marginal"  # LTV/CAC 2, payback 5
    free = unit_economics(UnitEconomicsIn(monthly_price=10, gross_margin_pct=50, monthly_churn_pct=5, cac=0))
    assert (free.assessment, free.ltv_to_cac, free.cac_payback_months) == ("no_cac_given", None, None)
    for bad in (dict(monthly_price=0), dict(monthly_price=-1), dict(gross_margin_pct=101), dict(monthly_churn_pct=0), dict(cac=-5)):
        with pytest.raises(Exception):
            UnitEconomicsIn(**{"monthly_price": 10, "gross_margin_pct": 50, "monthly_churn_pct": 5, "cac": 1, **bad})


def test_text_analysis_counts_and_scores_a_real_text():
    text = "Our new release makes onboarding faster. Teams can invite colleagues in seconds. Faster onboarding means more teams stay.\n\nQuestions? Ask support!"
    out = analyze_text(TextAnalyzeIn(text=text, top_keywords=3))
    assert (out.words, out.sentences, out.paragraphs) == (21, 5, 2) and out.longest_sentence_words == 6 and out.reading_time_minutes == 0.1
    assert out.top_keywords[0].word in ("onboarding", "faster", "teams") and out.top_keywords[0].count == 2 and len(out.top_keywords) == 3
    assert out.flesch_reading_ease is not None and out.readability in ("very_easy", "easy", "fairly_easy", "standard")
    assert analyze_text(TextAnalyzeIn(text="Hi there.")).readability == "not_enough_text"


def test_csv_summary_detects_types_and_is_bounded():
    out = summarize_csv(SummarizeCsvIn(csv_text="plan,price,seats\nstarter,19,3\nteam,49,10\nteam,49,\nscale,199,50"))
    assert out.rows == 4 and [c.kind for c in out.columns] == ["text", "number", "number"]
    price = out.columns[1]
    assert (price.minimum, price.maximum, price.mean, price.total, price.non_empty) == (19.0, 199.0, 79.0, 316.0, 4) and out.columns[2].non_empty == 3
    assert out.columns[0].top_values[0] == "team" and out.columns[0].distinct == 3
    assert summarize_csv(SummarizeCsvIn(csv_text="a;b\n1;2\n3;4", delimiter=";")).columns[0].total == 4.0
    assert summarize_csv(SummarizeCsvIn(csv_text="1,2\n3,4", has_header=False)).columns[0].name == "column_1"
    big = "x\n" + "\n".join(str(i) for i in range(6000))
    assert summarize_csv(SummarizeCsvIn(csv_text=big)).truncated is True
    for bad in ("", "   \n  ", ",".join(f"c{i}" for i in range(40)) + "\n" + ",".join("1" for _ in range(40))):
        with pytest.raises(ToolInputError):
            summarize_csv(SummarizeCsvIn(csv_text=bad or " "))


def test_date_math_is_exact():
    assert date_math(DateMathIn(operation="add_days", start="2026-12-28", days=7)).result_date == "2027-01-04"
    out = date_math(DateMathIn(operation="add_business_days", start="2026-12-01", days=10))
    assert (out.result_date, out.weekday) == ("2026-12-15", "Tuesday")
    assert date_math(DateMathIn(operation="add_business_days", start="2026-12-14", days=-1)).result_date == "2026-12-11"
    assert date_math(DateMathIn(operation="days_between", start="2026-01-01", end="2026-03-01")).number == 59
    assert date_math(DateMathIn(operation="business_days_between", start="2026-12-01", end="2026-12-15")).number == 10
    assert date_math(DateMathIn(operation="business_days_between", start="2026-12-15", end="2026-12-01")).number == -10
    assert date_math(DateMathIn(operation="weekday_of", start="2026-11-18")).weekday == "Wednesday"
    for bad in (dict(operation="add_days", start="2026-01-01"), dict(operation="days_between", start="2026-01-01")):
        with pytest.raises(ToolInputError):
            date_math(DateMathIn(**bad))
    for invalid in (dict(operation="add_days", start="2026-13-45", days=1), dict(operation="launch", start="2026-01-01"), dict(operation="add_days", start="2026-01-01", days=10**6)):
        with pytest.raises(Exception):
            DateMathIn(**invalid)


# ------------------------------------------------------------------------------------------------ durable invocation

def test_a_successful_call_is_recorded_before_and_after_it_runs(world):
    reg = default_registry()
    invoker = ToolInvoker(ToolStore(world["sf"]), reg)
    calc = reg.get("math.calculate")
    ctx = context(world, [calc])
    out = invoke(world, invoker, ctx, calc.wire_name, {"expression": "6 * 7"})
    assert (out.ok, out.status, out.tool_ref) == (True, "succeeded", "math.calculate@1") and json.loads(out.content) == {"expression": "6 * 7", "formatted": "42", "value": 42.0}
    view = invoker.store.get(world["sa"], out.invocation_id)
    assert (view.status, view.tool_id, view.tool_version, view.seq, view.side_effect, view.run_id) == ("succeeded", "math.calculate", 1, 1, "none", None)
    assert view.input == {"expression": "6 * 7"} and view.output["value"] == 42.0 and view.error_code is None and view.completed_at >= view.started_at
    assert len(view.args_hash) == 64 and view.execution_id == ctx.execution_id and view.agent_key == "research"
    again = invoke(world, invoker, context(world, [calc]), calc.wire_name, {"expression": "6 * 7"})  # same arguments, another execution: same hash, different identity
    assert invoker.store.get(world["sa"], again.invocation_id).args_hash == view.args_hash and again.invocation_id != out.invocation_id


def test_every_refusal_is_recorded_as_rejected_with_a_fixed_reason_and_runs_nothing(world):
    registry = open_registry()
    registry.register(spec(), lambda a: Out(n=a.n))
    registry.register(spec(id="test.mutate", risk="write", side_effect="non_idempotent"), lambda a: Out(n=a.n))
    registry.register(spec(id="test.off", lifecycle="disabled"), lambda a: Out(n=a.n))
    registry.register(spec(id="test.other"), lambda a: Out(n=a.n))
    invoker = ToolInvoker(ToolStore(world["sf"]), registry)
    echo, mutate, off = registry.get("test.echo"), registry.get("test.mutate"), registry.get("test.off", 1)
    ctx = context(world, [echo, mutate, off])
    cases = [(registry.get("test.other").wire_name, '{"n": 1}', "not_permitted"),  # registered, but this agent was not granted it
             ("shell__exec__v1", '{"cmd": "rm -rf /"}', "unknown_tool"), (mutate.wire_name, '{"n": 1}', "approval_required"), (off.wire_name, '{"n": 1}', "tool_unavailable"),
             (echo.wire_name, '{"n": "not a number"}', "invalid_input"), (echo.wire_name, '{"n": 1, "extra": 2}', "invalid_input"), (echo.wire_name, "[1]", "invalid_input"),
             (echo.wire_name, "not json", "invalid_input"), (echo.wire_name, '{"n": 1}' + " " * 9000, "invalid_input")]
    for seq, (name, args, code) in enumerate(cases, start=1):
        out = invoke(world, invoker, ctx, name, args, seq)
        assert (out.ok, out.status, out.error_code) == (False, "rejected", code), (name, code)
        body = json.loads(out.content)
        assert body["error"] == code and body["message"]
    stored = rows(world)
    assert [r[3] for r in stored] == ["rejected"] * len(cases) and [r[4] for r in stored] == [c[2] for c in cases]
    detail = invoker.store.get(world["sa"], invoke(world, invoker, ctx, echo.wire_name, '{"n": "x", "bad": 1}', 20).invocation_id).output["detail"]
    assert detail == "Invalid fields: bad, n"  # field PATHS only: the submitted values are not echoed
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'running'") == 0


def test_a_step_can_never_be_dispatched_twice(world):
    reg = default_registry()
    invoker = ToolInvoker(ToolStore(world["sf"]), reg)
    calc = reg.get("math.calculate")
    ctx = context(world, [calc])
    invoke(world, invoker, ctx, calc.wire_name, {"expression": "1+1"}, 1)
    with pytest.raises(ToolError) as info:
        invoke(world, invoker, ctx, calc.wire_name, {"expression": "1+1"}, 1)
    assert info.value.code == "duplicate_call" and world["db"].scalar("SELECT count(*) FROM tool_invocations") == 1
    key_before = world["db"].scalar("SELECT idempotency_key FROM tool_invocations")
    barrier, outcomes = threading.Barrier(6), []
    store = invoker.store

    def race():
        barrier.wait()
        try:
            store.begin(ctx, calc, 2, '{"expression":"2+2"}', "a" * 64, "k" * 64 if False else uuid.uuid4().hex + uuid.uuid4().hex[:32])
            outcomes.append("ok")
        except Exception as exc:  # noqa: BLE001
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=race) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("ok") == 1 and outcomes.count("IntegrityError") == 5  # six racing dispatches of (execution, seq 2): exactly one row
    assert world["db"].scalar("SELECT idempotency_key FROM tool_invocations WHERE seq = 1") == key_before


def test_timeouts_failures_oversized_and_invalid_results_are_classified_and_secrets_are_redacted(world):
    registry = open_registry()

    def slow(a):
        time.sleep(1.0)
        return Out(n=1)

    def boom(a):
        raise RuntimeError("secret internal detail sk-abcdefghijklmnopqrstuvwxyz123456")

    def semantic(a):
        raise ToolInputError("n must be positive")

    class Text(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str

    registry.register(spec(id="test.slow", timeout_seconds=0.2), slow)
    registry.register(spec(id="test.boom"), boom)
    registry.register(spec(id="test.semantic"), semantic)
    registry.register(spec(id="test.wrong"), lambda a: {"n": 1})  # not an output model instance
    registry.register(spec(id="test.big", output_model=Text, max_output_bytes=100), lambda a: Text(text="x" * 500))
    registry.register(spec(id="test.leak", output_model=Text), lambda a: Text(text="the key is sk-abcdefghijklmnopqrstuvwxyz123456 ok"))
    invoker = ToolInvoker(ToolStore(world["sf"]), registry)
    specs = {s.id: s for s in registry.list_specs()}
    ctx = context(world, list(specs.values()))
    results = {}
    for seq, name in enumerate(("test.slow", "test.boom", "test.semantic", "test.wrong", "test.big", "test.leak"), start=1):
        results[name] = invoke(world, invoker, ctx, specs[name].wire_name, '{"n": 1}', seq)
    assert [(n, r.status, r.error_code) for n, r in results.items()] == [("test.slow", "timed_out", "timeout"), ("test.boom", "failed", "tool_error"), ("test.semantic", "failed", "invalid_input"),
                                                                       ("test.wrong", "failed", "output_invalid"), ("test.big", "failed", "output_too_large"), ("test.leak", "succeeded", None)]
    assert "sk-abc" not in json.dumps(rows(world, "SELECT * FROM tool_invocations")) and "internal detail" not in json.dumps(rows(world, "SELECT * FROM tool_invocations"))  # exception text never stored
    assert "[redacted]" in invoker.store.get(world["sa"], results["test.leak"].invocation_id).output["text"] and "n must be positive" in results["test.semantic"].content
    assert invoker.store.get(world["sa"], results["test.big"].invocation_id).output is None  # an oversized result is discarded, not stored
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'running'") == 0


def test_a_cancelled_call_is_recorded_cancelled_and_the_cancellation_propagates(world):
    registry = open_registry()
    started = threading.Event()

    def slow(a):
        started.set()
        time.sleep(1.5)
        return Out(n=1)

    registry.register(spec(id="test.slow", timeout_seconds=5), slow)
    invoker = ToolInvoker(ToolStore(world["sf"]), registry)
    slow_spec = registry.get("test.slow")
    ctx = context(world, [slow_spec])

    async def scenario():
        job = asyncio.ensure_future(invoker.invoke(ctx, slow_spec.wire_name, '{"n": 1}', 1))
        while not started.is_set():
            await asyncio.sleep(0.02)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job

    asyncio.run(scenario())
    assert rows(world) == [("test.slow", 1, 1, "cancelled", "cancelled")]


def test_a_stale_owner_cannot_start_or_finish_a_tool_call(world):
    reg = default_registry()
    calc = reg.get("math.calculate")
    plan_id = make_plan(world, [task("a", "research")])
    old = PlanRunStore(world["sf"], owner_id="owner-old", config=FAST)
    run_id = old.create(world["sa"], plan_id)
    fence_old = Fence(run_id, "owner-old", 1)
    _, tasks = old.load_for_driver(run_id)
    eid = old.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence_old)
    invoker = ToolInvoker(ToolStore(world["sf"]), reg)
    ctx = InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={calc.wire_name: calc}, run=ToolRunContext(run_id, "a", fence_old))
    first = invoke(world, invoker, ctx, calc.wire_name, {"expression": "1+1"}, 1)  # the owner can
    assert first.ok and world["db"].rows("SELECT run_id IS NOT NULL, task_key FROM tool_invocations") == [(1, "a")]  # linked to its run and task
    expire(world)
    assert PlanRunStore(world["sf"], owner_id="owner-new", config=FAST).leases.takeover(run_id, "owner-new", 30) is not None
    before = rows(world, "SELECT * FROM tool_invocations")
    with pytest.raises(LeaseLost):
        invoke(world, invoker, ctx, calc.wire_name, {"expression": "2+2"}, 2)  # a stale owner can start nothing
    assert rows(world, "SELECT * FROM tool_invocations") == before


def test_a_crash_leaves_a_running_call_that_recovery_closes_with_its_execution(world):
    registry = open_registry()
    registry.register(spec(id="test.pure"), lambda a: Out(n=a.n))
    registry.register(spec(id="test.effect", risk="write", side_effect="non_idempotent"), lambda a: Out(n=a.n))  # not executable here; only its stored class matters
    pure, effect = registry.get("test.pure"), registry.get("test.effect")
    store = ToolStore(world["sf"])
    for tool_spec, seq in ((pure, 1), (effect, 2)):
        ctx = context(world, [pure, effect]) if seq == 1 else ctx
        started = store.begin(ctx, tool_spec, seq, '{"n":1}', "h" * 64, uuid.uuid4().hex + uuid.uuid4().hex[:32])  # intent written, the process "died" before the outcome
        if seq == 2:
            store.start_dispatch(ctx, started)  # the dispatch had STARTED when it died: only then is the outcome unknown (a dispatch that never started ends ``cancelled``, see test_durable_dispatch)
    done = store.begin(ctx, pure, 3, '{"n":2}', "h" * 64, uuid.uuid4().hex + uuid.uuid4().hex[:32])
    store.finish(done, "succeeded", output='{"n":2}')
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'running'") == 2
    with world["sf"]() as session, session.begin():
        assert close_open(session, [ctx.execution_id]) == 2
    assert rows(world) == [("test.pure", 1, 1, "interrupted", "interrupted"), ("test.effect", 1, 2, "uncertain", "uncertain"), ("test.pure", 1, 3, "succeeded", None)]
    with world["sf"]() as session, session.begin():
        assert close_open(session, [ctx.execution_id]) == 0 and close_open(session, []) == 0  # idempotent; finished calls are never touched


def test_plan_run_recovery_and_the_startup_sweep_close_in_flight_tool_calls_together_with_their_execution(world):
    reg = default_registry()
    calc = reg.get("math.calculate")
    plan_id = make_plan(world, [task("a", "research")])
    store = world["svc"].store
    run_id = store.create(world["sa"], plan_id)
    fence = Fence(run_id, store.owner_id, 1)
    _, tasks = store.load_for_driver(run_id)
    eid = store.start_task(tasks[0].id, world["sa"], BUILTIN, "Research", "t", "groq", None, executions=world["executions"].store, fence=fence)
    ToolStore(world["sf"]).begin(InvocationContext(scope=world["sa"], execution_id=eid, agent_key="research", agent_version_id=None, allowed={}, run=ToolRunContext(run_id, "a", fence)),
                                 calc, 1, '{"expression":"1+1"}', "h" * 64, "k" * 64)
    expire(world)
    assert store.recover_expired() == [run_id]
    assert rows(world) == [("math.calculate", 1, 1, "interrupted", "interrupted")] and world["db"].rows("SELECT status, error_code FROM agent_executions") == [("failed", "interrupted")]
    # a standalone execution that is swept at startup takes its in-flight call with it
    standalone = ExecutionStore(world["sf"]).start(world["sa"], BUILTIN, "Research", "t", provider="groq", model=None)
    ToolStore(world["sf"]).begin(InvocationContext(scope=world["sa"], execution_id=standalone, agent_key="research", agent_version_id=None, allowed={}), calc, 1, '{"expression":"2+2"}', "h" * 64, "j" * 64)
    assert ExecutionStore(world["sf"]).fail_orphaned() == 1
    assert world["db"].rows("SELECT status FROM tool_invocations ORDER BY created_at") == [("interrupted",), ("interrupted",)]


def test_the_invocation_table_enforces_its_own_rules(world):
    reg = default_registry()
    calc = reg.get("math.calculate")
    invoker = ToolInvoker(ToolStore(world["sf"]), reg)
    out = invoke(world, invoker, context(world, [calc]), calc.wire_name, {"expression": "1+1"})
    running = invoker.store.begin(context(world, [calc]), calc, 1, '{"expression":"3+3"}', "h" * 64, "z" * 64)
    for statement in ("UPDATE tool_invocations SET status = 'running', completed_at = NULL, error_code = NULL WHERE status = 'succeeded'", "UPDATE tool_invocations SET output = 'x' WHERE status = 'succeeded'",
                      "DELETE FROM tool_invocations", f"UPDATE tool_invocations SET tool_id = 'other.tool' WHERE id = '{running.hex}'", f"UPDATE tool_invocations SET input = '{{}}' WHERE id = '{running.hex}'",
                      f"UPDATE tool_invocations SET seq = 9 WHERE id = '{running.hex}'", f"UPDATE tool_invocations SET status = 'weird', completed_at = '2026-01-01' WHERE id = '{running.hex}'",
                      f"UPDATE tool_invocations SET status = 'failed', completed_at = '2026-01-01', error_code = NULL WHERE id = '{running.hex}'"):
        with pytest.raises(DatabaseError):
            world["db"].run(statement)
    assert invoker.store.finish(running, "succeeded", output="{}") is True and invoker.store.finish(running, "failed", error_code="tool_error") is False  # one transition, once
    assert world["db"].scalar(f"SELECT status FROM tool_invocations WHERE id = '{out.invocation_id.hex}'") == "succeeded"


def test_invocations_are_scoped_to_their_user_and_workspace_and_listing_and_usage_are_real(world):
    reg = default_registry()
    calc, text = reg.get("math.calculate"), reg.get("text.analyze")
    invoker = ToolInvoker(ToolStore(world["sf"]), reg)
    ctx = context(world, [calc, text])
    first = invoke(world, invoker, ctx, calc.wire_name, {"expression": "1+1"}, 1)
    invoke(world, invoker, ctx, calc.wire_name, {"expression": "2+2"}, 2)
    invoke(world, invoker, ctx, text.wire_name, {"text": "Short text here."}, 3)
    invoke(world, invoker, ctx, "nope__nope__v1", "{}", 4)  # rejected: not counted as usage
    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    member = authorize_memory_scope(world["sf"], world["b"], team)
    for other in (world["sb"], member):  # another user; the same user in another workspace
        with pytest.raises(InvocationNotFound):
            invoker.store.get(other, first.invocation_id)
        assert invoker.store.list(other)["items"] == [] and invoker.store.usage(other) == {}
    page = invoker.store.list(world["sa"], limit=2)
    assert [v.seq for v in page["items"]] == [4, 3] or len(page["items"]) == 2
    rest = invoker.store.list(world["sa"], limit=10, before=page["next"])
    assert len(page["items"]) + len(rest["items"]) == 4 and rest["next"] is None
    assert {v.seq for v in invoker.store.list(world["sa"], tool_id="math.calculate")["items"]} == {1, 2} and [v.status for v in invoker.store.list(world["sa"], status="rejected")["items"]] == ["rejected"]
    usage = invoker.store.usage(world["sa"])
    assert usage[("math.calculate", 1)]["calls"] == 2 and usage[("text.analyze", 1)]["calls"] == 1 and ("nope__nope__v1", None) not in usage
    for bad in (0, 101, "3"):
        with pytest.raises(ValueError):
            invoker.store.list(world["sa"], limit=bad)


# ------------------------------------------------------------------------------------------------ definitions pin tools

def test_an_agent_definition_pins_its_tools_and_refuses_what_is_not_executable(world):
    world["agents"].create(world["sa"], "pinned", definition(allowed_tools=["math.calculate", "finance.unit_economics@1"]))
    assert world["agents"].resolve(world["sa"], "pinned").definition.allowed_tools == ["math.calculate@1", "finance.unit_economics@1"]  # pinned to exact versions on save
    for bad in (["web.search"], ["math.calculate@9"], ["math.calculate", "math.calculate@1"], ["math.calculate@x"], ["rm -rf /"], ["math.calculate"] * 2, [f"time.date_math"] * 3,
                [f"x{i}.tool" for i in range(9)]):
        with pytest.raises(Exception):
            definition(allowed_tools=bad)
    assert definition(allowed_tools=[]).allowed_tools == [] and definition().allowed_tools == []


# ------------------------------------------------------------------------------------------------ the agent tool loop

def test_an_agent_uses_a_tool_through_the_real_execution_path_and_the_exchange_is_recorded(world):
    slug = make_agent(world, ("math.calculate", "finance.unit_economics"))
    provider = Scripted(asks(call("math__calculate__v1", {"expression": "49 * 12"}), text="Let me calculate."), final("The yearly price is 588."))
    svc, invoker = service(world, provider)
    outcome = asyncio.run(svc.run(world["sa"], slug, "What does the plan cost per year?"))
    assert (outcome.status, outcome.result) == ("succeeded", "The yearly price is 588.")
    first, second = provider.requests
    assert [t.name for t in first.tools] == ["math__calculate__v1", "finance__unit_economics__v1"] and first.tools[0].parameters["properties"]["expression"]
    assert "act ONLY through the tools" in first.system and "Calculator (math.calculate@1)" in first.system and "You have no tools" not in first.system
    assert second.messages[-2].role == "assistant" and second.messages[-2].tool_calls[0].name == "math__calculate__v1"
    tool_message = second.messages[-1]
    assert tool_message.role == "tool" and tool_message.tool_call_id == "c1" and json.loads(tool_message.content)["value"] == 588.0  # the model gets the validated result
    view = invoker.store.list(world["sa"])["items"][0]
    assert (view.status, view.tool_id, view.agent_key, view.execution_id) == ("succeeded", "math.calculate", slug, outcome.execution_id)
    assert world["db"].scalar("SELECT agent_version_id IS NOT NULL FROM tool_invocations") == 1  # the exact agent version that was allowed to call it
    assert world["db"].rows("SELECT status FROM agent_executions") == [("succeeded",)]


def test_an_agent_without_tools_is_unchanged_and_never_offered_any(world):
    slug = make_agent(world, tools=())
    provider = Scripted(final("plain answer"))
    svc, invoker = service(world, provider)
    outcome = asyncio.run(svc.run(world["sa"], slug, "hello"))
    assert outcome.result == "plain answer" and provider.requests[0].tools == () and "You have no tools" in provider.requests[0].system
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == 0
    # an agent that HAS tools but runs where no tool layer exists behaves the same (nothing is offered, nothing can run)
    slug2 = make_agent(world, ("math.calculate",), slug="toolless-runtime")
    provider2 = Scripted(final("no tools offered"))
    plain = ExecutionService(world["sf"], orchestrator=world["orch"], providers=ProviderRegistry({"groq": lambda: provider2}), short_term=ShortTermMemory(client=FakeRedis()),
                             long_term=MemoryRepository(world["sf"]), limits=Limits())
    assert asyncio.run(plain.run(world["sa"], slug2, "hi")).result == "no tools offered" and provider2.requests[0].tools == ()


def test_model_output_is_untrusted_ungranted_unknown_and_malformed_calls_are_refused_and_recorded(world):
    slug = make_agent(world, ("math.calculate",))
    provider = Scripted(asks(call("text__analyze__v1", {"text": "hello world this is text"}, "c1"), call("shell__run__v1", {"cmd": "ls"}, "c2"), call("math__calculate__v1", {"expr": "1+1"}, "c3"), text=""),
                        asks(call("math__calculate__v1", {"expression": "2 + 2"}, "c4")), final("4"))
    svc, invoker = service(world, provider)
    outcome = asyncio.run(svc.run(world["sa"], slug, "do things"))
    assert outcome.result == "4"
    assert rows(world) == [("text.analyze", 1, 1, "rejected", "not_permitted"), ("shell__run__v1", None, 2, "rejected", "unknown_tool"),
                           ("math.calculate", 1, 3, "rejected", "invalid_input"), ("math.calculate", 1, 4, "succeeded", None)]
    answers = [json.loads(m.content) for m in provider.requests[1].messages if m.role == "tool"]
    assert [a["error"] for a in answers] == ["not_permitted", "unknown_tool", "invalid_input"]  # the model is told why and can carry on
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations WHERE status = 'running'") == 0


def test_the_loop_is_bounded_in_rounds_and_in_calls(world):
    slug = make_agent(world, ("math.calculate",))
    endless = [asks(call("math__calculate__v1", {"expression": f"{i}+1"}, f"c{i}")) for i in range(20)]
    provider = Scripted(*endless)
    svc, invoker = service(world, provider)
    with pytest.raises(Exception) as info:  # the model keeps asking for tools even when none are offered any more: refused, never an endless loop
        asyncio.run(svc.run(world["sa"], slug, "never stop"))
    assert info.value.code == "invalid_response" and len(provider.requests) == MAX_TOOL_ROUNDS + 1
    assert provider.requests[-1].tools == () and all(r.tools for r in provider.requests[:-1])  # the last request offers no tools
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations") == MAX_TOOL_ROUNDS
    # three calls a round for three rounds: the 9th call exceeds the per-execution cap and is refused (and recorded); the final answer is then requested without tools
    slug2 = make_agent(world, ("math.calculate",), slug="busy-agent")
    many = [asks(*[call("math__calculate__v1", {"expression": f"{r}+{i}"}, f"c{r}-{i}") for i in range(3)]) for r in range(3)] + [final("enough")]
    provider2 = Scripted(*many)
    svc2, _ = service(world, provider2)
    assert asyncio.run(svc2.run(world["sa"], slug2, "many")).result == "enough"
    stored = world["db"].rows("SELECT t.status, t.error_code FROM tool_invocations t JOIN agent_executions e ON e.id = t.execution_id WHERE e.agent_key = 'busy-agent' ORDER BY t.seq")
    assert stored == [("succeeded", None)] * MAX_TOOL_CALLS + [("rejected", "limit_exceeded")] and provider2.requests[-1].tools == ()
    # more calls than MAX_CALLS_PER_ROUND in one round: the extra ones are dropped, never run
    from agentplatform.execution import MAX_CALLS_PER_ROUND

    slug3 = make_agent(world, ("math.calculate",), slug="greedy-agent")
    provider3 = Scripted(asks(*[call("math__calculate__v1", {"expression": f"1+{i}"}, f"g{i}") for i in range(9)]), final("ok"))
    svc3, _ = service(world, provider3)
    assert asyncio.run(svc3.run(world["sa"], slug3, "greedy")).result == "ok"
    assert world["db"].scalar("SELECT count(*) FROM tool_invocations t JOIN agent_executions e ON e.id = t.execution_id WHERE e.agent_key = 'greedy-agent'") == MAX_CALLS_PER_ROUND
    assert len([m for m in provider3.requests[1].messages if m.role == "tool"]) == MAX_CALLS_PER_ROUND  # the model is only answered for the calls that were honoured


def test_tool_calls_of_a_plan_task_are_linked_to_its_run_and_a_retry_never_reuses_a_finished_call(world):
    slug = make_agent(world, ("math.calculate",))
    provider = Scripted(asks(call("math__calculate__v1", {"expression": "7*6"})), final("42"), final("second attempt, no tools needed"))
    svc, invoker = service(world, provider)
    runs = PlanRunService(world["sf"], executions=svc, registry=RunRegistry(config=FAST), busy_poll=0.01)
    plan_id = make_plan(world, [task("a", slug)])

    async def scenario():
        started = await runs.start(world["sa"], plan_id)
        await runs.registry.wait(started.id, 30)
        return started.id

    run_id = asyncio.run(scenario())
    assert runs.store.get(world["sa"], run_id).status == "succeeded"
    assert world["db"].rows("SELECT run_id IS NOT NULL, task_key, status FROM tool_invocations") == [(1, "a", "succeeded")]
    listed = invoker.store.list(world["sa"], run_id=run_id)["items"]
    assert [(v.task_key, v.tool_id) for v in listed] == [("a", "math.calculate")] and invoker.store.list(world["sb"], run_id=run_id)["items"] == []


def test_the_groq_adapter_translates_tool_calls_both_ways_without_a_live_provider():
    captured = {}

    class Fn:
        name, arguments = "math__calculate__v1", '{"expression": "1+1"}'

    class Call:
        id, function = "call_1", Fn()

    class Choice:
        class message:
            content, tool_calls = None, [Call()]
        finish_reason = "tool_calls"

    class Completions:
        async def create(self, **options):
            captured.update(options)

            class Response:
                choices, model, usage = [Choice()], "m", None

            return Response()

    class Client:
        class chat:
            completions = Completions()

    provider = GroqProvider(client=Client())
    request = ProviderRequest(model="m", system="s", messages=(Message("user", "hi"), Message("assistant", "", tool_calls=(ToolCall("call_0", "x__y__v1", "{}"),)), Message("tool", "{}", tool_call_id="call_0")),
                              tools=(ToolDefinition("math__calculate__v1", "calc", {"type": "object"}),))
    response = asyncio.run(provider.complete(request))
    assert response.text == "" and [(c.id, c.name, c.arguments) for c in response.tool_calls] == [("call_1", "math__calculate__v1", '{"expression": "1+1"}')]
    assert captured["tools"] == [{"type": "function", "function": {"name": "math__calculate__v1", "description": "calc", "parameters": {"type": "object"}}}] and captured["tool_choice"] == "auto"
    roles = [m["role"] for m in captured["messages"]]
    assert roles == ["system", "user", "assistant", "tool"] and captured["messages"][2]["tool_calls"][0]["function"]["name"] == "x__y__v1" and captured["messages"][3]["tool_call_id"] == "call_0"
    captured.clear()
    asyncio.run(provider.complete(ProviderRequest(model="m", system="s", messages=(Message("user", "hi"),))))
    assert "tools" not in captured and "tool_choice" not in captured  # a plain request is exactly what it was before M4-B


# ------------------------------------------------------------------------------------------------ bounded execution of the shipped tools (M4-B final pass)

def test_business_days_between_is_constant_time_and_equals_the_brute_force_count():
    import random
    from datetime import date, timedelta

    def brute(low, high):
        return sum(1 for o in range(1, (high - low).days + 1) if (low + timedelta(days=o)).weekday() < 5)

    rng = random.Random(11)
    for _ in range(300):
        a = date.fromordinal(rng.randint(1, 800000))
        b = a + timedelta(days=rng.randint(0, 2000))
        assert date_math(DateMathIn(operation="business_days_between", start=a, end=b)).number == brute(a, b)
        assert date_math(DateMathIn(operation="business_days_between", start=b, end=a)).number == -brute(a, b)
    assert date_math(DateMathIn(operation="business_days_between", start=date(2026, 11, 16), end=date(2026, 11, 16))).number == 0
    started = time.perf_counter()
    assert date_math(DateMathIn(operation="business_days_between", start="0001-01-02", end="9999-12-31")).number == 2608613
    assert time.perf_counter() - started < 0.1  # was ~1.9 s with the day-by-day loop: nearly the tool's whole 2 s budget


def test_every_shipped_tool_finishes_its_worst_case_input_far_inside_its_time_limit(monkeypatch):
    """The invoker cannot kill a running thread, so the shipped tools must be bounded by construction. Worst cases within the 8,000-character argument cap."""
    reg = default_registry()
    cases = {
        "math.calculate": CalculateIn(expression="(" * 79 + "1" + ")" * 79),  # the deepest nesting the tool accepts (deeper is refused at once)
        "finance.unit_economics": UnitEconomicsIn(monthly_price=1e9, gross_margin_pct=100, monthly_churn_pct=1e-300, cac=0),
        "text.analyze": TextAnalyzeIn(text="a. " * 2600, top_keywords=15),
        "data.summarize_csv": SummarizeCsvIn(csv_text=("a\n" + "1\n" * 3990)[:8000]),
        "time.date_math": DateMathIn(operation="business_days_between", start="0001-01-02", end="9999-12-31"),
    }
    from agentplatform.tools import holidays
    import json as _json

    # the network tool's worst case: the largest response it will accept (200 rows of maximum-length text), served locally
    big = _json.dumps([{"date": f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}", "localName": "x" * 200, "name": "y" * 200, "global": True} for i in range(200)]).encode()
    assert len(big) < holidays.MAX_BYTES
    monkeypatch.setattr(holidays, "fetch", lambda url, **kw: big)
    cases["calendar.public_holidays"] = holidays.HolidaysIn(country="US", year=2026)
    assert set(cases) == {s.id for s in reg.list_specs()}, "a new shipped tool needs a worst-case entry here"
    for tool_id, args in cases.items():
        spec = reg.get(tool_id)
        started = time.perf_counter()
        reg.handler(spec)(args)
        assert time.perf_counter() - started < spec.timeout_seconds / 4, tool_id
