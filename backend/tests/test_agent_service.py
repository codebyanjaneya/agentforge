"""M2-A: custom agents and immutable versions (service layer on a migrated disposable SQLite database)."""

import copy
import threading
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import text

import agentplatform.service as svc
from agentplatform.schema import AgentDefinition, canonical, content_hash
from agentplatform.service import (
    AgentArchived,
    AgentNotFound,
    AgentService,
    LimitReached,
    SlugTaken,
    VersionConflict,
    VersionNotFound,
)
from authz.errors import ScopeError
from authz.workspaces import authorize_personal_scope
from tests.auth_support import auth_db  # noqa: F401  (fixture)
from tests.test_authz_memory import make_user

BASE = {
    "name": "Release Notes Writer",
    "description": "Turns a changelog into friendly release notes for customers.",
    "role": "Product marketer",
    "purpose": "Explain what changed and why it matters, without hype.",
    "instructions": "You write clear release notes from the changelog the user provides. Be accurate and concise.",
    "capabilities": ["release notes"],
    "model": {"provider": "groq", "name": "llama-3.3-70b-versatile"},
}


def definition(**overrides) -> AgentDefinition:
    data = copy.deepcopy(BASE)
    data.update(overrides)
    return AgentDefinition.model_validate(data)


@pytest.fixture
def world(auth_db):  # noqa: F811
    a, _ = make_user(auth_db, "a@example.com")
    b, _ = make_user(auth_db, "b@example.com")
    sf = auth_db.session_factory
    return dict(db=auth_db, svc=AgentService(sf), sa=authorize_personal_scope(sf, a), sb=authorize_personal_scope(sf, b), a=a, b=b)


def versions(db, agent_id):
    return db.rows("SELECT version, content_hash, change_note, restored_from_version, definition FROM custom_agent_versions "
                   "WHERE agent_id = :a ORDER BY version", a=agent_id.hex)


# ------------------------------------------------------------------------------------------------ create / read

def test_create_stores_an_owned_agent_with_an_immutable_first_version(world):
    view = world["svc"].create(world["sa"], "release-notes", definition(), change_note="first draft")
    assert (view.slug, view.status, view.current_version, view.latest_version) == ("release-notes", "active", 1, 1)
    assert view.template_id is None and view.archived_at is None
    assert view.version.number == 1 and view.version.change_note == "first draft" and view.version.restored_from_version is None
    assert view.version.content_hash == content_hash(definition())
    row = world["db"].rows("SELECT user_id, workspace_id FROM custom_agents")[0]
    assert (row[0], row[1]) == (world["sa"].user_id.hex, world["sa"].workspace_id.hex)  # taken from the scope
    assert world["db"].scalar("SELECT created_by FROM custom_agent_versions") == world["a"].user_id.hex
    assert world["svc"].get(world["sa"], view.id).definition == definition()


def test_create_records_template_provenance_only_as_a_pair(world):
    view = world["svc"].create(world["sa"], "from-template", definition(), template_id="sales-outreach", template_version=1)
    assert (view.template_id, view.template_version) == ("sales-outreach", 1)
    with pytest.raises(Exception):  # the database CHECK refuses half a pair
        world["svc"].create(world["sa"], "half-pair", definition(), template_id="sales-outreach")


def test_slugs_are_unique_per_workspace_valid_and_not_builtin(world):
    world["svc"].create(world["sa"], "my-agent", definition())
    with pytest.raises(SlugTaken):
        world["svc"].create(world["sa"], "my-agent", definition(name="Another one"))
    world["svc"].create(world["sb"], "my-agent", definition())  # another workspace may use the same slug
    for bad in ("ceo", "sales", "Bad Slug", "a", "agents", "x" * 41, "-x-", ""):
        with pytest.raises(ValueError):
            world["svc"].create(world["sa"], bad, definition())
    assert len(world["svc"].list_agents(world["sa"])) == 1  # nothing partial was stored


