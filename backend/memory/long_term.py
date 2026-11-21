from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import get_settings
from memory.repository import MemoryRepository
from memory.models import AgentMemory, Base, TelegramLink  # noqa: F401  (re-exported for compatibility)

load_dotenv()

# Lazy engine: no connection is opened at import time. Tables are created only by Alembic
# migrations (a separate deployment step); startup just verifies the schema version.
engine = create_engine(
    get_settings().database_url.get_secret_value(),
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_link(chat_id: int) -> dict | None:
    """Return the stored link record for a chat, or None if not linked yet."""
    with SessionLocal() as session:
        row = session.get(TelegramLink, chat_id)
        if row is None:
            return None
        return {
            "chat_id": row.chat_id,
            "email": row.email,
            "active_agent": row.active_agent,
            "updated_at": (
                row.updated_at.isoformat() if row.updated_at else None
            ),
        }


def set_email(chat_id: int, email: str) -> None:
    """Upsert the linked email for a chat (active agent defaults to 'ceo')."""
    with SessionLocal() as session:
        row = session.get(TelegramLink, chat_id)
        if row is None:
            session.add(TelegramLink(chat_id=chat_id, email=email))
        else:
            row.email = email
        session.commit()


def set_active_agent(chat_id: int, agent_name: str) -> None:
    """Upsert the active agent for a chat (email stays None if not linked)."""
    with SessionLocal() as session:
        row = session.get(TelegramLink, chat_id)
        if row is None:
            session.add(
                TelegramLink(chat_id=chat_id, active_agent=agent_name)
            )
        else:
            row.active_agent = agent_name
        session.commit()


class LongTermMemory(MemoryRepository):
    """Scope-only memory (M1-E). The email-keyed API was removed: every method takes a ``MemoryScope`` first, and
    anything else (an email string, None) raises ``ScopeError`` before touching the database."""

    def __init__(self, session_factory=None):
        super().__init__(session_factory or SessionLocal)
