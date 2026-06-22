import os

from dotenv import load_dotenv
from groq import AsyncGroq

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory

load_dotenv()

client = AsyncGroq(api_key=os.environ["GROQ_API_KEY"])
MODEL_NAME = os.getenv("MODEL_NAME", "llama-3.3-70b-versatile")

AGENT_NAME = "hiring"

SYSTEM_PROMPT = (
    "You are the Hiring Agent of AgentForge. Your job is: screen resumes, "
    "schedule interviews, rank candidates, automate candidate communication, "
    "and provide AI interview summaries. Be objective, structured, and thorough "
    "in evaluations."
)

short_term = ShortTermMemory()
long_term = LongTermMemory()


async def run(task: str, user_email: str | None = None) -> str:
    history = short_term.get(AGENT_NAME, user_email)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": task})

    response = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
    )
    reply = response.choices[0].message.content

    short_term.save(AGENT_NAME, "user", task, user_email)
    short_term.save(AGENT_NAME, "assistant", reply, user_email)
    long_term.save(AGENT_NAME, reply, user_email)

    return reply
