"""M1-E: MemoryScope and ownership-scoped memory (repository, short-term, agents, orchestrator, improvement).

Backend modules are imported at module top on purpose: authz is excluded from the import purge, so error and
scope classes must be the same objects the code under test raises and checks."""

import asyncio
import dataclasses
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from auth.principal import Principal
from authz.errors import AuthorizationError, ScopeError
from authz.scope import MemoryScope, _issue_scope, require_scope
from authz.workspaces import authorize_memory_scope
from memory.models import AgentMemory, User, Workspace, WorkspaceMembership
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.auth_support import auth_db  # noqa: F401  (fixture)

VOICE_EMAIL = "voice-call@agentforge.local"


def make_user(db, email="u@example.com"):
    uid, wid = uuid.uuid4(), uuid.uuid4()
    with db.session_factory() as s, s.begin():
        s.add(User(id=uid, email=email, email_normalized=email.casefold(), is_active=True))
        s.flush()
        s.add(Workspace(id=wid, name="Personal", is_personal=True, personal_for_user_id=uid))
        s.flush()
        s.add(WorkspaceMembership(workspace_id=wid, user_id=uid, role="owner"))
    principal = Principal(user_id=uid, provider="google", provider_subject=f"s-{uid}", email=email, is_active=True)
    return principal, wid


def team_workspace(db, owner, members=()):
    wid = uuid.uuid4()
    with db.session_factory() as s, s.begin():
        s.add(Workspace(id=wid, name="Team", is_personal=False))
        s.flush()
        s.add(WorkspaceMembership(workspace_id=wid, user_id=owner.user_id, role="owner"))
        for p, role in members:
            s.add(WorkspaceMembership(workspace_id=wid, user_id=p.user_id, role=role))
    return wid


@pytest.fixture
def world(auth_db):  # noqa: F811
    a, a_ws = make_user(auth_db, "a@example.com")
    b, b_ws = make_user(auth_db, "b@example.com")
    sf = auth_db.session_factory
    return dict(db=auth_db, repo=MemoryRepository(sf), a=a, b=b, a_ws=a_ws, b_ws=b_ws,
                sa=authorize_memory_scope(sf, a, a_ws), sb=authorize_memory_scope(sf, b, b_ws))


def legacy_row(db, email, summary="legacy", agent="ceo", user_id=None, workspace_id=None):
    with db.session_factory() as s, s.begin():
        s.add(AgentMemory(agent_name=agent, user_email=email, summary=summary, created_at=datetime.utcnow(),
                          user_id=user_id, workspace_id=workspace_id))


# ---------------------------------------------------------------- MemoryScope construction

def test_scope_cannot_be_constructed_directly():
    for args in [(), (uuid.uuid4(), uuid.uuid4()), ("a@example.com",), (None, None)]:
        with pytest.raises(ScopeError):
            MemoryScope(*args)
    with pytest.raises(ScopeError):
        MemoryScope(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())


def test_scope_issue_requires_private_proof_and_real_ids():
    u, w = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(ScopeError):
        _issue_scope(u, w)
    from authz.scope import _ISSUE
    for bad in [(None, w), (u, None), ("x", w), (u, str(w)), (uuid.UUID(int=0), w), (u, uuid.UUID(int=0))]:
        with pytest.raises(ScopeError):
            _issue_scope(*bad, _proof=_ISSUE)


def test_scope_is_immutable_and_not_replaceable(world):
    s = world["sa"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.user_id = uuid.uuid4()
    with pytest.raises(ScopeError):
        dataclasses.replace(s, workspace_id=uuid.uuid4())
    assert "@" not in repr(s)


def test_require_scope_rejects_everything_else():
    for bad in [None, "a@example.com", {"user_id": uuid.uuid4()}, (uuid.uuid4(), uuid.uuid4()), object(), 5]:
        with pytest.raises(ScopeError):
            require_scope(bad)


def test_only_authz_issues_scopes():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hits = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
            if "venv" not in p.parts and "tests" not in p.parts and "_issue_scope" in p.read_text(encoding="utf-8")]
    assert sorted(hits) == ["authz/scope.py", "authz/workspaces.py"]


def test_scope_comes_from_verified_membership_only(world):
    db, a, b = world["db"], world["a"], world["b"]
    assert world["sa"].user_id == a.user_id and world["sa"].workspace_id == world["a_ws"]
    with pytest.raises(AuthorizationError):  # b is not a member of a's workspace
        authorize_memory_scope(db.session_factory, b, world["a_ws"])


