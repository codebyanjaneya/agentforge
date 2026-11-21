"""M2-A: the REST contract for templates and custom agents (real app, real verifier, disposable SQLite)."""

import copy
import uuid

from agentplatform.schema import BUILTIN_AGENT_KEYS
from tests.api_support import api, auth_db, main_module  # noqa: F401  (fixtures)

DEFINITION = {
    "name": "Release Notes Writer",
    "description": "Turns a changelog into friendly release notes for customers.",
    "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.",
    "instructions": "You write clear release notes from the changelog the user provides. Be accurate and concise.",
    "capabilities": ["release notes"],
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile", "temperature": 0.5},
}
A, B = ("user-a", "a@example.com"), ("user-b", "b@example.com")


def definition(**overrides):
    data = copy.deepcopy(DEFINITION)
    data.update(overrides)
    return data


def create(api, slug="release-notes", who=A, **overrides):
    return api.client.post("/agents", json={"slug": slug, "definition": definition(**overrides)}, headers=api.auth(*who))


def code(response):
    return response.json()["detail"]["code"]


# ------------------------------------------------------------------------------------------------ templates

def test_templates_are_listed_and_readable(api):
    r = api.client.get("/agent-templates", headers=api.auth(*A))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [t["id"] for t in items] == ["sales-outreach", "support-triage", "market-research-brief", "weekly-update-writer", "job-description-writer", "unit-economics-analyst", "launch-planner", "launch-comms"]
    assert set(items[0]) == {"id", "version", "name", "description", "role", "capabilities"}
    detail = api.client.get("/agent-templates/support-triage", headers=api.auth(*A)).json()
    assert detail["id"] == "support-triage" and detail["version"] == 1
    assert detail["definition"]["name"] == "Support Triage Assistant" and detail["definition"]["allowed_tools"] == []
    assert "cannot browse the web" in detail["definition"]["instructions"]
    r = api.client.get("/agent-templates/nope", headers=api.auth(*A))
    assert r.status_code == 404 and code(r) == "not_found"


def test_templates_cannot_be_modified_through_the_api(api):
    for method in ("put", "patch", "delete", "post"):
        r = getattr(api.client, method)("/agent-templates/sales-outreach", headers=api.auth(*A))
        assert r.status_code == 405
    before = api.client.get("/agent-templates/sales-outreach", headers=api.auth(*A)).json()
    api.client.post("/agent-templates/sales-outreach/instantiate", json={"name": "Renamed"}, headers=api.auth(*A))
    assert api.client.get("/agent-templates/sales-outreach", headers=api.auth(*A)).json() == before


def test_instantiating_a_template_creates_an_independent_user_owned_agent(api):
    r = api.client.post("/agent-templates/sales-outreach/instantiate", headers=api.auth(*A))
    assert r.status_code == 201
    agent = r.json()
    template = api.client.get("/agent-templates/sales-outreach", headers=api.auth(*A)).json()
    assert agent["slug"] == "sales-outreach" and agent["template_id"] == "sales-outreach" and agent["template_version"] == 1
    assert agent["definition"] == template["definition"] and agent["current_version"] == 1 and agent["status"] == "active"
    assert agent["version"]["change_note"] == "Created from template sales-outreach"
    # editing the instance never changes the template
    r = api.client.patch(f"/agents/{agent['id']}", json={"base_version": 1, "definition": definition()}, headers=api.auth(*A))
    assert r.status_code == 200 and r.json()["definition"]["name"] == "Release Notes Writer"
    assert api.client.get("/agent-templates/sales-outreach", headers=api.auth(*A)).json() == template
    # a second instantiation needs another slug; options are validated
    r = api.client.post("/agent-templates/sales-outreach/instantiate", headers=api.auth(*A))
    assert r.status_code == 409 and code(r) == "slug_taken"
    r = api.client.post("/agent-templates/sales-outreach/instantiate", json={"slug": "my-outreach", "name": "My Outreach"}, headers=api.auth(*A))
    assert r.status_code == 201 and r.json()["slug"] == "my-outreach" and r.json()["definition"]["name"] == "My Outreach"
    for body in ({"slug": "ceo"}, {"slug": "Bad Slug"}, {"name": "x"}, {"name": "x" * 81}, {"user_id": str(uuid.uuid4())}, {"workspace_id": "x"},
                 {"name": "api_key = abcd1234efgh5678"}):
        assert api.client.post("/agent-templates/sales-outreach/instantiate", json=body, headers=api.auth(*A)).status_code == 422, body
    assert api.client.post("/agent-templates/nope/instantiate", headers=api.auth(*A)).status_code == 404
    # another user instantiates the same template independently, with the same default slug
    assert api.client.post("/agent-templates/sales-outreach/instantiate", headers=api.auth(*B)).status_code == 201


