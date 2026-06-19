import asyncio
import json
import os
import uuid
from datetime import datetime

import redis
from dotenv import load_dotenv

load_dotenv()

REDIS_KEY = "scheduled_tasks"

_client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


class TaskScheduler:
    def __init__(self, client: redis.Redis = _client):
        self.client = client
        self._runners: dict[str, asyncio.Task] = {}

    def add_task(
        self, agent_name: str, task: str, interval_minutes: int
    ) -> dict:
        task_id = uuid.uuid4().hex
        record = {
            "id": task_id,
            "agent": agent_name,
            "task": task,
            "interval_minutes": interval_minutes,
            "created_at": datetime.utcnow().isoformat(),
        }
        self.client.hset(REDIS_KEY, task_id, json.dumps(record))
        return record

    def get_scheduled_tasks(self) -> list:
        raw = self.client.hgetall(REDIS_KEY)
        return [json.loads(value) for value in raw.values()]

    def remove_task(self, task_id: str) -> bool:
        runner = self._runners.pop(task_id, None)
        if runner is not None:
            runner.cancel()
        removed = self.client.hdel(REDIS_KEY, task_id)
        return bool(removed)