def test_agents_are_isolated_between_users(world):
    mine = world["svc"].create(world["sa"], "mine", definition())
    theirs = world["svc"].create(world["sb"], "theirs", definition(name="Theirs"))
    assert [v.slug for v in world["svc"].list_agents(world["sa"])] == ["mine"]
    assert [v.slug for v in world["svc"].list_agents(world["sb"])] == ["theirs"]
    # a foreign id behaves exactly like an unknown one, for every operation
    for call in (lambda: world["svc"].get(world["sa"], theirs.id), lambda: world["svc"].update(world["sa"], theirs.id, 1, definition(role="Hijack")),
                 lambda: world["svc"].archive(world["sa"], theirs.id), lambda: world["svc"].unarchive(world["sa"], theirs.id),
                 lambda: world["svc"].list_versions(world["sa"], theirs.id), lambda: world["svc"].get_version(world["sa"], theirs.id, 1),
                 lambda: world["svc"].rollback(world["sa"], theirs.id, 1, 1), lambda: world["svc"].get(world["sa"], uuid.uuid4())):
        with pytest.raises(AgentNotFound):
            call()
    assert world["svc"].get(world["sb"], theirs.id).definition.role == "Product marketer"  # untouched
    assert world["svc"].get(world["sb"], theirs.id).status == "active" and mine.id != theirs.id


def test_the_same_user_in_another_workspace_cannot_see_an_agent(world):
    from tests.test_authz_memory import team_workspace
    from authz.workspaces import authorize_memory_scope
    from authz.workspaces import authorize_memory_scope

    agent = world["svc"].create(world["sa"], "mine", definition())
    team = team_workspace(world["db"], world["a"])
    team_scope = authorize_memory_scope(world["db"].session_factory, world["a"], team)
    assert world["svc"].list_agents(team_scope) == []
    with pytest.raises(AgentNotFound):
        world["svc"].get(team_scope, agent.id)
    assert world["svc"].resolve(team_scope, "mine") is None


def test_agents_stay_private_to_their_creator_even_inside_a_shared_workspace(world):
    """Accepted M1-E model: ownership is the (user, workspace) pair, so two members of one team workspace do not see each
    other's agents (workspace-wide sharing would be a separate, explicit decision)."""
    from authz.workspaces import authorize_memory_scope
    from tests.test_authz_memory import team_workspace
    from authz.workspaces import authorize_memory_scope

    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    sf = world["db"].session_factory
    owner_scope = authorize_memory_scope(sf, world["a"], team)
    member_scope = authorize_memory_scope(sf, world["b"], team)
    agent = world["svc"].create(owner_scope, "team-agent", definition())
    assert [v.slug for v in world["svc"].list_agents(owner_scope)] == ["team-agent"]
    assert world["svc"].list_agents(member_scope) == []
    for call in (lambda: world["svc"].get(member_scope, agent.id), lambda: world["svc"].update(member_scope, agent.id, 1, definition(role="X role")),
                 lambda: world["svc"].archive(member_scope, agent.id), lambda: world["svc"].list_versions(member_scope, agent.id)):
        with pytest.raises(AgentNotFound):
            call()
    assert world["svc"].resolve(member_scope, "team-agent") is None
    # 0018: a slug is unique per (workspace, user). The member may own an agent with the same slug; it stays private to them and the owner's agent is untouched.
    mine = world["svc"].create(member_scope, "team-agent", definition(name="Member copy"))
    assert mine.id != agent.id and [v.id for v in world["svc"].list_agents(member_scope)] == [mine.id] and [v.id for v in world["svc"].list_agents(owner_scope)] == [agent.id]
    assert world["svc"].resolve(member_scope, "team-agent").version_id == mine.version.version_id and world["svc"].resolve(owner_scope, "team-agent").version_id == agent.version.version_id
    with pytest.raises(SlugTaken):  # still unique for ONE user
        world["svc"].create(member_scope, "team-agent", definition())


