"""M4-B: the tool REST contract through the real app (scripted model, no network): catalog, detail, invocation history, scoping and a real
tool call made by a custom agent through ``POST /autonomous/run`` and through a plan run."""

import copy
import json

import pytest

from agentplatform.providers import ProviderRegistry, ProviderResponse, ToolCall
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)
from tests.test_plan_runs_api import A, AGENT, B, make_plan, papi, start, task, wait  # noqa: F401  (papi is a fixture)


class Scripted:
    name = "groq"

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    async def complete(self, request):
        self.requests.append(request)
        return self.responses.pop(0) if self.responses else ProviderResponse(text="done", model="m")


def ask(name, args, id="c1"):
    return ProviderResponse(text="", model="m", tool_calls=(ToolCall(id, name, json.dumps(args)),))


def use(api, *responses):
    provider = Scripted(*responses)
    api.main.providers = ProviderRegistry({"groq": lambda: provider})
    return provider


def agent_with_tools(api, tools=("math.calculate",), slug="analyst", who=A):
    r = api.client.post("/agents", json={"slug": slug, "definition": {**copy.deepcopy(AGENT), "allowed_tools": list(tools)}}, headers=api.auth(*who))
    assert r.status_code == 201, r.text
    return r.json()


def get(api, path, who=A, **kw):
    return api.client.get(path, headers=api.auth(*who), **kw)


def test_the_catalog_lists_the_real_tools_with_schemas_policy_and_the_callers_agents(papi):
    api = papi
    assert api.client.get("/tools").status_code == 401 and api.client.get("/tools/math.calculate").status_code == 401
    body = get(api, "/tools").json()
    assert [t["id"] for t in body["items"]] == ["math.calculate", "data.summarize_csv", "finance.unit_economics", "calendar.public_holidays", "time.date_math", "text.analyze"]
    assert body["policy"] == {"max_risk": "read", "side_effects": "none", "approvals": "per_call"}
    calc = body["items"][0]
    assert calc["latest"]["ref"] == "math.calculate@1" and calc["latest"]["executable"] is True and calc["latest"]["policy"] == "allow" and calc["usage"] == {"calls": 0, "last_used": None} and calc["agents"] == []
    created = agent_with_tools(api, ("math.calculate", "time.date_math"))
    assert created["definition"]["allowed_tools"] == ["math.calculate@1", "time.date_math@1"]  # pinned on save
    after = {t["id"]: t for t in get(api, "/tools").json()["items"]}
    assert [a["slug"] for a in after["math.calculate"]["agents"]] == ["analyst"] and after["math.calculate"]["agents"][0]["version"] == 1 and after["text.analyze"]["agents"] == []
    assert get(api, "/tools", who=B).json()["items"][0]["agents"] == []  # another user's agents are never listed
    detail = get(api, "/tools/finance.unit_economics").json()
    version = detail["versions"][0]
    assert version["input_schema"]["properties"]["monthly_price"] and version["output_schema"]["properties"]["ltv"] and version["limitations"] and version["example"] and version["description"]
    assert version["usage"] == {"calls": 0, "last_used": None} and detail["recent_invocations"] == []
    assert get(api, "/tools/nope.nope").status_code == 404


def test_an_agent_cannot_be_granted_a_tool_that_does_not_exist_or_is_not_executable(papi):
    api = papi
    for bad in (["web.search"], ["math.calculate@9"], ["math.calculate", "math.calculate@1"], ["rm -rf /"], [f"t{i}.tool" for i in range(9)]):
        r = api.client.post("/agents", json={"slug": "bad-tools", "definition": {**copy.deepcopy(AGENT), "allowed_tools": bad}}, headers=api.auth(*A))
        assert r.status_code == 422, bad
        assert "rm -rf" not in r.text  # a rejected value is never echoed
    assert api.db.scalar("SELECT count(*) FROM custom_agents") == 0


