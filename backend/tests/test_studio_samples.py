"""M3-C support: record REAL responses of the backend (fake provider and fake built-in agents, no network) for the frontend's
contract test. Nothing here asserts product behaviour (the other test files do); it drives the whole Studio journey through the
real app once and writes the JSON bodies to ``$STUDIO_SAMPLES_OUT`` so the frontend can verify that the shapes it relies on are the
shapes the backend really returns. Skipped unless that variable is set."""

import copy
import json
import os
import sys
import time

import anyio.from_thread
import pytest

from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)

OUT = os.environ.get("STUDIO_SAMPLES_OUT")
A = ("user-a", "a@example.com")
AGENT = {"name": "Release Notes Writer", "description": "Turns a changelog into friendly release notes for customers.", "role": "Product marketer",
         "purpose": "Explain what changed and why it matters, without hype.", "instructions": "You write clear release notes from the changelog the user provides.",
         "capabilities": ["release notes"], "model": {"provider": "groq", "name": "llama-3.3-70b-versatile", "temperature": 0.5, "max_tokens": 512}}


def task(id, agent, deps=()):
    return {"id": id, "title": f"Task {id}", "description": f"Do the work for task {id} carefully.", "expected_output": "A short written result.",
            "agent": agent, "rationale": "This agent fits the work.", "depends_on": list(deps)}


@pytest.fixture
def papi(api):
    manager = anyio.from_thread.start_blocking_portal()
    api.client.portal = manager.__enter__()
    yield api
    manager.__exit__(None, None, None)


