import os

from dotenv import load_dotenv
from groq import AsyncGroq

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

AGENT_NAME = "ceo"

SYSTEM_PROMPT = (
    "You are the CEO Agent of AgentForge, an AI-powered startup OS. You are the "
    "mastermind orchestrator. Your job is: set goals, delegate tasks to other "
    "agents, monitor progress, plan weekly strategy, and forecast revenue. When "
    "given a goal, break it down into specific tasks for Sales, Marketing, "
    "Research, Support, Finance, and Hiring agents. Be decisive, strategic, and "
    "action-oriented."
)

short_term = ShortTermMemory()
long_term = LongTermMemory()


async def run(task: str) -> str:
    history = short_term.get(AGENT_NAME)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": task})

    response = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
    )
    reply = response.choices[0].message.content

    short_term.save(AGENT_NAME, "user", task)
    short_term.save(AGENT_NAME, "assistant", reply)

    return reply