def test_a_real_tool_call_through_the_rest_execution_path_is_recorded_and_shown(papi):
    api = papi
    agent_with_tools(api, ("math.calculate",))
    provider = use(api, ask("math__calculate__v1", {"expression": "1200 * 0.85 / 12"}), ProviderResponse(text="Monthly cost: 85.", model="m"))
    r = api.client.post("/autonomous/run", json={"agent": "analyst", "task": "What is the monthly cost?"}, headers=api.auth(*A))
    assert r.status_code == 200 and r.json()["status"] == "succeeded" and "85" in json.dumps(r.json())
    assert [t.name for t in provider.requests[0].tools] == ["math__calculate__v1"]
    page = get(api, "/tool-invocations").json()
    assert len(page["items"]) == 1 and page["next_cursor"] is None
    item = page["items"][0]
    assert (item["tool"], item["status"], item["agent"], item["run_id"], item["task"]) == ({"id": "math.calculate", "version": 1}, "succeeded", "analyst", None, None)
    assert item["execution_id"] == r.json()["execution_id"] and item["duration_ms"] is not None and "1200" in item["input_preview"] and "85" in item["output_preview"] and "input" not in item
    full = get(api, f"/tool-invocations/{item['id']}").json()
    assert full["input"] == {"expression": "1200 * 0.85 / 12"} and full["output"]["value"] == 85.0 and len(full["args_hash"]) == 64 and full["error"] is None
    usage = {t["id"]: t for t in get(api, "/tools").json()["items"]}["math.calculate"]["usage"]
    assert usage["calls"] == 1 and usage["last_used"]
    assert get(api, "/tools/math.calculate").json()["recent_invocations"][0]["id"] == item["id"]


def test_invocations_are_scoped_filtered_and_paged(papi):
    api = papi
    agent_with_tools(api, ("math.calculate",))
    use(api, *[x for i in range(3) for x in (ask("math__calculate__v1", {"expression": f"{i}+1"}), ProviderResponse(text=f"r{i}", model="m"))])
    for i in range(3):
        assert api.client.post("/autonomous/run", json={"agent": "analyst", "task": f"t{i}"}, headers=api.auth(*A)).status_code == 200
    first = get(api, "/tool-invocations?limit=2").json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = get(api, f"/tool-invocations?limit=2&cursor={first['next_cursor']}").json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None and len({i["id"] for i in first["items"] + second["items"]}) == 3
    assert get(api, "/tool-invocations?status=rejected").json()["items"] == [] and len(get(api, "/tool-invocations?tool_id=math.calculate").json()["items"]) == 3
    assert get(api, "/tool-invocations?agent=nobody").json()["items"] == []
    assert get(api, "/tool-invocations?cursor=garbage").status_code == 400 and get(api, "/tool-invocations?limit=0").status_code == 422 and get(api, "/tool-invocations?limit=101").status_code == 422
    one = first["items"][0]["id"]
    assert get(api, f"/tool-invocations/{one}", who=B).status_code == 404 and get(api, "/tool-invocations", who=B).json()["items"] == []  # foreign == unknown
    assert get(api, "/tool-invocations/00000000-0000-4000-8000-000000000001").status_code == 404 and api.client.get("/tool-invocations").status_code == 401
    assert api.client.get(f"/tool-invocations/{one}").status_code == 401


def test_a_refused_call_is_visible_with_its_fixed_reason(papi):
    api = papi
    agent_with_tools(api, ("math.calculate",))
    use(api, ask("text__analyze__v1", {"text": "this agent was never granted this tool"}), ProviderResponse(text="I could not analyse it.", model="m"))
    assert api.client.post("/autonomous/run", json={"agent": "analyst", "task": "analyse"}, headers=api.auth(*A)).status_code == 200
    item = get(api, "/tool-invocations").json()["items"][0]
    assert (item["status"], item["error"]["code"], item["tool"]["id"]) == ("rejected", "not_permitted", "text.analyze") and item["error"]["message"] == "This agent is not permitted to use that tool."
    assert {t["id"]: t for t in get(api, "/tools").json()["items"]}["text.analyze"]["usage"]["calls"] == 0  # a refusal is not usage


def test_a_plan_task_that_uses_a_tool_shows_it_under_its_run(papi):
    api = papi
    agent_with_tools(api, ("math.calculate",))
    pid = make_plan(api, [task("a", "analyst")])
    use(api, ask("math__calculate__v1", {"expression": "7 * 6"}), ProviderResponse(text="The answer is 42.", model="m"))
    run = wait(api, start(api, pid).json()["id"])
    assert run["status"] == "succeeded"
    listed = get(api, f"/plan-runs/{run['id']}/tool-invocations").json()
    assert listed["truncated"] is False and [(i["task"], i["tool"]["id"], i["status"], i["run_id"]) for i in listed["items"]] == [("a", "math.calculate", "succeeded", run["id"])]
    assert get(api, f"/plan-runs/{run['id']}/tool-invocations", who=B).status_code == 404 and get(api, "/plan-runs/00000000-0000-4000-8000-000000000001/tool-invocations").status_code == 404
    assert get(api, f"/tool-invocations?run_id={run['id']}").json()["items"][0]["task"] == "a"
    assert api.client.get(f"/plan-runs/{run['id']}/tool-invocations").status_code == 401
