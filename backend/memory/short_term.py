import json
import os

import redis
from dotenv import load_dotenv

load_dotenv()

MAX_MESSAGES = 20

_client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


class ShortTermMemory:
    def __init__(self, client: redis.Redis = _client):
        self.client = client

    def _key(self, agent_name: str) -> str:
        return f"memory:{agent_name}"

    def save(self, agent_name: str, role: str, content: str) -> None:
        messages = self.get(agent_name)
        messages.append({"role": role, "content": content})
        messages = messages[-MAX_MESSAGES:]
        self.client.set(self._key(agent_name), json.dumps(messages))

    def get(self, agent_name: str) -> list:
        raw = self.client.get(self._key(agent_name))
        if not raw:
            return []
        return json.loads(raw)[-MAX_MESSAGES:]

    def clear(self, agent_name: str) -> None:
        self.client.delete(self._key(agent_name))
