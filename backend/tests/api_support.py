"""Shared fixture for the M1-F API tests: the real ``main`` app on a migrated disposable SQLite database, real
verifier with an ephemeral P-256 key, fake Redis, and fake agents (no LLM, no network).

Backend modules are imported at module top where classes cross module boundaries (see test_authz_memory).
"""

import json
import time
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from authz.scope import MemoryScope  # noqa: F401  (keeps the original class loaded)
from memory.models import AgentMemory
from memory.repository import MemoryRepository
from memory.short_term import ShortTermMemory
from tests.auth_support import auth_config, auth_db, make_token, new_keypair  # noqa: F401  (fixture)

ORIGIN = "http://localhost:3000"


class FakeRedis:
    def __init__(self):
        self.d, self.h = {}, {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hdel(self, key, field):
        return 1 if self.h.get(key, {}).pop(field, None) is not None else 0

    def keys_used(self):
        return set(self.d) | set(self.h)


class FakeLLM:
    def __init__(self):
        self.calls = 0
        self.chat = self
        self.completions = self

    async def create(self, **kw):
        self.calls += 1
        body = json.dumps({"strengths": ["s"], "weaknesses": [], "improvements": [], "updated_strategy": "u"})
        msg = type("M", (), {"content": body})
        return type("R", (), {"choices": [type("C", (), {"message": msg})]})


class FakeProvider:
    """A deterministic provider for the execution tests: records every request, can fail, delay or wait on a gate."""

    name = "groq"

    def __init__(self):
        self.requests = []
        self.error = None
        self.delay = 0.0
        self.gate = None  # a threading.Event the call waits for (polled, so it works from the app's event loop thread)
        self.started = None  # a threading.Event set when a call begins
        self.reply = lambda request: f"custom reply: {request.messages[-1].content}"
        self.usage = None  # a Usage to report with every reply (None: the provider reports nothing)

    async def complete(self, request):
        import asyncio

        from agentplatform.providers import ProviderResponse

        self.requests.append(request)
        if self.started is not None:
            self.started.set()
        if self.gate is not None:
            while not self.gate.is_set():
                await asyncio.sleep(0.01)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        extra = {} if self.usage is None else {"usage": self.usage}
        return ProviderResponse(text=self.reply(request), model=request.model, **extra)


class Api:
    def __init__(self, main, db, private, public, redis, llm):
        self.main, self.db, self.private, self.public, self.redis, self.llm = main, db, private, public, redis, llm
        self.client = TestClient(main.app, raise_server_exceptions=False)
        self.seen_scopes = []

    def token(self, sub="user-a", email="a@example.com", **claims):
        return make_token(self.private, claims={"sub": sub, "email": email, **claims})

    def auth(self, sub="user-a", email="a@example.com", **claims):
        return {"Authorization": "Bearer " + self.token(sub, email, **claims)}

    def provision(self, sub="user-a", email="a@example.com"):
        """First authenticated call provisions the user; returns (user_id, personal_workspace_id)."""
        assert self.client.get("/tasks", headers=self.auth(sub, email)).status_code == 200
        uid = self.db.scalar("SELECT u.id FROM users u JOIN auth_identities i ON i.user_id = u.id WHERE i.provider_subject = :s", s=sub)
        wid = self.db.scalar("SELECT id FROM workspaces WHERE personal_for_user_id = :u", u=uid)
        return uuid.UUID(uid), uuid.UUID(wid)

    def add_memory(self, user_id, workspace_id, summary, created_at, agent="ceo"):
        with self.db.session_factory() as s, s.begin():
            s.add(AgentMemory(agent_name=agent, summary=summary, created_at=created_at, user_id=user_id, workspace_id=workspace_id))


def _purge():
    import sys
    from pathlib import Path

    backend = Path(__file__).resolve().parent.parent
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        p = Path(path).resolve()
        if (backend in p.parents and "venv" not in p.parts and (backend / "tests") not in p.parents
                and p.name not in ("config.py", "app_factory.py", "schema_guard.py")
                and (backend / "authz") not in p.parents and (backend / "auth") not in p.parents):
            sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def main_module(tmp_path_factory):
    """Import ``main`` once per test module (importing it is slow); every test rewires its own fakes."""
    import importlib

    from config import reset_settings_cache

    mp = pytest.MonkeyPatch()
    for key, value in {"APP_ENV": "test", "GROQ_API_KEY": "test-only-not-a-real-groq-key",
                       "DATABASE_URL": f"sqlite:///{(tmp_path_factory.mktemp('m') / 'unused.db').as_posix()}",
                       "CORS_ALLOWED_ORIGINS": ORIGIN, "PYTHON_DOTENV_DISABLED": "1"}.items():
        mp.setenv(key, value)
    _purge()
    reset_settings_cache()
    module = importlib.import_module("main")
    yield module
    _purge()
    mp.undo()
    reset_settings_cache()


@pytest.fixture
def api(auth_db, main_module, monkeypatch):  # noqa: F811
    private, public = new_keypair()
    main = main_module
    sf = auth_db.session_factory
    redis, llm = FakeRedis(), FakeLLM()
    main.app.dependency_overrides[main.get_auth_config] = lambda: auth_config(public)
    main.app.dependency_overrides[main.get_session_factory] = lambda: sf
    main.long_term = main.LongTermMemory(sf)
    main.short_term = ShortTermMemory(client=redis)
    from agentplatform.execution import Limits
    from agentplatform.providers import ProviderRegistry

    provider = FakeProvider()
    main.providers = ProviderRegistry({"groq": lambda: provider})
    main.execution_limits = Limits()
    main.scheduler.client = redis
    main.improver.client = redis
    main.improver.analyzer.short_term = ShortTermMemory(client=redis)
    main.improver.analyzer.long_term = MemoryRepository(sf)
    import improvement.analyzer as analyzer_module
    monkeypatch.setattr(analyzer_module, "client", llm)
    short = ShortTermMemory(client=redis)
    holder = {}

    def fake_agent(name):
        async def run(task, scope):
            holder["api"].seen_scopes.append((name, scope))
            short.save(scope, name, "user", task)
            main.long_term.save(scope, name, f"reply:{task}")
            return f"reply from {name}: {task}"

        return run

    main.orchestrator._agents = {name: fake_agent(name) for name in main.orchestrator.agent_names}
    holder["api"] = Api(main, auth_db, private, public, redis, llm)
    holder["api"].provider = provider
    return holder["api"]
