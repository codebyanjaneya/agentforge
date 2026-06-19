import os

from dotenv import load_dotenv
from groq import AsyncGroq

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

AGENT_NAME = "finance"

SYSTEM_PROMPT = (
    "You are the Finance Agent of AgentForge. Your job is: track expenses and "
    "revenue, predict cashflow, generate profit reports, and alert on burn "
    "rate. Analyze financial data provided and give clear, actionable insights. "
    "Always include numbers, percentages, and specific recommendations."
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
