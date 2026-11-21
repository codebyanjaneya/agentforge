"""Test isolation for the backend.

Guarantees, in order of importance:
1. No real .env is ever read (PYTHON_DOTENV_DISABLED is set before any app import).
2. No outbound network: connections are blocked except in-process plumbing
   (AF_UNIX and the loopback socketpair asyncio creates on Windows).
3. No Telegram token and no real credentials exist in the test environment.
4. Databases are SQLite files under pytest's tmp_path.
"""

import importlib
import os
import socket
import sys
from pathlib import Path

# Must happen before any backend module (which calls load_dotenv at import) is imported.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import pytest  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

# Variables the app (including legacy modules that read os.environ directly) may use.
APP_ENV_VARS = (
    "APP_ENV", "GROQ_API_KEY", "MODEL_NAME", "DATABASE_URL", "REDIS_URL",
    "TELEGRAM_BOT_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "CORS_ALLOWED_ORIGINS", "API_DOCS_ENABLED", "AUTH_CONFIG_REQUIRED",
    "AUTH_JWT_ISSUER", "AUTH_JWT_AUDIENCE", "AUTH_JWT_PUBLIC_KEYS",
)
# Obviously fake; only needed because legacy agent modules construct a client at import.
DUMMY_GROQ_KEY = "test-only-not-a-real-groq-key"


class NetworkBlocked(RuntimeError):
    pass


def _is_internal_socketpair_call() -> bool:
    # On Windows, socket.socketpair() (used by asyncio for its self-pipe) connects
    # over loopback from inside the stdlib socket module.
    frame = sys._getframe(3)  # 0=this, 1=_allowed, 2=guarded_*, 3=caller
    return frame.f_code.co_name == "socketpair" and frame.f_code.co_filename.endswith("socket.py")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _allowed(sock) -> bool:
        return sock.family == getattr(socket, "AF_UNIX", -1) or _is_internal_socketpair_call()

    def guarded_connect(self, address):
        if not _allowed(self):
            raise NetworkBlocked("outbound connection blocked during tests")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _allowed(self):
            raise NetworkBlocked("outbound connection blocked during tests")
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host not in (None, "", "localhost", "127.0.0.1", "::1"):
            raise NetworkBlocked("DNS lookup blocked during tests")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(autouse=True)
def clean_app_env(monkeypatch):
    """Start every test with none of the app's variables set."""
    for name in APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    from config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


def _purge_backend_modules() -> None:
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if not path:
            continue
        p = Path(path).resolve()
        if (
            BACKEND_DIR in p.parents
            and "venv" not in p.parts
            and TESTS_DIR not in p.parents
            and p.name not in ("config.py", "app_factory.py", "schema_guard.py")  # stateless; keeps error-class identity
            and (BACKEND_DIR / "authz") not in p.parents and (BACKEND_DIR / "auth") not in p.parents  # keeps AuthzError/MemoryScope class identity (M1-E)
        ):
            sys.modules.pop(name, None)


@pytest.fixture
def import_fresh(monkeypatch, tmp_path):
    """Import a fresh backend module using an isolated SQLite file and a clean module cache."""
    db = tmp_path / "test.db"

    def _import(module: str, **env):
        from config import reset_settings_cache

        _purge_backend_modules()
        reset_settings_cache()
        base = {
            "APP_ENV": "test",
            "GROQ_API_KEY": DUMMY_GROQ_KEY,
            "DATABASE_URL": f"sqlite:///{db.as_posix()}",
        }
        for key, value in {**base, **env}.items():
            monkeypatch.setenv(key, value)
        return importlib.import_module(module)

    yield _import
    _purge_backend_modules()


@pytest.fixture
def import_main(import_fresh):
    """Import a fresh ``main`` (see import_fresh)."""
    return lambda **env: import_fresh("main", **env)


@pytest.fixture
def migrate_test_db():
    """Build the schema the way production does: the real ``alembic upgrade head``.

    Runs env.py's online path against whatever ``DATABASE_URL`` the test environment currently
    holds (set by import_fresh/import_main to a throw-away SQLite file in tmp_path, APP_ENV=test).
    Startup never creates tables, so tests that run the real lifespan call this first.
    """

    def _migrate(revision: str = "head") -> None:
        from alembic import command
        from alembic.config import Config

        assert os.environ.get("APP_ENV") == "test" and os.environ["DATABASE_URL"].startswith("sqlite:///")
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.attributes["configure_logger"] = False
        command.upgrade(cfg, revision)

    return _migrate