def test_every_operation_requires_a_genuine_scope(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    for bad in (None, "a@example.com", {"user_id": 1}, object()):
        for call in (lambda: world["svc"].create(bad, "x-agent", definition()), lambda: world["svc"].get(bad, agent.id),
                     lambda: world["svc"].list_agents(bad), lambda: world["svc"].update(bad, agent.id, 1, definition()),
                     lambda: world["svc"].archive(bad, agent.id), lambda: world["svc"].unarchive(bad, agent.id),
                     lambda: world["svc"].list_versions(bad, agent.id), lambda: world["svc"].get_version(bad, agent.id, 1),
                     lambda: world["svc"].rollback(bad, agent.id, 1, 1), lambda: world["svc"].resolve(bad, "mine")):
            with pytest.raises(ScopeError):
                call()


# ------------------------------------------------------------------------------------------------ versions

def test_update_creates_a_new_version_and_never_touches_old_ones(world):
    first = world["svc"].create(world["sa"], "mine", definition())
    snapshot = versions(world["db"], first.id)
    second = world["svc"].update(world["sa"], first.id, 1, definition(role="Marketing lead"), change_note="new role")
    assert (second.current_version, second.latest_version, second.version.number) == (2, 2, 2)
    assert second.definition.role == "Marketing lead" and second.version.change_note == "new role"
    rows = versions(world["db"], first.id)
    assert rows[0] == snapshot[0]  # version 1 is byte-for-byte what it was
    assert [r[0] for r in rows] == [1, 2]
    info, old = world["svc"].get_version(world["sa"], first.id, 1)
    assert old.role == "Product marketer" and info.number == 1
    assert [v.number for v in world["svc"].list_versions(world["sa"], first.id)] == [2, 1]  # newest first
    assert world["svc"].get(world["sa"], first.id).definition.role == "Marketing lead"


def test_stale_edits_are_refused_not_merged(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role two"))
    with pytest.raises(VersionConflict) as info:
        world["svc"].update(world["sa"], agent.id, 1, definition(role="Role from a stale editor"))
    assert info.value.latest_version == 2
    with pytest.raises(VersionConflict):
        world["svc"].update(world["sa"], agent.id, 5, definition(role="Future base"))
    assert [r[0] for r in versions(world["db"], agent.id)] == [1, 2]
    assert world["svc"].get(world["sa"], agent.id).definition.role == "Role two"
    world["svc"].update(world["sa"], agent.id, 2, definition(role="Role three"))  # a fresh base works


def test_saving_an_identical_definition_creates_no_version(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    same = world["svc"].update(world["sa"], agent.id, 1, definition(), change_note="no change")
    assert same.latest_version == 1 and world["db"].scalar("SELECT count(*) FROM custom_agent_versions") == 1
    changed = world["svc"].update(world["sa"], agent.id, 1, definition(role="Changed"))
    assert changed.latest_version == 2


def test_rollback_creates_a_new_version_copied_from_the_old_one(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role two"))
    world["svc"].update(world["sa"], agent.id, 2, definition(role="Role three"))
    before = versions(world["db"], agent.id)
    restored = world["svc"].rollback(world["sa"], agent.id, 1, 3)
    assert (restored.current_version, restored.latest_version) == (4, 4)
    assert restored.definition.role == "Product marketer"
    assert restored.version.restored_from_version == 1 and restored.version.change_note == "Restored version 1"
    after = versions(world["db"], agent.id)
    assert after[:3] == before  # versions 1-3 are unchanged: history is never rewritten
    assert after[3][1] == before[0][1] and after[3][3] == 1  # same content hash as version 1, marked as a restore
    assert [v.number for v in world["svc"].list_versions(world["sa"], agent.id)] == [4, 3, 2, 1]


def test_rollback_edge_cases(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role two"))
    with pytest.raises(VersionNotFound):
        world["svc"].rollback(world["sa"], agent.id, 9, 2)
    with pytest.raises(VersionConflict):
        world["svc"].rollback(world["sa"], agent.id, 1, 1)  # stale base
    same = world["svc"].rollback(world["sa"], agent.id, 2, 2)  # rolling back to what is current changes nothing
    assert same.latest_version == 2
    world["svc"].archive(world["sa"], agent.id)
    with pytest.raises(AgentArchived):
        world["svc"].rollback(world["sa"], agent.id, 1, 2)


def test_version_rows_are_immutable_in_the_database_itself(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    for statement in ("UPDATE custom_agent_versions SET change_note = 'tampered'",
                      "UPDATE custom_agent_versions SET definition = '{}'",
                      "DELETE FROM custom_agent_versions"):
        with pytest.raises(Exception) as info:
            world["db"].run(statement)
        assert "immutable" in str(info.value)
    assert world["db"].scalar("SELECT count(*) FROM custom_agent_versions") == 1
    assert world["svc"].get(world["sa"], agent.id).definition == definition()


def test_a_corrupted_stored_definition_is_never_served_as_valid(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    world["db"].run("DROP TRIGGER custom_agent_versions_no_update")  # only to simulate corruption below the application
    world["db"].run("UPDATE custom_agent_versions SET definition = '{\"name\": \"x\", \"unknown\": 1}'")
    with pytest.raises(ValidationError):
        world["svc"].get(world["sa"], agent.id)
    with pytest.raises(ValidationError):
        world["svc"].list_agents(world["sa"])


# ------------------------------------------------------------------------------------------------ archive

def test_archive_hides_without_destroying_and_blocks_edits_until_restored(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role two"))
    archived = world["svc"].archive(world["sa"], agent.id)
    assert archived.status == "archived" and archived.archived_at is not None
    assert world["svc"].archive(world["sa"], agent.id).status == "archived"  # idempotent
    assert world["svc"].list_agents(world["sa"]) == []
    assert [v.slug for v in world["svc"].list_agents(world["sa"], "archived")] == ["mine"]
    assert [v.slug for v in world["svc"].list_agents(world["sa"], "all")] == ["mine"]
    assert world["db"].scalar("SELECT count(*) FROM custom_agent_versions") == 2  # history kept
    with pytest.raises(AgentArchived):
        world["svc"].update(world["sa"], agent.id, 2, definition(role="Not allowed"))
    assert world["svc"].get(world["sa"], agent.id).definition.role == "Role two"  # still readable
    assert world["svc"].get_version(world["sa"], agent.id, 1)[1].role == "Product marketer"
    assert world["svc"].resolve(world["sa"], "mine") is None  # an archived agent is not resolvable
    with pytest.raises(SlugTaken):  # the slug stays reserved by the archived agent
        world["svc"].create(world["sa"], "mine", definition())
    restored = world["svc"].unarchive(world["sa"], agent.id)
    assert restored.status == "active" and restored.archived_at is None
    assert world["svc"].update(world["sa"], agent.id, 2, definition(role="Role three")).latest_version == 3
    with pytest.raises(ValueError):
        world["svc"].list_agents(world["sa"], "deleted")


# ------------------------------------------------------------------------------------------------ concurrency

def test_concurrent_edits_from_the_same_base_have_exactly_one_winner(world):
    agent = world["svc"].create(world["sa"], "mine", definition())
    outcomes, barrier = [], threading.Barrier(8)

    def edit(i):
        barrier.wait()
        try:
            world["svc"].update(world["sa"], agent.id, 1, definition(role=f"Role {i:02d} from editor"))
            outcomes.append("won")
        except VersionConflict as exc:
            outcomes.append(("conflict", exc.latest_version))

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("won") == 1 and len(outcomes) == 8
    assert all(o[0] == "conflict" and o[1] >= 2 for o in outcomes if o != "won")
    assert [r[0] for r in versions(world["db"], agent.id)] == [1, 2]  # no gaps, no duplicates, no phantom version
    assert world["db"].scalar("SELECT latest_version FROM custom_agents") == 2


def test_concurrent_creates_of_one_slug_have_exactly_one_winner(world):
    outcomes, barrier = [], threading.Barrier(6)

    def create(i):
        barrier.wait()
        try:
            world["svc"].create(world["sa"], "contested", definition(name=f"Contender {i}"))
            outcomes.append("won")
        except SlugTaken:
            outcomes.append("taken")

    threads = [threading.Thread(target=create, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert outcomes.count("won") == 1 and outcomes.count("taken") == 5
    assert world["db"].scalar("SELECT count(*) FROM custom_agents") == 1
    assert world["db"].scalar("SELECT count(*) FROM custom_agent_versions") == 1  # losers left no orphan version


def test_a_failed_save_leaves_no_trace(world, monkeypatch):
    agent = world["svc"].create(world["sa"], "mine", definition())
    monkeypatch.setattr(svc, "MAX_VERSIONS_PER_AGENT", 1)
    with pytest.raises(LimitReached):
        world["svc"].update(world["sa"], agent.id, 1, definition(role="Over the limit"))
    assert world["db"].scalar("SELECT count(*) FROM custom_agent_versions") == 1
    assert world["db"].scalar("SELECT latest_version FROM custom_agents") == 1


# ------------------------------------------------------------------------------------------------ limits

def test_safety_limits(world, monkeypatch):
    monkeypatch.setattr(svc, "MAX_AGENTS_PER_WORKSPACE", 2)
    world["svc"].create(world["sa"], "agent-one", definition())
    world["svc"].create(world["sa"], "agent-two", definition())
    with pytest.raises(LimitReached) as info:
        world["svc"].create(world["sa"], "agent-three", definition())
    assert info.value.what == "agents"
    world["svc"].create(world["sb"], "agent-one", definition())  # the limit is per workspace
    monkeypatch.setattr(svc, "MAX_VERSIONS_PER_AGENT", 3)
    agent = world["svc"].list_agents(world["sa"])[0]
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role 2 here"))
    world["svc"].update(world["sa"], agent.id, 2, definition(role="Role 3 here"))
    with pytest.raises(LimitReached) as info:
        world["svc"].update(world["sa"], agent.id, 3, definition(role="Role 4 here"))
    assert info.value.what == "versions"


# ------------------------------------------------------------------------------------------------ execution boundary

def test_resolve_is_the_read_only_integration_point_for_execution(world):
    # built-in agents always resolve to the existing modules and can never be shadowed by a custom agent
    for key in ("ceo", "sales", "marketing", "support", "research", "finance", "hiring"):
        resolved = world["svc"].resolve(world["sa"], key)
        assert (resolved.source, resolved.key, resolved.version_id, resolved.definition) == ("builtin", key, None, None)
    assert world["svc"].resolve(world["sa"], "unknown") is None
    agent = world["svc"].create(world["sa"], "mine", definition())
    first = world["svc"].resolve(world["sa"], "mine")
    assert (first.source, first.key, first.version) == ("custom", "mine", 1) and first.definition == definition()
    assert first.version_id == agent.version.version_id
    world["svc"].update(world["sa"], agent.id, 1, definition(role="Role two"))
    second = world["svc"].resolve(world["sa"], "mine")
    assert second.version == 2 and second.version_id != first.version_id
    # an execution that pinned version 1 can still read exactly what it ran
    assert world["svc"].get_version(world["sa"], agent.id, 1)[1] == definition()
    assert world["db"].scalar("SELECT definition FROM custom_agent_versions WHERE id = :i", i=first.version_id.hex)
    assert world["svc"].resolve(world["sb"], "mine") is None  # another user's agent does not resolve


def test_every_member_can_onboard_the_starter_workforce_concurrently_without_conflicts_or_cross_visibility(auth_db):  # noqa: F811
    """0018: eight members of one workspace each add the same three starter slugs at the same moment: nobody is refused, nobody gets duplicates, nobody sees another's agents."""
    from tests.test_authz_memory import team_workspace
    from authz.workspaces import authorize_memory_scope
    sf = auth_db.session_factory
    people = [make_user(auth_db, f"m{i}@example.com")[0] for i in range(8)]
    wid = team_workspace(auth_db, people[0], [(p, "member") for p in people[1:]])
    svc = AgentService(sf)
    scopes = [authorize_memory_scope(sf, p, wid) for p in people]
    slugs = ("unit-economics-analyst", "launch-planner", "launch-comms")
    errors, barrier = [], threading.Barrier(len(scopes) * len(slugs))

    def onboard(scope, slug):
        barrier.wait()
        try:
            svc.create(scope, slug, definition(name=slug))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=onboard, args=(sc, sl)) for sc in scopes for sl in slugs]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    for scope in scopes:
        assert sorted(v.slug for v in svc.list_agents(scope)) == sorted(slugs)  # exactly its own three
        assert all(svc.resolve(scope, slug) is not None for slug in slugs)
    assert auth_db.scalar("SELECT count(*) FROM custom_agents") == len(scopes) * len(slugs)


def test_the_agent_quota_is_per_user_not_per_workspace(world, monkeypatch):
    from agentplatform import service as svc_module
    from tests.test_authz_memory import team_workspace
    from authz.workspaces import authorize_memory_scope
    monkeypatch.setattr(svc_module, "MAX_AGENTS_PER_WORKSPACE", 2)
    sf = world["db"].session_factory
    team = team_workspace(world["db"], world["a"], [(world["b"], "member")])
    owner_scope, member_scope = authorize_memory_scope(sf, world["a"], team), authorize_memory_scope(sf, world["b"], team)
    world["svc"].create(owner_scope, "one-agent", definition())
    world["svc"].create(owner_scope, "two-agent", definition())
    with pytest.raises(LimitReached):
        world["svc"].create(owner_scope, "three-agent", definition())
    assert world["svc"].create(member_scope, "one-agent", definition()).slug == "one-agent"  # the owner reaching the cap does not lock the member out