# ---------------------------------------------------------------- repository isolation

def test_save_persists_scope_ids_not_email(world):
    rid = world["repo"].save(world["sa"], "ceo", "hello")
    with world["db"].session_factory() as s:
        row = s.get(AgentMemory, rid)
    assert (row.user_id, row.workspace_id, row.user_email) == (world["a"].user_id, world["a_ws"], None)


def test_users_never_see_each_other(world):
    repo = world["repo"]
    repo.save(world["sa"], "ceo", "A-secret")
    repo.save(world["sb"], "ceo", "B-secret")
    assert [m["summary"] for m in repo.get(world["sa"], "ceo")] == ["A-secret"]
    assert [m["summary"] for m in repo.get_history(world["sb"])] == ["B-secret"]
    assert all("user_email" not in m for m in repo.get_history(world["sa"]))


def test_same_user_other_workspace_is_separate(world):
    db, a = world["db"], world["a"]
    t = team_workspace(db, a)
    st = authorize_memory_scope(db.session_factory, a, t)
    world["repo"].save(world["sa"], "ceo", "personal")
    world["repo"].save(st, "ceo", "team")
    assert [m["summary"] for m in world["repo"].get_history(world["sa"])] == ["personal"]
    assert [m["summary"] for m in world["repo"].get_history(st)] == ["team"]


def test_update_and_delete_are_owner_and_id_constrained(world):
    repo = world["repo"]
    mine = repo.save(world["sa"], "ceo", "mine")
    theirs = repo.save(world["sb"], "ceo", "theirs")
    assert repo.update_summary(world["sa"], theirs, "pwned") is False
    assert repo.delete(world["sa"], theirs) is False
    assert repo.get_history(world["sb"])[0]["summary"] == "theirs"
    assert repo.update_summary(world["sa"], mine, "edited") is True
    assert repo.get_history(world["sa"])[0]["summary"] == "edited"
    assert repo.delete(world["sa"], mine) is True
    assert repo.get_history(world["sa"]) == []
    assert repo.update_summary(world["sa"], 10**6, "x") is False


def test_other_workspace_row_of_same_user_not_touchable(world):
    db, a, repo = world["db"], world["a"], world["repo"]
    t = team_workspace(db, a)
    st = authorize_memory_scope(db.session_factory, a, t)
    rid = repo.save(st, "ceo", "team")
    assert repo.update_summary(world["sa"], rid, "x") is False and repo.delete(world["sa"], rid) is False


def test_get_limit_and_stable_order(world):
    repo = world["repo"]
    base = datetime(2026, 1, 1)
    with world["db"].session_factory() as s, s.begin():
        for i in range(12):
            s.add(AgentMemory(agent_name="ceo", summary=f"m{i}", created_at=base, user_id=world["a"].user_id,
                              workspace_id=world["a_ws"]))
    got = repo.get(world["sa"], "ceo")
    assert len(got) == 10 and [m["summary"] for m in got][:2] == ["m11", "m10"]  # same timestamp: id breaks the tie
    assert len(repo.get_history(world["sa"])) == 12


@pytest.mark.parametrize("bad", [None, "a@example.com", object()])
def test_every_operation_fails_closed_without_scope(world, bad):
    repo = world["repo"]
    rid = repo.save(world["sa"], "ceo", "x")
    for call in (lambda: repo.save(bad, "ceo", "x"), lambda: repo.get(bad, "ceo"), lambda: repo.get_history(bad),
                 lambda: repo.update_summary(bad, rid, "y"), lambda: repo.delete(bad, rid)):
        with pytest.raises(ScopeError):
            call()
    assert repo.get_history(world["sa"])[0]["summary"] == "x"  # nothing changed


# ---------------------------------------------------------------- legacy quarantine and voice

def test_legacy_rows_are_quarantined(world):
    db, repo, a = world["db"], world["repo"], world["a"]
    legacy_row(db, a.email, "legacy-by-email")                      # email equals a real user's email
    legacy_row(db, None, "legacy-no-email")
    legacy_row(db, a.email, "half-owned-user-only", user_id=a.user_id)       # one id only
    legacy_row(db, a.email, "half-owned-ws-only", workspace_id=world["a_ws"])
    repo.save(world["sa"], "ceo", "owned")
    assert [m["summary"] for m in repo.get_history(world["sa"])] == ["owned"]
    assert [m["summary"] for m in repo.get(world["sa"], "ceo")] == ["owned"]
    ids = [r[0] for r in db.rows("SELECT id FROM agent_memories WHERE summary LIKE '%legacy%' OR summary LIKE 'half%'")]
    for i in ids:
        assert repo.update_summary(world["sa"], i, "x") is False and repo.delete(world["sa"], i) is False
    assert db.scalar("SELECT count(*) FROM agent_memories WHERE summary='x'") == 0
    assert db.scalar("SELECT count(*) FROM agent_memories WHERE user_email IS NOT NULL AND user_id IS NULL") >= 2


