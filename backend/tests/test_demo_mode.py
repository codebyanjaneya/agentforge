"""M5: demo mode and the starter workforce. The scripted model and the simulated sending tool are the ONLY stand-ins; the plan, the run, the tools' computations, the approval gate
and the history are the real product, and the tests check the REAL numbers the real tools produce."""

import importlib
import time

import pytest
from pydantic import ValidationError

from config import load_settings
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_config import env, problems
from tests.test_plan_runs_api import A, B, papi, start, wait  # noqa: F401  (papi is a fixture)


def get(api, path, who=A):
    return api.client.get(path, headers=api.auth(*who))


def post(api, path, who=A, **kw):
    return api.client.post(path, headers=api.auth(*who), **kw)


# ------------------------------------------------------------------------------------------------ configuration

def test_demo_mode_needs_no_model_key_and_is_refused_in_production():
    settings = load_settings(env(APP_ENV="test", AGENTFORGE_DEMO_MODE="1", GROQ_API_KEY=""))
    assert settings.demo_mode is True and settings.groq_api_key.get_secret_value() == "demo-mode-no-model-key"
    assert any("AGENTFORGE_DEMO_MODE" in p and "production" in p for p in problems(APP_ENV="production", AGENTFORGE_DEMO_MODE="1"))
    assert load_settings(env(APP_ENV="test")).demo_mode is False  # off unless asked for
    assert any(p.startswith("GROQ_API_KEY") for p in problems(APP_ENV="test", GROQ_API_KEY=""))  # a normal run still requires a key


# ------------------------------------------------------------------------------------------------ the scripted model reads the user's numbers and declines what it cannot do

def test_the_scripted_planner_plans_one_workflow_reads_the_numbers_and_declines_anything_else():
    demo = importlib.import_module("agentplatform.demo")
    catalog = " ".join(demo.STARTER)
    i = demo.read_inputs("We launch a $79/month plan. 70% gross margin, 4% monthly churn and a $600 CAC. Tell sam@acme.io.")
    assert (i["price"], i["margin"], i["churn"], i["cac"], i["recipient"]) == (79.0, 70.0, 4.0, 600.0, "sam@acme.io")
    assert demo.read_inputs("launch it")["cac"] == demo.DEFAULTS["cac"]  # a number the user did not give falls back to the documented default
    plan = demo.plan_json(demo.EXAMPLE_OBJECTIVE, catalog)
    assert plan["feasible"] and [t["agent"] for t in plan["tasks"]] == list(demo.STARTER) and plan["tasks"][2]["depends_on"] == ["economics", "dates"]
    assert "price=49; margin=80; churn=3; cac=400" in plan["tasks"][0]["description"]
    other = demo.plan_json("Write me a poem about the sea", catalog)
    assert other["feasible"] is False and "script" in other["unsupported"][0]["reason"]  # honest: it is not a live model
    assert demo.plan_json(demo.EXAMPLE_OBJECTIVE, "")["feasible"] is False  # the starter workforce is needed first


# ------------------------------------------------------------------------------------------------ the starter workforce (a product feature in every mode)

def test_the_starter_workforce_is_created_once_from_templates_and_never_overwrites(papi):
    first = post(papi, "/starter-workforce").json()["agents"]
    assert [(a["slug"], a["created"]) for a in first] == [("unit-economics-analyst", True), ("launch-planner", True), ("launch-comms", True)]
    assert first[0]["tools"] == ["finance.unit_economics@1", "math.calculate@1"] and first[2]["tools"] == ["text.analyze@1"]  # real, pinned tools
    again = post(papi, "/starter-workforce").json()["agents"]
    assert [a["created"] for a in again] == [False, False, False]
    assert len(get(papi, "/agents?status=active").json()["items"]) == 3
    assert post(papi, "/starter-workforce", who=B).json()["agents"][0]["created"] is True  # per workspace
    assert get(papi, "/demo").json()["demo"] is False and get(papi, "/demo").json()["model"] == "live"
    assert papi.client.get("/demo").status_code == 401 and papi.client.post("/starter-workforce").status_code == 401


# ------------------------------------------------------------------------------------------------ the whole journey, over REST, with REAL tool results

