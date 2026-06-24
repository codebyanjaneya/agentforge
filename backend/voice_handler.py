import os
from xml.sax.saxutils import escape

import httpx
from dotenv import load_dotenv
from groq import AsyncGroq

load_dotenv()

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")

WHISPER_MODEL = "whisper-large-v3"
MAX_SAY_CHARS = 200

# Inbound phone calls have no logged-in user; attribute their memory to a
# stable placeholder account so it stays separated from real users.
VOICE_USER_EMAIL = "voice-call@agentforge.local"

_groq = AsyncGroq(api_key=GROQ_API_KEY)


def build_incoming_response() -> str:
    """TwiML played when a call comes in: prompt + record, callback to /voice/process."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Say>Welcome to Agent Forge. Please describe your task after the beep, "
        "then press the pound key.</Say>"
        '<Record action="/voice/process" method="POST" maxLength="30" '
        'playBeep="true" finishOnKey="#" timeout="5" />'
        "<Say>We did not receive a recording. Goodbye.</Say>"
        "</Response>"
    )


def _say_response(text: str) -> str:
    safe = escape(text)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{safe}</Say>"
        "</Response>"
    )


async def transcribe_audio(audio_bytes: bytes, filename: str = "audio.mp3") -> str:
    """Transcribe raw audio bytes with Groq Whisper and return the transcript.

    Shared by the Twilio voice flow and the Telegram voice-message handler so
    the Groq client/model setup lives in exactly one place. Raises on hard API
    errors; returns an empty string if Whisper produced no text.
    """
    transcription = await _groq.audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=WHISPER_MODEL,
    )
    return (transcription.text or "").strip()


async def process_recording(recording_url: str, orchestrator) -> str:
    """Download the Twilio recording, transcribe with Groq Whisper, run the CEO
    agent on the transcript, and return TwiML that reads back the response."""
    if not recording_url:
        return _say_response("Sorry, no recording was received. Goodbye.")

    # Twilio recording URLs have no extension; append .mp3 to get an audio file.
    audio_url = recording_url if recording_url.endswith(".mp3") else f"{recording_url}.mp3"

    try:
        async with httpx.AsyncClient(timeout=30) as http:
            audio_resp = await http.get(
                audio_url,
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            )
            audio_resp.raise_for_status()
            audio_bytes = audio_resp.content
    except httpx.HTTPError:
        return _say_response(
            "Sorry, I could not retrieve your recording. Please try again."
        )

    try:
        transcript = await transcribe_audio(audio_bytes, "recording.mp3")
    except Exception:  # noqa: BLE001
        return _say_response(
            "Sorry, I could not understand the audio. Please try again."
        )

    if not transcript:
        return _say_response("I did not catch that. Please call again. Goodbye.")

    result = await orchestrator.run_with_delegation(
        "ceo", transcript, VOICE_USER_EMAIL
    )
    primary = result.get("primary_response", "") or "No response was generated."

    if len(primary) > MAX_SAY_CHARS:
        primary = primary[:MAX_SAY_CHARS].rsplit(" ", 1)[0] + "..."

    return _say_response(primary)