def test_legacy_rows_are_not_backfilled_by_use(world):
    legacy_row(world["db"], world["a"].email, "legacy")
    world["repo"].get_history(world["sa"])
    world["repo"].save(world["sa"], "ceo", "x")
    assert world["db"].scalar("SELECT count(*) FROM agent_memories WHERE user_id IS NULL AND workspace_id IS NULL") == 1


def test_voice_rows_are_separate_and_not_exposed(world):
    legacy_row(world["db"], VOICE_EMAIL, "voice transcript")
    for scope in (world["sa"], world["sb"]):
        assert world["repo"].get_history(scope) == [] and world["repo"].get(scope, "ceo") == []
    assert world["db"].scalar("SELECT count(*) FROM agent_memories WHERE user_email=:e AND user_id IS NULL", e=VOICE_EMAIL) == 1


def test_a_call_sid_is_not_identity(world):
    for sid in ("CA" + "0" * 32, str(uuid.uuid4())):
        with pytest.raises(ScopeError):
            world["repo"].get_history(sid)


# ---------------------------------------------------------------- short-term memory

class FakeRedis:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


def test_short_term_is_scope_keyed_and_isolated(world):
    r = FakeRedis()
    st = ShortTermMemory(client=r)
    st.save(world["sa"], "ceo", "user", "A")
    st.save(world["sb"], "ceo", "user", "B")
    assert st.get(world["sa"], "ceo") == [{"role": "user", "content": "A"}]
    assert st.get(world["sb"], "ceo") == [{"role": "user", "content": "B"}]
    assert all("@" not in k and str(world["a"].user_id) in k or str(world["b"].user_id) in k for k in r.d)
    st.clear(world["sa"], "ceo")
    assert st.get(world["sa"], "ceo") == [] and st.get(world["sb"], "ceo")


def test_short_term_fails_closed_without_scope():
    r = FakeRedis()
    st = ShortTermMemory(client=r)
    for call in (lambda: st.save("a@example.com", "ceo", "user", "x"), lambda: st.get(None, "ceo"),
                 lambda: st.clear("a@example.com", "ceo")):
        with pytest.raises(ScopeError):
            call()
    assert r.d == {}


# ---------------------------------------------------------------- agents, orchestrator, executor, improvement

AGENTS = ["ceo", "sales", "marketing", "support", "research", "finance", "hiring"]


class FakeLLM:
    def __init__(self, reply="reply"):
        self.calls = 0
        self.reply = reply
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls += 1
        msg = type("M", (), {"content": self.reply})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


@pytest.fixture
def wired(world, import_fresh):
    orch_mod = import_fresh("orchestrator")
    import importlib
    llm = FakeLLM()
    redis = FakeRedis()
    mods = {}
    for name in AGENTS:
        m = importlib.import_module(f"agents.{name}")
        m.client = llm
        m.short_term = ShortTermMemory(client=redis)
        m.long_term = MemoryRepository(world["db"].session_factory)
        mods[name] = m
    return dict(world, orch=orch_mod.AgentOrchestrator(), mods=mods, llm=llm, redis=redis, orch_mod=orch_mod)


@pytest.mark.parametrize("name", AGENTS)
def test_every_agent_requires_scope_before_the_llm(wired, name):
    for bad in ("a@example.com", None):
        with pytest.raises(ScopeError):
            asyncio.run(wired["mods"][name].run("task", bad))
    assert wired["llm"].calls == 0 and wired["redis"].d == {}
    assert wired["db"].scalar("SELECT count(*) FROM agent_memories") == 0


@pytest.mark.parametrize("name", AGENTS)
def test_every_agent_persists_under_the_scope(wired, name):
    assert asyncio.run(wired["mods"][name].run("task", wired["sa"])) == "reply"
    assert [m["agent_name"] for m in wired["repo"].get_history(wired["sa"])] == [name]
    assert wired["repo"].get_history(wired["sb"]) == []
    assert wired["db"].scalar("SELECT count(*) FROM agent_memories WHERE user_email IS NOT NULL") == 0