# ------------------------------------------------------------------------------------------------ agents

def test_create_get_list_and_the_response_contract(api):
    r = create(api)
    assert r.status_code == 201
    agent = r.json()
    assert set(agent) == {"id", "slug", "name", "description", "role", "status", "current_version", "latest_version", "template_id",
                          "template_version", "created_at", "updated_at", "archived_at", "definition", "version"}
    assert set(agent["version"]) == {"version", "content_hash", "change_note", "restored_from_version", "created_at"}
    assert agent["definition"]["name"] == "Release Notes Writer" and agent["definition"]["model"]["temperature"] == 0.5
    assert "user_id" not in agent and "workspace_id" not in agent and "created_by" not in agent  # no internal identifiers leak
    got = api.client.get(f"/agents/{agent['id']}", headers=api.auth(*A)).json()
    assert got == agent
    listed = api.client.get("/agents", headers=api.auth(*A)).json()["items"]
    assert [i["slug"] for i in listed] == ["release-notes"] and "definition" not in listed[0]
    assert listed[0]["name"] == "Release Notes Writer"


def test_validation_failures_are_422_and_never_echo_submitted_values(api):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    r = api.client.post("/agents", json={"slug": "x-agent", "definition": definition(instructions=f"Use {secret} to log in to the service.")},
                        headers=api.auth(*A))
    assert r.status_code == 422
    assert secret not in r.text and '"input"' not in r.text and "credential" in r.text
    for body in (
        {"definition": definition()},  # no slug
        {"slug": "ceo", "definition": definition()}, {"slug": "Bad Slug", "definition": definition()},
        {"slug": "okay-slug", "definition": definition(allowed_tools=["shell"])},
        {"slug": "okay-slug", "definition": definition(model={"provider": "openai", "name": "gpt"})},
        {"slug": "okay-slug", "definition": {**definition(), "python": "import os"}},
        {"slug": "okay-slug", "definition": definition(), "user_id": str(uuid.uuid4())},
        {"slug": "okay-slug", "definition": definition(), "workspace_id": str(uuid.uuid4())},
        {"slug": "okay-slug", "definition": definition(), "change_note": "x" * 201},
        {"slug": "okay-slug", "definition": "not an object"}, [], "x",
    ):
        r = api.client.post("/agents", json=body, headers=api.auth(*A))
        assert r.status_code == 422, body
        assert '"input"' not in r.text
    assert api.client.get("/agents", headers=api.auth(*A)).json()["items"] == []  # nothing was stored
    # the sanitized 422 also applies to the existing routes
    r = api.client.post("/tasks/schedule", json={"agent": "ceo", "task": "t", "interval_minutes": "api_key=abcd1234efgh5678"}, headers=api.auth(*A))
    assert r.status_code == 422 and "abcd1234" not in r.text and '"input"' not in r.text


def test_duplicate_slug_is_409_and_isolated_per_user(api):
    assert create(api).status_code == 201
    r = create(api, role="Other role")
    assert r.status_code == 409 and code(r) == "slug_taken"
    assert create(api, who=B).status_code == 201  # the same slug for another user


def test_update_creates_versions_and_detects_conflicts(api):
    agent = create(api).json()
    url = f"/agents/{agent['id']}"
    r = api.client.patch(url, json={"base_version": 1, "definition": definition(role="Role two"), "change_note": "role"}, headers=api.auth(*A))
    assert r.status_code == 200
    updated = r.json()
    assert (updated["current_version"], updated["latest_version"]) == (2, 2) and updated["definition"]["role"] == "Role two"
    assert updated["version"]["change_note"] == "role"
    r = api.client.patch(url, json={"base_version": 1, "definition": definition(role="Stale editor")}, headers=api.auth(*A))
    assert r.status_code == 409 and r.json()["detail"] == {"code": "version_conflict", "latest_version": 2}
    r = api.client.patch(url, json={"base_version": 2, "definition": definition(role="Role two")}, headers=api.auth(*A))  # unchanged
    assert r.status_code == 200 and r.json()["latest_version"] == 2
    for body in ({"definition": definition()}, {"base_version": 0, "definition": definition()}, {"base_version": "1", "definition": definition()},
                 {"base_version": True, "definition": definition()}, {"base_version": 2}, {"base_version": 2, "definition": definition(), "slug": "new-slug"}):
        assert api.client.patch(url, json=body, headers=api.auth(*A)).status_code == 422, body
    assert api.client.get(f"{url}/versions", headers=api.auth(*A)).json()["items"][0]["version"] == 2


