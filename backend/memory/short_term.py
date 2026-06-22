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

    def _key(self, agent_name: str, user_email: str | None = None) -> str:
        # Namespace conversation history per user so accounts never share
        # context. Callers without a user (e.g. global analysis) fall back to
        # a non-namespaced key.
        if user_email:
            return f"short_term:{user_email}:{agent_name}"
        return f"short_term:{agent_name}"

    def save(
        self,
        agent_name: str,
        role: str,
        content: str,
        user_email: str | None = None,
    ) -> None:
        messages = self.get(agent_name, user_email)
        messages.append({"role": role, "content": content})
        messages = messages[-MAX_MESSAGES:]
        self.client.set(
            self._key(agent_name, user_email), json.dumps(messages)
        )

    def get(self, agent_name: str, user_email: str | None = None) -> list:
        raw = self.client.get(self._key(agent_name, user_email))
        if not raw:
            return []
        return json.loads(raw)[-MAX_MESSAGES:]

    def clear(self, agent_name: str, user_email: str | None = None) -> None:
        self.client.delete(self._key(agent_name, user_email))