def test_the_launch_readiness_journey_runs_real_tools_asks_before_sending_and_reports_real_numbers(papi):
    demo = importlib.import_module("agentplatform.demo")
    tools = importlib.import_module("agentplatform.tools")
    fixtures = importlib.import_module("agentplatform.tools.fixtures")
    providers = importlib.import_module("agentplatform.providers")
    registry = tools.default_registry()
    registry.register(fixtures.DEMO_SEND_NOTE, fixtures.send_note)  # the SIMULATED tool: what demo mode registers
    fixtures.OUTBOX.clear()
    papi.main.providers = providers.ProviderRegistry({"groq": lambda: demo.DemoProvider()})
    try:
        for slug, definition, template in demo.starter_definitions(True):
            created = post(papi, "/agents", json={"slug": slug, "definition": definition.model_dump(mode="json")})
            assert created.status_code == 201, created.text
        comms = [a for a in get(papi, "/agents?status=active").json()["items"] if a["slug"] == "launch-comms"][0]
        assert get(papi, f"/agents/{comms['id']}").json()["definition"]["allowed_tools"] == ["text.analyze@1", "demo.send_note@1"]
        # the objective -> a structured plan (scripted planner, real validation)
        plan = post(papi, "/plans", json={"objective": demo.EXAMPLE_OBJECTIVE})
        assert plan.status_code == 201, plan.text
        body = plan.json()
        pid = body["id"]
        tasks = {t["id"]: t for t in body["revision"]["tasks"]}
        assert set(tasks) == {"economics", "dates", "announce"} and set(tasks["announce"]["depends_on"]) == {"economics", "dates"}
        assert post(papi, f"/plans/{pid}/approve", json={"revision": 1}).status_code == 200
        run = start(papi, pid).json()
        # the two independent specialists finish with REAL tool results; the third pauses for a human
        deadline, pending = time.time() + 30, []
        while time.time() < deadline and not pending:
            pending = get(papi, "/approvals?status=pending").json()["items"]
            time.sleep(0.1)
        assert pending and fixtures.OUTBOX == []  # nothing was sent before a person decided
        approval = pending[0]
        assert (approval["tool"]["ref"], approval["agent"], approval["task"]) == ("demo.send_note@1", "launch-comms", "announce")
        assert approval["arguments"]["recipient"] == "first.customer@example.com" and "LTV:CAC" in approval["arguments"]["body"] and "goes live on" in approval["arguments"]["body"]
        assert "The first customer should hear" in approval["reason"]
        invocations = get(papi, f"/plan-runs/{run['id']}/tool-invocations").json()["items"]
        by_tool = {i["tool"]["id"]: i for i in invocations}
        assert {"finance.unit_economics", "time.date_math", "text.analyze"} <= set(by_tool) and all(i["status"] == "succeeded" for i in by_tool.values())
        economics = get(papi, f"/tool-invocations/{by_tool['finance.unit_economics']['id']}").json()["output"]
        assert round(economics["ltv"], 2) == 1306.67 and round(economics["ltv_to_cac"], 2) == 3.27 and economics["assessment"] == "healthy"  # 49 * 0.8 / 0.03, over a 400 CAC
        assert post(papi, f"/approvals/{approval['id']}/approve", json={"payload_hash": approval["payload_hash"]}).status_code == 200
        final = wait(papi, run["id"], timeout=40)
        assert final["status"] == "succeeded" and [t["status"] for t in final["tasks"]] == ["succeeded"] * 3
        assert len(fixtures.OUTBOX) == 1 and fixtures.OUTBOX[0][0] == "first.customer@example.com"
        result = get(papi, f"/plan-runs/{run['id']}").json()["outputs"][0]["result"] if "outputs" in get(papi, f"/plan-runs/{run['id']}").json() else ""
        events = [e["type"] for e in get(papi, f"/plan-runs/{run['id']}/events").json()["items"]]
        assert events.count("approval_consumed") == 1 and events.count("task_suspended") == 1 and events[-1] == "run_completed"
        assert result == "" or "simulated" in result  # the final text says the send was simulated
    finally:
        registry._tools.pop(("demo.send_note", 1), None)



def test_the_communications_agent_does_not_send_when_the_real_numbers_are_not_healthy():
    """Judgment on REAL results: with weak unit economics the agent writes no note, asks for no approval and says why."""
    import json
    from types import SimpleNamespace

    demo = importlib.import_module("agentplatform.demo")
    system = "You are an AI agent running inside AgentForge. Launch communications lead"

    def reply(assessment):
        task = ("Overall objective: x\n\nYour task: Tell the first customer\n\nRecipient: sam@acme.io.\n\nResults of the tasks you depend on:\n"
                f"[Check the unit economics]\nUnit economics: LTV $400, LTV:CAC 1.4x, CAC payback 20.0 months. Assessment: {assessment}. Rule applied: r.\n\n"
                "[Set the launch date]\nLaunch date: 2026-12-02 (Wednesday). counted")
        return demo.agent_reply(SimpleNamespace(system=system, messages=[SimpleNamespace(role="user", content=task)]))

    weak = reply("marginal")
    assert not weak.tool_calls and weak.text.startswith("Not sent.") and "marginal" in weak.text and "1.4x" in weak.text
    assert not reply("unprofitable").tool_calls
    healthy = reply("healthy")
    assert [c.name for c in healthy.tool_calls] == ["text__analyze__v1"]  # healthy numbers go on to the readability check, then the send (which asks a person first)
    assert json.loads(healthy.tool_calls[0].arguments)["top_keywords"] == 3