def test_version_history_and_rollback(api):
    agent = create(api).json()
    url = f"/agents/{agent['id']}"
    api.client.patch(url, json={"base_version": 1, "definition": definition(role="Role two")}, headers=api.auth(*A))
    api.client.patch(url, json={"base_version": 2, "definition": definition(role="Role three")}, headers=api.auth(*A))
    history = api.client.get(f"{url}/versions", headers=api.auth(*A)).json()["items"]
    assert [v["version"] for v in history] == [3, 2, 1] and all("definition" not in v for v in history)
    v1 = api.client.get(f"{url}/versions/1", headers=api.auth(*A)).json()
    assert v1["version"] == 1 and v1["definition"]["role"] == "Product marketer" and v1["content_hash"] == history[-1]["content_hash"]
    r = api.client.post(f"{url}/rollback", json={"to_version": 1, "base_version": 3}, headers=api.auth(*A))
    assert r.status_code == 200
    restored = r.json()
    assert restored["current_version"] == 4 and restored["definition"]["role"] == "Product marketer"
    assert restored["version"]["restored_from_version"] == 1
    assert api.client.get(f"{url}/versions/3", headers=api.auth(*A)).json()["definition"]["role"] == "Role three"  # history intact
    assert api.client.get(f"{url}/versions/1", headers=api.auth(*A)).json() == v1
    r = api.client.post(f"{url}/rollback", json={"to_version": 1, "base_version": 3}, headers=api.auth(*A))
    assert r.status_code == 409 and r.json()["detail"] == {"code": "version_conflict", "latest_version": 4}
    r = api.client.post(f"{url}/rollback", json={"to_version": 99, "base_version": 4}, headers=api.auth(*A))
    assert r.status_code == 404 and code(r) == "version_not_found"
    assert api.client.get(f"{url}/versions/99", headers=api.auth(*A)).status_code == 404
    for path in (f"{url}/versions/0", f"{url}/versions/x", "/agents/not-a-uuid", "/agents/not-a-uuid/versions"):
        assert api.client.get(path, headers=api.auth(*A)).status_code == 422, path
    for body in ({"to_version": 1}, {"base_version": 4}, {"to_version": 0, "base_version": 4}, {"to_version": 1, "base_version": 4, "x": 1}):
        assert api.client.post(f"{url}/rollback", json=body, headers=api.auth(*A)).status_code == 422, body


def test_archive_and_restore(api):
    agent = create(api).json()
    url = f"/agents/{agent['id']}"
    archived = api.client.post(f"{url}/archive", headers=api.auth(*A))
    assert archived.status_code == 200 and archived.json()["status"] == "archived" and archived.json()["archived_at"]
    assert api.client.get("/agents", headers=api.auth(*A)).json()["items"] == []
    assert [i["slug"] for i in api.client.get("/agents?status=archived", headers=api.auth(*A)).json()["items"]] == ["release-notes"]
    assert len(api.client.get("/agents?status=all", headers=api.auth(*A)).json()["items"]) == 1
    assert api.client.get("/agents?status=deleted", headers=api.auth(*A)).status_code == 422
    r = api.client.patch(url, json={"base_version": 1, "definition": definition(role="Blocked")}, headers=api.auth(*A))
    assert r.status_code == 409 and code(r) == "agent_archived"
    assert api.client.get(f"{url}/versions/1", headers=api.auth(*A)).status_code == 200  # history is still there
    assert api.client.post(f"{url}/unarchive", headers=api.auth(*A)).json()["status"] == "active"
    assert api.client.patch(url, json={"base_version": 1, "definition": definition(role="Allowed")}, headers=api.auth(*A)).status_code == 200