@pytest.mark.skipif(not OUT, reason="STUDIO_SAMPLES_OUT is not set")
def test_record_real_responses_for_the_frontend_contract(papi):
    api = papi
    h = api.auth(*A)
    samples, get = {}, lambda r: r.json()

    def rec(name, response, status=None):
        if status is not None:
            assert response.status_code == status, (name, response.status_code, response.text)
        samples[name] = {"status": response.status_code, "body": response.json()}
        return response.json()

    rec("templates", api.client.get("/agent-templates", headers=h), 200)
    first = samples["templates"]["body"]["items"][0]["id"]
    rec("template", api.client.get(f"/agent-templates/{first}", headers=h), 200)
    inst = rec("instantiated", api.client.post(f"/agent-templates/{first}/instantiate", json={"slug": "from-template"}, headers=h), 201)
    agent = rec("agent_created", api.client.post("/agents", json={"slug": "writer", "definition": AGENT}, headers=h), 201)
    rec("agents", api.client.get("/agents", headers=h), 200)
    edited = copy.deepcopy(AGENT)
    edited["role"] = "Second role"
    updated = rec("agent_updated", api.client.patch(f"/agents/{agent['id']}", json={"base_version": 1, "definition": edited, "change_note": "role"}, headers=h), 200)
    rec("versions", api.client.get(f"/agents/{agent['id']}/versions", headers=h), 200)
    rec("version", api.client.get(f"/agents/{agent['id']}/versions/1", headers=h), 200)
    rec("rolled_back", api.client.post(f"/agents/{agent['id']}/rollback", json={"to_version": 1, "base_version": updated["latest_version"]}, headers=h), 200)
    rec("archived", api.client.post(f"/agents/{inst['id']}/archive", headers=h), 200)
    rec("unarchived", api.client.post(f"/agents/{inst['id']}/unarchive", headers=h), 200)
    rec("error_version_conflict", api.client.patch(f"/agents/{agent['id']}", json={"base_version": 1, "definition": edited}, headers=h), 409)
    bad = copy.deepcopy(AGENT)
    bad["model"]["temperature"] = 9
    rec("error_validation", api.client.post("/agents", json={"slug": "other-one", "definition": bad}, headers=h), 422)
    rec("error_slug_taken", api.client.post("/agents", json={"slug": "writer", "definition": AGENT}, headers=h), 409)

    api.provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "A launch plan for the product.", "unsupported": [],
                                                     "tasks": [task("a", "research"), task("b", "writer", ["a"]), task("c", "sales", ["b"])]})
    plan = rec("plan", api.client.post("/plans", json={"objective": "Launch our new product to early customers within a month."}, headers=h), 201)
    pid = plan["id"]
    rec("plans", api.client.get("/plans", headers=h), 200)
    rec("revisions", api.client.get(f"/plans/{pid}/revisions", headers=h), 200)
    rec("revision", api.client.get(f"/plans/{pid}/revisions/1", headers=h), 200)
    rec("validation", api.client.post(f"/plans/{pid}/validate", headers=h), 200)
    rec("error_plan_not_approved", api.client.post(f"/plans/{pid}/runs", headers=h), 409)
    revised = rec("plan_revised", api.client.post(f"/plans/{pid}/revisions", json={"base_revision": 1, "tasks": [
        {k: v for k, v in task("a", "research").items() if k != "rationale"}, {k: v for k, v in task("b", "writer", ["a"]).items() if k != "rationale"}]}, headers=h), 200)
    rec("error_revision_conflict", api.client.post(f"/plans/{pid}/approve", json={"revision": 1}, headers=h), 409)
    rec("plan_approved", api.client.post(f"/plans/{pid}/approve", json={"revision": revised["latest_revision"]}, headers=h), 200)
    api.provider.reply = lambda request: f"custom reply: {request.messages[-1].content[-30:]}"
    run = rec("run_started", api.client.post(f"/plans/{pid}/runs", headers=h), 202)
    deadline = time.time() + 20
    while time.time() < deadline:
        body = api.client.get(f"/plan-runs/{run['id']}", headers=h).json()
        if body["status"] != "running":
            break
        time.sleep(0.05)
    rec("run", api.client.get(f"/plan-runs/{run['id']}", headers=h), 200)
    rec("run_task", api.client.get(f"/plan-runs/{run['id']}/tasks/b", headers=h), 200)
    rec("runs", api.client.get(f"/plans/{pid}/runs", headers=h), 200)
    rec("plan_with_run", api.client.get(f"/plans/{pid}", headers=h), 200)
    rec("run_cancelled_noop", api.client.post(f"/plan-runs/{run['id']}/cancel", headers=h), 200)
    rec("error_already_executed", api.client.post(f"/plans/{pid}/runs", headers=h), 409)
    rec("plan_cancelled", api.client.post(f"/plans/{pid}/cancel", headers=h), 200)

    # a failed run, for the partial-failure shapes
    sys_providers = sys.modules["agentplatform.providers"]
    api.provider.reply = lambda request: json.dumps({"feasible": True, "interpretation": "Two steps.", "unsupported": [], "tasks": [task("x", "writer"), task("y", "research", ["x"])]})
    pid2 = api.client.post("/plans", json={"objective": "Write the announcement and then research reactions."}, headers=h).json()["id"]
    api.client.post(f"/plans/{pid2}/approve", json={"revision": 1}, headers=h)
    api.provider.error = sys_providers.ProviderError("timeout")
    run2 = api.client.post(f"/plans/{pid2}/runs", headers=h).json()
    while api.client.get(f"/plan-runs/{run2['id']}", headers=h).json()["status"] == "running":
        time.sleep(0.05)
    rec("run_failed", api.client.get(f"/plan-runs/{run2['id']}", headers=h), 200)
    rec("run_failed_task", api.client.get(f"/plan-runs/{run2['id']}/tasks/x", headers=h), 200)
    api.provider.error = None
    api.provider.reply = lambda request: json.dumps({"feasible": False, "interpretation": "Charge cards.", "tasks": [],
                                                     "unsupported": [{"requirement": "Charge cards", "reason": "No agent can take payments."}]})
    rec("error_not_plannable", api.client.post("/plans", json={"objective": "Charge every customer card for the annual plan today."}, headers=h), 422)
    rec("error_not_found", api.client.get("/plans/00000000-0000-0000-0000-000000000000", headers=h), 404)

    # M4-B: the tool catalog and a REAL tool call made by a custom agent (scripted model: first it asks for a tool, then it answers)
    from agentplatform.providers import ProviderRegistry, ProviderResponse, ToolCall

    class ScriptedTools:
        name = "groq"

        def __init__(self):
            self.calls = 0

        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(text="", model="m", tool_calls=(ToolCall("c1", "math__calculate__v1", json.dumps({"expression": "49 * 12"})),))
            return ProviderResponse(text="The yearly price is 588.", model="m")

    rec("agent_with_tools", api.client.post("/agents", json={"slug": "calculator-agent", "definition": {**copy.deepcopy(AGENT), "allowed_tools": ["math.calculate"]}}, headers=h), 201)
    scripted = ScriptedTools()
    api.main.providers = ProviderRegistry({"groq": lambda: scripted})
    assert api.client.post("/autonomous/run", json={"agent": "calculator-agent", "task": "What is the yearly price?"}, headers=h).status_code == 200
    rec("tools", api.client.get("/tools", headers=h), 200)
    rec("tool", api.client.get("/tools/math.calculate", headers=h), 200)
    listed = rec("tool_invocations", api.client.get("/tool-invocations", headers=h), 200)
    rec("tool_invocation", api.client.get(f"/tool-invocations/{listed['items'][0]['id']}", headers=h), 200)
    rec("run_tool_invocations", api.client.get(f"/plan-runs/{run['id']}/tool-invocations", headers=h), 200)

    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(samples, handle)
