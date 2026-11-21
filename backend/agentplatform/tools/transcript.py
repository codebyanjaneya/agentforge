"""Serialising the agent's conversation so a SUSPENDED task can be resumed by any process (M4-C finalization).

Only what the resumed tool loop needs: the original task text and the messages so far (role, content, the tool calls an assistant made, the id a tool result
answers). Every string is passed through the platform's secret redaction BEFORE it is stored, so a credential that appeared in a prompt, a tool argument or a
tool result is never persisted in the clear; a resumed model sees ``[redacted]`` where the secret was. Parsing is strict and bounded: a stored transcript that does
not parse as exactly this shape is refused (the task then fails closed instead of running on a corrupt conversation)."""

import json

from agentplatform.providers import Message, ToolCall
from agentplatform.schema import redact_secrets

MAX_TRANSCRIPT_CHARS = 200_000


class TranscriptError(ValueError):
    pass


def dump(task: str, messages) -> str:
    rows = []
    for m in messages:
        row = {"r": m.role, "c": redact_secrets(m.content)}
        if m.tool_calls:
            row["t"] = [[c.id, c.name, redact_secrets(c.arguments)] for c in m.tool_calls]
        if m.tool_call_id is not None:
            row["i"] = m.tool_call_id
        rows.append(row)
    text = json.dumps({"task": redact_secrets(task), "messages": rows}, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise TranscriptError("transcript too large")
    return text


def load(text: str) -> tuple:
    """(task, tuple of Message) or ``TranscriptError``."""
    try:
        data = json.loads(text)
        task, rows = data["task"], data["messages"]
        if not isinstance(task, str) or not isinstance(rows, list):
            raise TypeError
        out = []
        for row in rows:
            role, content = row["r"], row["c"]
            if role not in ("user", "assistant", "tool") or not isinstance(content, str):
                raise TypeError
            calls = tuple(ToolCall(str(i), str(n), str(a)) for i, n, a in row.get("t", ()))
            out.append(Message(role, content, tool_calls=calls, tool_call_id=row.get("i")))
        return task, tuple(out)
    except (KeyError, TypeError, ValueError):
        raise TranscriptError("corrupt transcript") from None


def dump_calls(calls) -> str:
    return json.dumps([[c.id, c.name, redact_secrets(c.arguments)] for c in calls], ensure_ascii=False, separators=(",", ":"))


def load_calls(text: str) -> tuple:
    try:
        return tuple(ToolCall(str(i), str(n), str(a)) for i, n, a in json.loads(text))
    except (TypeError, ValueError):
        raise TranscriptError("corrupt calls") from None
