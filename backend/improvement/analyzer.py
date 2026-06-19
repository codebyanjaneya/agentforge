import json
import os

from dotenv import load_dotenv
from groq import AsyncGroq

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

SYSTEM_PROMPT = (
    "You are an AI performance analyst. Analyze this agent's past interactions "
    "and identify: 1) What worked well 2) What didn't work 3) Specific "
    "improvements to the agent's approach 4) Updated strategy recommendations. "
    "Be specific and actionable. Respond ONLY with a JSON object using exactly "
    "these keys: \"strengths\" (array of strings), \"weaknesses\" (array of "
    "strings), \"improvements\" (array of strings), and \"updated_strategy\" "
    "(a single string)."
)


class AgentAnalyzer:
    def __init__(self):
        self.short_term = ShortTermMemory()
        self.long_term = LongTermMemory()

    async def analyze(self, agent_name: str) -> dict:
        interactions = self.short_term.get(agent_name)
        summaries = self.long_term.get(agent_name)

        if not interactions and not summaries:
            return {
                "agent_name": agent_name,
                "strengths": [],
                "weaknesses": [],
                "improvements": [],
                "updated_strategy": "",
                "has_history": False,
            }

        history = self._format_history(interactions, summaries)

        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": history},
            ],
            response_format={"type": "json_object"},
        )

        parsed = self._parse(response.choices[0].message.content)

        return {
            "agent_name": agent_name,
            "strengths": parsed.get("strengths", []),
            "weaknesses": parsed.get("weaknesses", []),
            "improvements": parsed.get("improvements", []),
            "updated_strategy": parsed.get("updated_strategy", ""),
            "has_history": True,
        }

    def _format_history(self, interactions: list, summaries: list) -> str:
        lines = ["=== Recent Interactions ==="]
        for item in interactions:
            role = item.get("role", "unknown")
            content = item.get("content", "")
            lines.append(f"[{role}] {content}")

        lines.append("\n=== Past Summaries ===")
        for summary in summaries:
            lines.append(f"- {summary.get('summary', '')}")

        lines.append(
            "\nAnalyze the agent's performance based on the history above."
        )
        return "\n".join(lines)

    def _parse(self, content: str) -> dict:
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return {}
