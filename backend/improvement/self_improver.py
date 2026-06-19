import json
import os
from datetime import datetime

import redis
from dotenv import load_dotenv

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

    def _key(self, agent_name: str) -> str:
        return f"improvements:{agent_name}"

    async def improve(self, agent_name: str) -> dict:
        report = await self.analyzer.analyze(agent_name)
        report["analyzed_at"] = datetime.utcnow().isoformat()
        self.client.set(self._key(agent_name), json.dumps(report))
        return report

    def get_improvements(self, agent_name: str) -> dict:
        raw = self.client.get(self._key(agent_name))
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
