"""Ownership-scoped agent-memory repository (M1-E).

Needs no settings or engine at import; the session factory is injected. EVERY operation first calls
``require_scope`` (a genuine ``MemoryScope`` or ``ScopeError``) and then constrains by BOTH ``user_id`` and
``workspace_id`` from the scope. There is no email parameter and no unscoped path.

Legacy rows (NULL ``user_id``/``workspace_id``) never match an equality predicate, so they are unreachable here:
never returned, updated or deleted, never inferred from ``user_email``, content or time, never backfilled.
New rows store the scope's ids and leave ``user_email`` NULL (email is not an ownership key).
"""

from datetime import datetime

from sqlalchemy import and_, delete, desc, or_, select, update

from authz.scope import require_scope
from memory.cursor import decode_cursor, encode_cursor
from memory.models import AgentMemory

DEFAULT_LIMIT = 10
PAGE_DEFAULT, PAGE_MAX = 50, 100


def _owned(scope):
    return (AgentMemory.user_id == scope.user_id, AgentMemory.workspace_id == scope.workspace_id)


class MemoryRepository:
    def __init__(self, session_factory):
        self._session_factory = session_factory

    def save(self, scope, agent_name: str, summary: str) -> int:
        scope = require_scope(scope)
        with self._session_factory() as session:
            row = AgentMemory(agent_name=agent_name, summary=summary, user_id=scope.user_id,
                              workspace_id=scope.workspace_id, user_email=None, created_at=datetime.utcnow())
            session.add(row)
            session.commit()
            return row.id

    def get(self, scope, agent_name: str, limit: int = DEFAULT_LIMIT) -> list:
        scope = require_scope(scope)
        with self._session_factory() as session:
            rows = session.execute(
                select(AgentMemory).where(AgentMemory.agent_name == agent_name, *_owned(scope))
                .order_by(desc(AgentMemory.created_at), desc(AgentMemory.id)).limit(limit)
            ).scalars().all()
            return [{"id": r.id, "agent_name": r.agent_name, "summary": r.summary,
                     "created_at": r.created_at.isoformat()} for r in rows]

    def get_history(self, scope) -> list:
        scope = require_scope(scope)
        with self._session_factory() as session:
            rows = session.execute(
                select(AgentMemory).where(*_owned(scope)).order_by(desc(AgentMemory.created_at), desc(AgentMemory.id))
            ).scalars().all()
            return [{"id": r.id, "agent_name": r.agent_name, "summary": r.summary,
                     "created_at": r.created_at.isoformat()} for r in rows]

    def get_history_page(self, scope, limit: int = PAGE_DEFAULT, cursor: str | None = None) -> dict:
        """Newest-first page of the scope's history. Keyset pagination on ``(created_at, id)``: no duplicates or
        omissions across pages, even with equal timestamps. Raises ``CursorError`` for a bad cursor and ValueError
        for a limit outside 1..100."""
        scope = require_scope(scope)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        query = select(AgentMemory).where(*_owned(scope))
        if cursor is not None:
            c_time, c_id = decode_cursor(cursor)
            query = query.where(or_(AgentMemory.created_at < c_time,
                                    and_(AgentMemory.created_at == c_time, AgentMemory.id < c_id)))
        with self._session_factory() as session:
            rows = session.execute(
                query.order_by(desc(AgentMemory.created_at), desc(AgentMemory.id)).limit(limit + 1)
            ).scalars().all()
            page = rows[:limit]
            items = [{"id": r.id, "agent_name": r.agent_name, "summary": r.summary,
                      "created_at": r.created_at.isoformat()} for r in page]
            more = len(rows) > limit
            return {"items": items, "next_cursor": encode_cursor(page[-1].created_at, page[-1].id) if more else None}

    def update_summary(self, scope, memory_id: int, summary: str) -> bool:
        """True if an OWNED row with this id was updated; a foreign, legacy or missing id changes nothing."""
        scope = require_scope(scope)
        with self._session_factory() as session:
            result = session.execute(update(AgentMemory).where(AgentMemory.id == memory_id, *_owned(scope)).values(summary=summary))
            session.commit()
            return result.rowcount == 1

    def delete(self, scope, memory_id: int) -> bool:
        """True if an OWNED row with this id was deleted; a foreign, legacy or missing id changes nothing."""
        scope = require_scope(scope)
        with self._session_factory() as session:
            result = session.execute(delete(AgentMemory).where(AgentMemory.id == memory_id, *_owned(scope)))
            session.commit()
            return result.rowcount == 1