def test_users_cannot_see_or_change_each_others_agents(api):
    mine = create(api, who=A).json()
    theirs = create(api, slug="theirs-agent", who=B, name="Theirs").json()
    assert [i["slug"] for i in api.client.get("/agents", headers=api.auth(*A)).json()["items"]] == ["release-notes"]
    assert [i["slug"] for i in api.client.get("/agents", headers=api.auth(*B)).json()["items"]] == ["theirs-agent"]
    t = f"/agents/{theirs['id']}"
    attempts = [
        api.client.get(t, headers=api.auth(*A)),
        api.client.patch(t, json={"base_version": 1, "definition": definition(role="Hijack")}, headers=api.auth(*A)),
        api.client.post(f"{t}/archive", headers=api.auth(*A)), api.client.post(f"{t}/unarchive", headers=api.auth(*A)),
        api.client.get(f"{t}/versions", headers=api.auth(*A)), api.client.get(f"{t}/versions/1", headers=api.auth(*A)),
        api.client.post(f"{t}/rollback", json={"to_version": 1, "base_version": 1}, headers=api.auth(*A)),
        api.client.get(f"/agents/{uuid.uuid4()}", headers=api.auth(*A)),
    ]
    assert all(r.status_code == 404 and code(r) == "not_found" for r in attempts)  # foreign and unknown look identical
    assert api.client.get(t, headers=api.auth(*B)).json()["definition"]["role"] == "Product marketer"
    assert api.client.get(f"/agents/{mine['id']}", headers=api.auth(*A)).status_code == 200


def test_client_supplied_identity_cannot_change_ownership(api):
    # a forged email header/query/body cannot make B's token act as A, and ids in bodies are refused outright
    mine = create(api, who=A).json()
    forged = {**api.auth(*B), "X-User-Email": "a@example.com"}
    assert api.client.get(f"/agents/{mine['id']}", headers=forged, params={"user_email": "a@example.com"}).status_code == 404
    assert api.client.get("/agents", headers=forged).json()["items"] == []
    r = api.client.post("/agents", json={"slug": "forged-owner", "definition": definition(), "user_id": str(uuid.uuid4())}, headers=api.auth(*B))
    assert r.status_code == 422
    owner = api.db.scalar("SELECT user_id FROM custom_agents WHERE slug = 'release-notes'")
    a_id = api.db.scalar("SELECT u.id FROM users u JOIN auth_identities i ON i.user_id = u.id WHERE i.provider_subject = 'user-a'")
    assert owner == a_id  # owned by the token's user


def test_every_agent_route_needs_a_session_and_refuses_a_workspace_the_caller_is_not_a_member_of(api):
    agent = create(api).json()
    uid = agent["id"]
    routes = [("GET", "/agent-templates"), ("GET", "/agent-templates/sales-outreach"), ("POST", "/agent-templates/sales-outreach/instantiate"),
              ("POST", "/agents"), ("GET", "/agents"), ("GET", f"/agents/{uid}"), ("PATCH", f"/agents/{uid}"), ("POST", f"/agents/{uid}/archive"),
              ("POST", f"/agents/{uid}/unarchive"), ("GET", f"/agents/{uid}/versions"), ("GET", f"/agents/{uid}/versions/1"),
              ("POST", f"/agents/{uid}/rollback")]
    for method, path in routes:
        assert api.client.request(method, path).status_code == 401, (method, path)
        assert api.client.request(method, path, headers={"Authorization": "Bearer junk"}).status_code == 401
        r = api.client.request(method, path, headers={**api.auth(*A), "X-Workspace-Id": str(uuid.uuid4())})
        assert r.status_code == 403, (method, path)  # M6: a requested workspace is verified; a stranger's workspace is refused
    api.db.run("UPDATE users SET is_active = 0")
    for method, path in routes:
        assert api.client.request(method, path, headers=api.auth(*A)).status_code == 403, (method, path)


# ------------------------------------------------------------------------------------------------ built-in compatibility

def test_builtin_agents_are_unchanged_and_custom_agents_run_through_the_same_endpoint(api):
    orchestrator = api.main.orchestrator
    assert orchestrator.agent_names == tuple(BUILTIN_AGENT_KEYS)  # the registry still holds exactly the seven built-ins
    create(api, slug="custom-writer")
    h = api.auth(*A)
    for key in BUILTIN_AGENT_KEYS:  # the existing execution path still runs every built-in agent
        r = api.client.post("/autonomous/run", json={"agent": key, "task": "hello"}, headers=h)
        assert r.status_code == 200 and r.json()["agent"] == key and r.json()["agent_source"] == "builtin"
    r = api.client.post("/autonomous/run", json={"agent": "custom-writer", "task": "hello"}, headers=h)
    assert r.status_code == 200 and r.json()["agent_source"] == "custom" and r.json()["agent_version"] == 1  # M2-B closed the gap
    assert api.client.post("/agents", json={"slug": "ceo", "definition": definition()}, headers=h).status_code == 422  # cannot shadow a built-in
