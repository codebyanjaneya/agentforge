"""Guards against new scattered environment reads.

``LEGACY`` lists files that still read the environment directly. It must only
shrink: when a file stops reading env vars this test fails until it is removed
from the list.
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"os\.environ|os\.getenv|\bgetenv\(")

LEGACY = {
    "agents/ceo.py", "agents/sales.py", "agents/marketing.py", "agents/support.py",
    "agents/research.py", "agents/finance.py", "agents/hiring.py",
    "improvement/analyzer.py", "improvement/self_improver.py", "memory/short_term.py",
    "tasks/scheduler.py", "telegram_bot.py", "voice_handler.py",
}
ALLOWED_NEW = {"config.py"}


def backend_sources():
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND)
        if rel.parts[0] in ("venv", "tests", "__pycache__"):
            continue
        yield rel.as_posix(), path


def test_env_access_only_in_config_and_legacy_files():
    users = {rel for rel, p in backend_sources() if PATTERN.search(p.read_text(encoding="utf-8"))}
    assert users - ALLOWED_NEW - LEGACY == set(), "new direct environment access: use config.get_settings()"
    assert LEGACY - users == set(), "no longer reads env: remove from LEGACY"
