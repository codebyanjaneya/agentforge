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
import base64
import logging
import os
import re
from datetime import datetime

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from memory.long_term import (
    LongTermMemory,
    get_link,
    set_active_agent,
    set_email,
)
from orchestrator import AgentOrchestrator
from voice_handler import transcribe_audio

load_dotenv()

logger = logging.getLogger("uvicorn.error")

# Public web dashboard — shown in /start so Telegram users can find the UI.
DASHBOARD_URL = "https://agentforge-omega-topaz.vercel.app"

# Telegram rejects messages longer than 4096 characters.
TELEGRAM_MAX_CHARS = 4096

# Recent-history command: how many entries to show and per-entry summary cap.
RECENT_HISTORY_LIMIT = 5
HISTORY_SUMMARY_CHARS = 300

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

# One emoji per agent, used in inline buttons and history listings.
AGENT_EMOJIS: dict[str, str] = {
    "ceo": "👔",
    "sales": "💼",
    "marketing": "📣",
    "support": "🎧",
    "research": "🔬",
    "finance": "💰",
    "hiring": "🧑‍💼",
}

# Reuse the same orchestrator the API uses (stateless — just holds agent refs).
orchestrator = AgentOrchestrator()

# Same long-term store the GET /history/{user_email} endpoint reads from.
long_term = LongTermMemory()


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


def _looks_like_email(value: str) -> bool:
    """Loose email sanity check (good enough for linking)."""
    return "@" in value and "." in value.split("@")[-1]


def _decode_link_token(token: str) -> str | None:
    """Decode a url-safe base64 deep-link token back into an email.

    The web dashboard builds the deep link as
    https://t.me/agentforge_official_bot?start=<token> where token is the
    url-safe base64 encoding of the user's email. Returns the email if it
    decodes to something that looks valid, else None.
    """
    try:
        padding = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    decoded = decoded.strip()
    return decoded if _looks_like_email(decoded) else None


