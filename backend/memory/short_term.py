import json
import os

import redis
from dotenv import load_dotenv

from authz.scope import require_scope

load_dotenv()

MAX_MESSAGES = 20

_client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


class ShortTermMemory:
    """Conversation history keyed by (user_id, workspace_id, agent). No email key, no unscoped fallback:
    a missing or invalid ``MemoryScope`` raises ``ScopeError`` before Redis is touched."""

    def __init__(self, client: redis.Redis = _client):
        self.client = client

    def _key(self, scope, agent_name: str) -> str:
        scope = require_scope(scope)
        return f"short_term:v2:{scope.user_id}:{scope.workspace_id}:{agent_name}"

    def save(self, scope, agent_name: str, role: str, content: str) -> None:
        key = self._key(scope, agent_name)
        messages = self.get(scope, agent_name)
        messages.append({"role": role, "content": content})
        self.client.set(key, json.dumps(messages[-MAX_MESSAGES:]))

    def get(self, scope, agent_name: str) -> list:
        raw = self.client.get(self._key(scope, agent_name))
        if not raw:
            return []
        return json.loads(raw)[-MAX_MESSAGES:]

    def clear(self, scope, agent_name: str) -> None:
        self.client.delete(self._key(scope, agent_name))
