"""In-app notifications of the caller (M6). Durable, backend-originated rows (written in the transaction that creates what they announce: see ``ApprovalStore.request``);
this module only READS them for their recipient and lets the recipient mark them read. No email, SMS or push exists.

``pending`` (the badge) counts the caller's notifications whose approval is STILL pending now (open approvals past their window are expired first, so the number never
counts a request nobody can decide any more); ``unread`` counts notifications not yet marked read. Both are scoped to the caller's user AND workspace."""

from datetime import datetime

from sqlalchemy import func, select, update

from agentplatform.tools.approvals import _expire_due
from authz.scope import require_scope
from memory.models import Notification, ToolApproval

PAGE_MAX = 100


class NotificationStore:
    def __init__(self, session_factory, *, clock=datetime.utcnow):
        self._sessions, self._now = session_factory, clock

    def _mine(self, scope):
        return (Notification.user_id == scope.user_id, Notification.workspace_id == scope.workspace_id)

    def list(self, scope, *, unread_only: bool = False, limit: int = 50) -> dict:
        scope = require_scope(scope)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise ValueError("invalid_limit")
        with self._sessions() as session, session.begin():
            _expire_due(session, ToolApproval.workspace_id == scope.workspace_id, self._now())
            query = select(Notification, ToolApproval).join(ToolApproval, ToolApproval.id == Notification.ref_id).where(*self._mine(scope))
            if unread_only:
                query = query.where(Notification.read_at.is_(None))
            rows = session.execute(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)).all()
            items = [{"id": str(n.id), "kind": n.kind, "approval_id": str(n.ref_id), "run_id": str(n.run_id) if n.run_id else None, "created_at": n.created_at.isoformat(),
                      "read_at": n.read_at.isoformat() if n.read_at else None,
                      "approval": {"status": a.status, "tool": f"{a.tool_id}@{a.tool_version}", "agent": a.agent_key, "task": a.task_key, "expires_at": a.expires_at.isoformat()}} for n, a in rows]
            return {"items": items, **self._counts(session, scope)}

    def _counts(self, session, scope) -> dict:
        pending = session.execute(select(func.count()).select_from(Notification).join(ToolApproval, ToolApproval.id == Notification.ref_id)
                                  .where(*self._mine(scope), ToolApproval.status == "pending")).scalar_one()
        unread = session.execute(select(func.count()).select_from(Notification).where(*self._mine(scope), Notification.read_at.is_(None))).scalar_one()
        return {"pending": pending, "unread": unread}

    def counts(self, scope) -> dict:
        scope = require_scope(scope)
        with self._sessions() as session, session.begin():
            _expire_due(session, ToolApproval.workspace_id == scope.workspace_id, self._now())
            return self._counts(session, scope)

    def mark_read(self, scope, notification_id=None) -> int:
        """Mark one (or, with None, all) of the caller's unread notifications read. Returns how many changed; a foreign or unknown id changes nothing (the caller sees 404)."""
        scope = require_scope(scope)
        query = update(Notification).where(*self._mine(scope), Notification.read_at.is_(None)).values(read_at=self._now())
        if notification_id is not None:
            query = query.where(Notification.id == notification_id)
        with self._sessions() as session, session.begin():
            return session.execute(query).rowcount

    def exists(self, scope, notification_id) -> bool:
        scope = require_scope(scope)
        with self._sessions() as session:
            return session.execute(select(Notification.id).where(*self._mine(scope), Notification.id == notification_id)).first() is not None
