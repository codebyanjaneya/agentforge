from authz.scope import require_scope
from agents import (
    ceo,
    finance,
    hiring,
    marketing,
    research,
    sales,
    support,
)


class AgentOrchestrator:
    def __init__(self):
        self._agents = {
            "ceo": ceo.run,
            "sales": sales.run,
            "marketing": marketing.run,
            "support": support.run,
            "research": research.run,
            "finance": finance.run,
            "hiring": hiring.run,
        }

    @property
    def agent_names(self) -> tuple:
        return tuple(self._agents)

    async def run_agent(
        self, agent_name: str, task: str, scope
    ) -> str:
        scope = require_scope(scope)  # fail closed before any agent or LLM call
        agent = self._agents.get(agent_name)
        if agent is None:
            raise ValueError(
                f"Unknown agent '{agent_name}'. "
                f"Valid options: {', '.join(self._agents)}"
            )
        return await agent(task, scope)

    async def run_with_delegation(
        self, agent_name: str, task: str, scope
    ) -> dict:
        scope = require_scope(scope)
        full_response = await self.run_agent(agent_name, task, scope)

        delegations = []
        for sub_agent, subtask in self._extract_delegations(
            full_response, exclude=agent_name
        ):
            response = await self.run_agent(sub_agent, subtask, scope)
            delegations.append(
                {"agent": sub_agent, "task": subtask, "response": response}
            )

        primary_response = self._overview(full_response)

        return {
            "primary_agent": agent_name,
            "primary_response": primary_response,
            "delegations": delegations,
        }

    def _overview(self, text: str) -> str:
        """Return only the first 3 paragraphs of the response as a concise overview."""
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        return "\n\n".join(paragraphs[:3])

    def _extract_delegations(self, text: str, exclude: str):
        """Find sub-agents mentioned in the text and extract a subtask for each.

        Returns a list of (agent_name, subtask) tuples, preserving order and
        without duplicates.
        """
        keywords = {
            "sales agent": "sales",
            "marketing agent": "marketing",
            "research agent": "research",
            "finance agent": "finance",
            "hiring agent": "hiring",
            "support agent": "support",
        }

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        found = {}

        for line in lines:
            lowered = line.lower()
            for keyword, agent in keywords.items():
                if agent == exclude or agent in found:
                    continue
                if keyword in lowered:
                    found[agent] = line

        return [(agent, found[agent]) for agent in found]
