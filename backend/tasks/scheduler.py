import asyncio
import json
import os
import uuid
from datetime import datetime

import redis
from dotenv import load_dotenv

from authz.scope import require_scope

load_dotenv()

_client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


class TaskScheduler:
    """Scheduled-task records, one Redis hash per (user_id, workspace_id) scope (M1-F). The legacy global
    ``scheduled_tasks`` hash is never read, written or deleted. A task id alone authorizes nothing: it is only a
    field inside the caller's own scope hash, so another scope's id is simply not found. Records carry no email."""

    def __init__(self, client: redis.Redis = _client):
        self.client = client
        self._runners: dict[str, asyncio.Task] = {}

    def _key(self, scope) -> str:
        scope = require_scope(scope)
        return f"scheduled_tasks:v2:{scope.user_id}:{scope.workspace_id}"

    def add_task(self, scope, agent_name: str, task: str, interval_minutes: int) -> dict:
        key = self._key(scope)
        task_id = uuid.uuid4().hex
        record = {
            "id": task_id,
            "agent": agent_name,
            "task": task,
            "interval_minutes": interval_minutes,
            "created_at": datetime.utcnow().isoformat(),
        }
        self.client.hset(key, task_id, json.dumps(record))
        return record

    def get_scheduled_tasks(self, scope) -> list:
        raw = self.client.hgetall(self._key(scope))
        return [json.loads(value) for value in raw.values()]

    def remove_task(self, scope, task_id: str) -> bool:
        key = self._key(scope)
        removed = self.client.hdel(key, task_id)
        if removed:
            runner = self._runners.pop(f"{key}:{task_id}", None)
            if runner is not None:
                runner.cancel()
        return bool(removed)
