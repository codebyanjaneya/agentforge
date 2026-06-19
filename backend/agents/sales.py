import os

from dotenv import load_dotenv
from groq import AsyncGroq

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

AGENT_NAME = "sales"

SYSTEM_PROMPT = (
    "Your name is the Sales Agent for a startup. You are an expert in lead "
    "generation, outreach emails, sales strategies, and closing deals. "
    "Be concise and actionable. Use the recent conversation history provided "
    "as your memory context to stay consistent and avoid repeating yourself."
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
