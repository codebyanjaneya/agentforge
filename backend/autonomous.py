import re
from datetime import datetime

from orchestrator import AgentOrchestrator

# Keyword patterns mapped to concrete action types.
ACTION_PATTERNS = {
    "email_draft": [
        "email",
        "outreach",
        "subject:",
        "dear ",
        "reach out",
    ],
    "report_generated": [
        "report",
        "summary",
        "overview",
        "breakdown",
    ],
    "strategy_created": [
        "strategy",
        "plan",
        "roadmap",
        "goal",
        "step ",
    ],
    "leads_found": [
        "lead",
        "prospect",
        "contact",
        "company",
    ],
    "analysis_done": [
        "analysis",
        "competitor",
        "trend",
        "insight",
        "research",
    ],
}


class AutonomousExecutor:
    def __init__(self, orchestrator: AgentOrchestrator | None = None):
        self.orchestrator = orchestrator or AgentOrchestrator()

    async def execute(
        self, agent_name: str, task: str, scope
    ) -> dict:
        response = await self.orchestrator.run_agent(
            agent_name, task, scope
        )
        return self.describe(agent_name, task, response)

    def describe(self, agent_name: str, task: str, response: str) -> dict:
        """The response shape of ``POST /autonomous/run`` for an answer produced elsewhere (the execution service)."""
        return {
            "agent": agent_name,
            "task": task,
            "response": response,
            "actions": self._extract_actions(response),
            "executed_at": datetime.utcnow().isoformat(),
        }

    def _extract_actions(self, response: str) -> list:
        """Derive concrete actions from the agent response.

        Scans the response for keywords tied to each action type and creates a
        completed action describing what was produced. Falls back to a generic
        action if nothing specific is detected.
        """
        lowered = response.lower()
        actions = []

        for action_type, keywords in ACTION_PATTERNS.items():
            if any(keyword in lowered for keyword in keywords):
                actions.append(
                    {
                        "action_type": action_type,
                        "description": self._describe(action_type, response),
                        "status": "completed",
                    }
                )

        if not actions:
            actions.append(
                {
                    "action_type": "report_generated",
                    "description": "Generated a response for the requested task.",
                    "status": "completed",
                }
            )

        return actions

    def _describe(self, action_type: str, response: str) -> str:
        descriptions = {
            "email_draft": "Drafted an outreach email based on the request.",
            "report_generated": "Compiled a structured report from the response.",
            "strategy_created": "Created a strategic plan with concrete steps.",
            "leads_found": "Identified potential leads and contacts.",
            "analysis_done": "Completed analysis with actionable insights.",
        }
        base = descriptions.get(action_type, "Completed the requested action.")

        numbers = re.findall(r"\b\d+\b", response)
        if action_type == "leads_found" and numbers:
            base = f"Identified {numbers[0]} potential leads and contacts."
        return base