def test_orchestrator_fails_closed_and_passes_scope(wired):
    o = wired["orch"]
    for bad in ("a@example.com", None):
        with pytest.raises(ScopeError):
            asyncio.run(o.run_agent("ceo", "t", bad))
        with pytest.raises(ScopeError):
            asyncio.run(o.run_with_delegation("ceo", "t", bad))
    assert wired["llm"].calls == 0
    wired["llm"].reply = "Ask the Sales Agent to call leads.\n\nok"
    out = asyncio.run(o.run_with_delegation("ceo", "plan", wired["sa"]))
    assert [d["agent"] for d in out["delegations"]] == ["sales"]
    assert {m["agent_name"] for m in wired["repo"].get_history(wired["sa"])} == {"ceo", "sales"}
    assert wired["repo"].get_history(wired["sb"]) == []


def test_autonomous_executor_requires_scope(wired):
    import autonomous
    ex = autonomous.AutonomousExecutor(wired["orch"])
    with pytest.raises(ScopeError):
        asyncio.run(ex.execute("ceo", "t", "a@example.com"))
    assert wired["llm"].calls == 0
    assert asyncio.run(ex.execute("ceo", "t", wired["sa"]))["agent"] == "ceo"


def test_improvement_is_scoped_and_leaks_no_email(wired, monkeypatch):
    from improvement import analyzer as an, self_improver as si
    llm = FakeLLM(json.dumps({"strengths": ["s"], "weaknesses": [], "improvements": [], "updated_strategy": "u"}))
    monkeypatch.setattr(an, "client", llm)
    redis = FakeRedis()
    imp = si.SelfImprover(client=redis)
    imp.analyzer.short_term = ShortTermMemory(client=wired["redis"])
    imp.analyzer.long_term = MemoryRepository(wired["db"].session_factory)
    for bad in ("a@example.com", None):
        with pytest.raises(ScopeError):
            asyncio.run(imp.improve("ceo", bad))
        with pytest.raises(ScopeError):
            imp.get_improvements("ceo", bad)
        with pytest.raises(ScopeError):
            asyncio.run(imp.analyzer.analyze("ceo", bad))
    assert redis.d == {} and llm.calls == 0
    empty = asyncio.run(imp.improve("ceo", wired["sa"]))
    assert empty["has_history"] is False and "user_email" not in empty
    asyncio.run(wired["mods"]["ceo"].run("task", wired["sa"]))
    rep = asyncio.run(imp.improve("ceo", wired["sa"]))
    assert rep["has_history"] is True and "user_email" not in rep and llm.calls == 1
    assert imp.get_improvements("ceo", wired["sb"])["has_history"] is False  # B never sees A's report
    assert imp.get_improvements("ceo", wired["sa"])["strengths"] == ["s"]
    assert all("@" not in k for k in redis.d)


# ---------------------------------------------------------------- every legacy caller fails closed

def test_telegram_task_with_email_fails_closed_without_running_an_agent(import_fresh):
    tb = import_fresh("telegram_bot")
    replies = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    class Upd:
        message = Msg()

    class Bot:
        async def send_chat_action(self, **kw):
            pass

    class Ctx:
        bot = Bot()

    calls = []
    tb.orchestrator.run_agent = lambda *a, **k: calls.append(a)
    asyncio.run(tb._process_task(Upd(), Ctx(), 1, "a@example.com", "ceo", "task"))
    assert calls == []  # the orchestrator rejected the email before any agent ran
    assert replies and "error" in replies[-1].lower()


def test_voice_call_fails_closed_and_voice_email_is_not_a_scope(import_fresh, monkeypatch):
    vh = import_fresh("voice_handler")

    class Resp:
        content = b"x"

        def raise_for_status(self):
            pass

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return Resp()

    monkeypatch.setattr(vh.httpx, "AsyncClient", lambda **k: Http())

    async def fake_transcribe(*a, **k):
        return "hello"

    monkeypatch.setattr(vh, "transcribe_audio", fake_transcribe)
    from orchestrator import AgentOrchestrator
    with pytest.raises(ScopeError):  # voice memory stays separate and is NOT migrated; webhook security is S2
        asyncio.run(vh.process_recording("https://x/rec", AgentOrchestrator()))
