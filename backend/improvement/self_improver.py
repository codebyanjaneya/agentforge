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

    def _key(self, agent_name: str, user_email: str | None = None) -> str:
        if user_email:
            return f"improvements:{agent_name}:{user_email}"
        return f"improvements:{agent_name}"

    async def improve(self, agent_name: str, user_email: str | None = None) -> dict:
        report = await self.analyzer.analyze(agent_name, user_email)
        report["analyzed_at"] = datetime.utcnow().isoformat()
        self.client.set(self._key(agent_name, user_email), json.dumps(report))
        return report

    def get_improvements(
        self, agent_name: str, user_email: str | None = None
    ) -> dict:
        raw = self.client.get(self._key(agent_name, user_email))
        if not raw:
            return {
                "agent_name": agent_name,
                "user_email": user_email,
                "strengths": [],
                "weaknesses": [],
                "improvements": [],
                "updated_strategy": "",
                "has_history": False,
            }
        return json.loads(raw)
