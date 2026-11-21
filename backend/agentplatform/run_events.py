"""Append-only execution history of plan runs (M4-A2).

CONTRACT. One row per meaningful lifecycle fact, written with ``add`` INSIDE the same database transaction as the state change it
describes (so an event exists if and only if its transition committed). Rows are scoped by (user_id, workspace_id) — copied from the
run — and can never be updated or deleted (triggers in migration 0010). ``id`` is the total order (a database-assigned, strictly
increasing number); readers sort by it. This is NOT a logging system: the event types are a closed vocabulary, and ``detail`` is
built only from a fixed whitelist of short codes, task keys, ids and numbers — task text, results, provider output, exception text,
credentials and tokens are never accepted (an unknown key, a long string or a nested value raises, so a mistake fails loudly in a test
instead of leaking). ``actor_id`` is internal (a user id or a process id) and is never returned by the API; the API shows only the
actor TYPE.
"""

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select

from memory.models import EVENT_ACTORS, EVENT_TYPES, ExecutionEvent, PlanRun

MAX_STRING = 64
MAX_LIST = 50
MAX_DETAIL_CHARS = 1000

# key -> accepted kind ("str", "int", "bool", "keys" = a list of short strings)
ALLOWED_DETAIL = {
    "status": "str", "code": "str", "reason": "str", "attempt": "int", "retry_of_attempt": "int", "generation": "int", "count": "int",
    "reconciled": "int", "carried_count": "int", "resumed": "bool", "acknowledged": "keys", "carried": "keys", "retried": "keys",
    "tool": "str", "seq": "int", "approval": "str", "policy": "str",  # M4-C: the exact tool ref ("id@version"), the step, the approval id and the policy fingerprint (never values)
}


class EventError(ValueError):
    pass


def build_detail(detail: dict) -> str | None:
    """Validate and serialise ``detail`` (deterministic JSON). Raises ``EventError`` for anything outside the whitelist."""
    clean = {}
    for key in sorted(detail):
        value = detail[key]
        if value is None:
            continue
        kind = ALLOWED_DETAIL.get(key)
        if kind is None:
            raise EventError(f"detail key not allowed: {key}")
        if kind == "str" and not (isinstance(value, str) and len(value) <= MAX_STRING and value.isprintable()):
            raise EventError(f"detail {key}: a short printable string is required")
        if kind == "int" and not (type(value) is int and 0 <= value < 10**9):
            raise EventError(f"detail {key}: a small integer is required")
        if kind == "bool" and type(value) is not bool:
            raise EventError(f"detail {key}: a boolean is required")
        if kind == "keys":
            if not (isinstance(value, (list, tuple)) and len(value) <= MAX_LIST and all(isinstance(v, str) and 0 < len(v) <= 32 and v.isprintable() for v in value)):
                raise EventError(f"detail {key}: a short list of task keys is required")
            value = sorted(value)
        clean[key] = value
    if not clean:
        return None
    text = json.dumps(clean, sort_keys=True, separators=(",", ":"))
    if len(text) > MAX_DETAIL_CHARS:
        raise EventError("detail too large")
    return text


def run_scope(session, run_id) -> tuple:
    """(user_id, workspace_id) of a run: events always carry the run's own scope, never a caller-supplied one."""
    return session.execute(select(PlanRun.user_id, PlanRun.workspace_id).where(PlanRun.id == run_id)).one()


def add(session, run_id, event_type: str, *, actor_type: str, actor_id: str | None = None, task_key: str | None = None, execution_id=None,
        scope: tuple | None = None, created_at: datetime | None = None, **detail) -> None:
    """Insert one event in the caller's transaction. ``scope`` may be passed to avoid a lookup when several events are written."""
    if event_type not in EVENT_TYPES:
        raise EventError(f"unknown event type: {event_type}")
    if actor_type not in EVENT_ACTORS:
        raise EventError(f"unknown actor type: {actor_type}")
    user_id, workspace_id = scope or run_scope(session, run_id)
    session.execute(insert(ExecutionEvent).values(
        run_id=run_id, user_id=user_id, workspace_id=workspace_id, task_key=task_key, execution_id=execution_id, event_type=event_type,
        actor_type=actor_type, actor_id=actor_id, detail=build_detail(detail), created_at=created_at or datetime.utcnow()))


@dataclass(frozen=True)
class EventView:
    id: int
    run_id: object
    task_key: str | None
    execution_id: object | None
    event_type: str
    actor_type: str
    detail: dict
    created_at: datetime


def view(row: ExecutionEvent) -> EventView:
    return EventView(row.id, row.run_id, row.task_key, row.execution_id, row.event_type, row.actor_type, json.loads(row.detail) if row.detail else {}, row.created_at)
