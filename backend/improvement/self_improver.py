import json
import os
from datetime import datetime

import redis
from dotenv import load_dotenv

from authz.scope import require_scope
from improvement.analyzer import AgentAnalyzer

load_dotenv()

_client = redis.Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)


class SelfImprover:
    def __init__(self, client: redis.Redis = _client):
        self.client = client
        self.analyzer = AgentAnalyzer()

    def _key(self, scope, agent_name: str) -> str:
        scope = require_scope(scope)  # no unscoped key
        return f"improvements:v2:{scope.user_id}:{scope.workspace_id}:{agent_name}"

    async def improve(self, agent_name: str, scope) -> dict:
        key = self._key(scope, agent_name)
        report = await self.analyzer.analyze(agent_name, scope)
        report["analyzed_at"] = datetime.utcnow().isoformat()
        self.client.set(key, json.dumps(report))
        return report

    def get_improvements(
        self, agent_name: str, scope
    ) -> dict:
        raw = self.client.get(self._key(scope, agent_name))
        if not raw:
            return {
                "agent_name": agent_name,
                "strengths": [],
                "weaknesses": [],
                "improvements": [],
                "updated_strategy": "",
                "has_history": False,
            }
        return json.loads(raw)