def _relative_time(iso_str: str) -> str:
    """Render a stored (naive UTC) ISO timestamp as e.g. '2 hours ago'."""
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return ""
    seconds = (datetime.utcnow() - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        n = int(minutes)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    hours = minutes / 60
    if hours < 24:
        n = int(hours)
        return f"{n} hour{'s' if n != 1 else ''} ago"
    n = int(hours / 24)
    return f"{n} day{'s' if n != 1 else ''} ago"


def _agent_keyboard() -> InlineKeyboardMarkup:
    """Inline keyboard with one button per agent, two per row."""
    buttons = [
        InlineKeyboardButton(
            f"{AGENT_EMOJIS[name]} {name.capitalize()}",
            callback_data=f"setagent:{name}",
        )
        for name in AGENT_DESCRIPTIONS
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def _process_task(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    email: str,
    agent_name: str,
    task: str,
) -> None:
    """Run the given agent on `task` and send back the cleaned reply.

    Shared by the text and voice handlers so both go through identical
    dispatch, error handling, and markdown cleanup.
    """
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


async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id

    # Deep link from the web dashboard: /start <base64(email)> auto-links.
    if context.args:
        email = _decode_link_token(context.args[0].strip())
        if email:
            await asyncio.to_thread(set_email, chat_id, email)
            await update.message.reply_text(
                f"✅ Linked to {email} straight from your AgentForge "
                f"dashboard.\n\n"
                f"You're all set — send me a task, or tap /agents to pick a "
                f"specialist."
            )
            return

    await update.message.reply_text(
        "👋 Welcome to *AgentForge* — an AI-powered multi-agent startup OS.\n\n"
        "A team of seven specialist agents (CEO, Sales, Marketing, Support, "
        "Research, Finance, Hiring) can take on tasks for you right here in "
        "Telegram.\n\n"
        "To get started, link your email so your chats sync with your "
        "AgentForge dashboard and history:\n"
        "`/link you@example.com`\n\n"
        "Then just send a message (or a voice note) and an agent will handle "
        "it.\n"
        "• /agents — see the team and switch with one tap\n"
        "• /agent <name> — switch your active agent\n"
        "• /recenthistory — your last few tasks\n\n"
        f"🌐 Open your dashboard: {DASHBOARD_URL}",
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
    if not _looks_like_email(email):
        await update.message.reply_text(
            "That doesn't look like a valid email. Try: /link your@email.com"
        )
        return

    await asyncio.to_thread(set_email, chat_id, email)
    link = await asyncio.to_thread(get_link, chat_id)
    active = (link.get("active_agent") if link else None) or DEFAULT_AGENT
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
        lines.append(f"{AGENT_EMOJIS[name]} `{name}` — {desc}")
    lines.append("\nTap a button below to switch, or use /agent <name>.")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=_agent_keyboard(),
    )


async def agent_button_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline-keyboard agent selection from /agents."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("setagent:"):
        return

    name = data.split(":", 1)[1].lower()
    if name not in VALID_AGENTS:
        await query.edit_message_text("❌ Unknown agent.")
        return

    chat_id = query.message.chat.id
    await asyncio.to_thread(set_active_agent, chat_id, name)
    await query.edit_message_text(
        f"✅ Active agent set to {AGENT_EMOJIS[name]} {name.capitalize()}.\n"
        f"{AGENT_DESCRIPTIONS[name]}"
    )



async def agent_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        link = await asyncio.to_thread(get_link, chat_id)
        current = (link.get("active_agent") if link else None) or DEFAULT_AGENT
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

    await asyncio.to_thread(set_active_agent, chat_id, name)
    await update.message.reply_text(f"✅ Active agent set to '{name}'.")


async def message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    chat_id = update.effective_chat.id
    link = await asyncio.to_thread(get_link, chat_id)
    email = link["email"] if link else None
    if not email:
        await update.message.reply_text(
            "Please link your email first so I can save your work:\n"
            "/link your@email.com"
        )
        return

    task = (update.message.text or "").strip()
    if not task:
        return

    agent_name = (link.get("active_agent") if link else None) or DEFAULT_AGENT
    await _process_task(update, context, chat_id, email, agent_name, task)


async def voice_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Transcribe a voice/audio message with Groq Whisper, then run it as a task."""
    chat_id = update.effective_chat.id
    link = await asyncio.to_thread(get_link, chat_id)
    email = link["email"] if link else None
    if not email:
        await update.message.reply_text(
            "Please link your email first so I can save your work:\n"
            "/link your@email.com"
        )
        return

    media = update.message.voice or update.message.audio
    if media is None:
        return

    # Telegram has no "transcribing" action; TYPING is the closest indicator.
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.TYPING
        )
    except Exception:  # noqa: BLE001
        pass

    try:
        tg_file = await media.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception:  # noqa: BLE001
        logger.exception("Failed to download Telegram voice message")
        await update.message.reply_text(
            "⚠️ Sorry, I couldn't download that voice message. Please try again."
        )
        return

    try:
        transcript = await transcribe_audio(audio_bytes, "voice.ogg")
    except Exception:  # noqa: BLE001
        logger.exception("Telegram voice transcription failed")
        await update.message.reply_text(
            "⚠️ Sorry, I couldn't transcribe that audio. Please try again."
        )
        return

    if not transcript:
        await update.message.reply_text(
            "🤔 I couldn't make out any words in that audio. Mind trying again?"
        )
        return

    # Echo back what was heard so the user can confirm, then run it as a task.
    await update.message.reply_text(f"🎙️ I heard: {transcript}")
    agent_name = (link.get("active_agent") if link else None) or DEFAULT_AGENT
    await _process_task(update, context, chat_id, email, agent_name, transcript)


async def recent_history_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send the linked user's most recent history entries."""
    chat_id = update.effective_chat.id
    link = await asyncio.to_thread(get_link, chat_id)
    email = link["email"] if link else None
    if not email:
        await update.message.reply_text(
            "Please link your email first so I can pull your history:\n"
            "/link your@email.com"
        )
        return

    try:
        # get_history is a blocking DB call; offload it from the event loop.
        history = await asyncio.to_thread(long_term.get_history, email)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fetch history for %s", email)
        await update.message.reply_text(
            "⚠️ Sorry, I couldn't fetch your history right now. Try again soon."
        )
        return

    if not history:
        await update.message.reply_text(
            "No history yet — send me a task to get started!"
        )
        return

    await update.message.reply_text(
        "🕘 *Your recent history:*", parse_mode="Markdown"
    )
    for entry in history[:RECENT_HISTORY_LIMIT]:
        agent = entry.get("agent_name", "agent")
        emoji = AGENT_EMOJIS.get(agent, "🤖")
        when = _relative_time(entry.get("created_at", ""))
        summary = clean_markdown(entry.get("summary", "") or "")
        if len(summary) > HISTORY_SUMMARY_CHARS:
            summary = summary[:HISTORY_SUMMARY_CHARS].rstrip() + "..."

        header = f"{emoji} {agent.capitalize()}"
        if when:
            header += f" · {when}"
        await update.message.reply_text(f"{header}\n{summary or '(no summary)'}")


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
        CommandHandler("recenthistory", recent_history_command)
    )
    application.add_handler(
        CallbackQueryHandler(agent_button_callback, pattern=r"^setagent:")
    )
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, voice_message_handler)
    )
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
