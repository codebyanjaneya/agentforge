"""Telegram bot for AgentForge.

Runs in-process alongside the FastAPI app (launched as an asyncio background
task from main.py's lifespan). It reuses the same orchestrator/agent functions
the REST and WebSocket handlers use, so Telegram conversations are persisted to
the same per-user long-term memory (keyed by the linked email) and therefore
show up in GET /history/{user_email}.

Note: the agents already call long_term.save(...) inside their own run()
functions, so simply invoking the orchestrator here is enough to persist the
interaction — no extra/duplicate save is performed.
"""

import asyncio
import logging
import os
import re

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from orchestrator import AgentOrchestrator

load_dotenv()

logger = logging.getLogger("uvicorn.error")

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_CHARS = 4096

DEFAULT_AGENT = "ceo"

# Ordered so /agents lists them consistently.
AGENT_DESCRIPTIONS: dict[str, str] = {
    "ceo": "Sets goals, delegates to the team, and plans overall strategy.",
    "sales": "Finds leads, writes outreach, and drives revenue.",
    "marketing": "Crafts campaigns, content, and launch messaging.",
    "support": "Writes helpful, empathetic customer replies.",
    "research": "Analyzes competitors, markets, and trends.",
    "finance": "Tracks burn rate, runway, and financial forecasts.",
    "hiring": "Screens candidates and drafts role requirements.",
}
VALID_AGENTS = set(AGENT_DESCRIPTIONS)

# Reuse the same orchestrator the API uses (stateless — just holds agent refs).
orchestrator = AgentOrchestrator()

# Simple in-memory state. chat_id -> linked email, chat_id -> active agent.
# (Fine for now; swap for a table if persistence across restarts is needed.)
_linked_emails: dict[int, str] = {}
_active_agents: dict[int, str] = {}


def clean_markdown(text: str) -> str:
    """Strip common markdown so agent replies read cleanly as plain Telegram text.

    Mirrors the frontend's cleanMarkdown: removes **bold**/__bold__, `code`,
    leading # headings, and normalizes bullet markers to "- ".
    """
    if not text:
        return text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[*+-]\s+', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'\*(\S[^*]*?\S|\S)\*', r'\1', text)
    return text.strip()


def _split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split a long reply into <=limit-char chunks, preferring line/word breaks."""
    text = text or "(no response)"
    chunks: list[str] = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await update.message.reply_text(
        "👋 Welcome to *AgentForge* — an AI-powered multi-agent startup OS.\n\n"
        "A team of seven specialist agents (CEO, Sales, Marketing, Support, "
        "Research, Finance, Hiring) can take on tasks for you right here in "
        "Telegram.\n\n"
        "To get started, link your email so your chats sync with your "
        "AgentForge dashboard and history:\n"
        "`/link you@example.com`\n\n"
        "Then just send a message and an agent will handle it.\n"
        "• /agents — see the team\n"
        "• /agent <name> — switch your active agent",
        parse_mode="Markdown",
    )


async def link_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text(
            "Usage: /link your@email.com"
        )
        return

    email = context.args[0].strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        await update.message.reply_text(
            "That doesn't look like a valid email. Try: /link your@email.com"
        )
        return

    _linked_emails[chat_id] = email
    active = _active_agents.setdefault(chat_id, DEFAULT_AGENT)
    await update.message.reply_text(
        f"✅ Linked to {email}.\n"
        f"Your active agent is '{active}'. Send a message to get started, "
        f"or use /agents to see the team."
    )


async def agents_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    lines = ["*Available agents:*"]
    for name, desc in AGENT_DESCRIPTIONS.items():
        lines.append(f"• `{name}` — {desc}")
    lines.append("\nSwitch with /agent <name>.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def agent_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        current = _active_agents.get(chat_id, DEFAULT_AGENT)
        await update.message.reply_text(
            f"Your active agent is '{current}'. "
            f"Use /agent <name> to change it, or /agents to see the list."
        )
        return

    name = context.args[0].strip().lower()
    if name not in VALID_AGENTS:
        await update.message.reply_text(
            f"❌ '{name}' is not a valid agent.\n"
            f"Choose one of: {', '.join(AGENT_DESCRIPTIONS)}."
        )
        return

    _active_agents[chat_id] = name
    await update.message.reply_text(f"✅ Active agent set to '{name}'.")


async def message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    email = _linked_emails.get(chat_id)
    if not email:
        await update.message.reply_text(
            "Please link your email first so I can save your work:\n"
            "/link your@email.com"
        )
        return

    task = (update.message.text or "").strip()
    if not task:
        return

    agent_name = _active_agents.get(chat_id, DEFAULT_AGENT)

    # Show a "typing…" indicator while the LLM works (a few seconds).
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.TYPING
        )
    except Exception:  # noqa: BLE001 - non-fatal, just a UX nicety
        pass

    try:
        if agent_name == "ceo":
            result = await orchestrator.run_with_delegation(
                agent_name, task, email
            )
            reply = result.get("primary_response") or "(no response)"
        else:
            reply = await orchestrator.run_agent(agent_name, task, email)
    except Exception:  # noqa: BLE001 - surface a friendly message, don't crash
        logger.exception("Telegram agent run failed for %s", agent_name)
        await update.message.reply_text(
            "⚠️ Sorry, the agent hit an error (it may be rate-limited or busy "
            "right now). Please try again in a moment."
        )
        return

    # Strip markdown once on the full reply (before splitting) so patterns that
    # span chunk boundaries aren't broken, then send each chunk as plain text.
    reply = clean_markdown(reply)
    for chunk in _split_message(reply):
        await update.message.reply_text(chunk)


async def start_telegram_bot() -> None:
    """Run the Telegram bot's polling loop as an asyncio background task.

    Designed to be launched with asyncio.create_task() from the FastAPI
    lifespan. Cancelling the task cleanly stops polling and shuts the bot down.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set; Telegram bot will not start."
        )
        return

    application = ApplicationBuilder().token(token).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("link", link_command))
    application.add_handler(CommandHandler("agents", agents_command))
    application.add_handler(CommandHandler("agent", agent_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
    )

    logger.info("Starting Telegram bot polling…")
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)

    try:
        # Block forever until the surrounding task is cancelled on shutdown.
        await asyncio.Event().wait()
    finally:
        logger.info("Stopping Telegram bot…")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
