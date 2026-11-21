"""REST routes for the caller's in-app notifications (M6): list, counts (the badge) and mark read. Every route uses ``get_scope``; a notification is only ever visible to its recipient."""

import asyncio
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query

from agentplatform.notifications import NotificationStore
from auth.dependencies import get_session_factory
from authz.dependencies import get_scope

router = APIRouter()


def get_store(session_factory=Depends(get_session_factory)) -> NotificationStore:
    return NotificationStore(session_factory)


@router.get("/notifications")
async def list_notifications(unread: bool = False, limit: int = Query(default=50, ge=1, le=100), scope=Depends(get_scope), store: NotificationStore = Depends(get_store)):
    return await asyncio.to_thread(store.list, scope, unread_only=unread, limit=limit)


@router.get("/notifications/count")
async def count_notifications(scope=Depends(get_scope), store: NotificationStore = Depends(get_store)):
    """The badge: cheap, bounded, and accurate (expired approvals are closed first)."""
    return await asyncio.to_thread(store.counts, scope)


@router.post("/notifications/read")
async def read_all(scope=Depends(get_scope), store: NotificationStore = Depends(get_store)):
    return {"changed": await asyncio.to_thread(store.mark_read, scope)}


@router.post("/notifications/{notification_id}/read")
async def read_one(notification_id: uuid.UUID, scope=Depends(get_scope), store: NotificationStore = Depends(get_store)):
    if not await asyncio.to_thread(store.exists, scope, notification_id):
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return {"changed": await asyncio.to_thread(store.mark_read, scope, notification_id)}
