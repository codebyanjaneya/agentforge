"""Provider interface for agent execution (M2-B, D005): the smallest layer the runtime needs.

CONTRACT. ``Provider.complete(ProviderRequest) -> ProviderResponse`` is async, bounded by ``request.timeout_seconds`` and
makes exactly ONE upstream attempt: there are no hidden retries and no fallback to another model or provider (the Groq
SDK's own retries are switched off), so one call is one execution. Every failure is a ``ProviderError`` with a stable
code and a FIXED user-safe message; upstream text, request bodies and credentials are never put in an exception, a
log record or a stored record.

CREDENTIALS stay server-side: the Groq key comes from the validated settings and is only handed to the SDK client.
A provider is selected by name from the validated agent model configuration (``model.provider``); an unknown name is
``unsupported_provider``. Models are not allow-listed here (this layer cannot verify a provider's catalogue offline):
the provider's own "model not found" answer becomes the actionable ``unsupported_model`` error.

Only Groq is implemented. A new provider is one class with ``name`` and ``complete`` plus a registry entry.
"""

from dataclasses import dataclass, field
from typing import Callable, Protocol

DEFAULT_TIMEOUT_SECONDS = 60.0

# code -> (user-safe message, whether a later identical request could succeed). Messages are fixed text.
PROVIDER_ERRORS = {
    "timeout": ("The model provider took too long to answer.", True),
    "rate_limited": ("The model provider is rate-limiting requests. Please wait a moment and try again.", True),
    "unavailable": ("The model provider is currently unavailable. Please try again shortly.", True),
    "auth": ("The model provider rejected the server's credentials. This is a server configuration problem.", False),
    "unsupported_model": ("The agent's model is not available from the provider. Edit the agent and choose a supported model.", False),
    "unsupported_provider": ("The agent's model provider is not supported.", False),
    "bad_request": ("The model provider rejected the request.", False),
    "invalid_response": ("The model provider returned an unusable response.", False),
    "malformed_tool_call": ("The model produced a malformed tool call.", True),
    "required_action_missing": ("The agent did not perform the action this task requires.", False),
    "side_effect_outcome_unknown": ("An external action of this task has an unknown outcome; reconcile it before the task can count as done.", False),
    "side_effect_failed": ("An external action of this task failed.", False),
}


class ProviderError(Exception):
    def __init__(self, code: str, retry_after: float | None = None):
        message, retryable = PROVIDER_ERRORS[code]
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after  # seconds the provider asked us to wait (Retry-After), when it said so; never a guess


@dataclass(frozen=True)
class ToolDefinition:
    """A tool OFFERED to the model for one request (M4-B): a function name, a short description and a JSON schema of its arguments. The
    registry produces these from trusted specs; a provider only forwards them in its own wire format."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    """A tool the model REQUESTED. Untrusted input: the runtime re-validates the name against the agent's grants and the arguments against
    the tool's schema before anything runs. ``arguments`` is the raw JSON text exactly as the model produced it."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: tuple = ()  # assistant messages: the ToolCalls the model made
    tool_call_id: str | None = None  # tool messages: which call this result answers


@dataclass(frozen=True)
class ProviderRequest:
    model: str
    system: str
    messages: tuple
    temperature: float = 0.7
    max_tokens: int | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    tools: tuple = ()  # ToolDefinitions offered for this request (empty: a plain text request, exactly as before M4-B)
    tool_choice: str | None = None  # M6b: the wire name of ONE offered tool the model MUST call (a provider that cannot force it ignores this; the server still checks the result)


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None  # only when the provider reports it; never inferred


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    model: str
    finish_reason: str | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple = ()  # ToolCalls the model requested instead of (or before) a final text


class Provider(Protocol):
    name: str

    async def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class GroqProvider:
    """The existing provider (Groq chat completions), behind the interface."""

    name = "groq"

    def __init__(self, api_key: str | None = None, *, client=None, base_url: str | None = None, http_client=None):
        if client is None:
            from groq import AsyncGroq

            client = AsyncGroq(api_key=api_key, max_retries=0, base_url=base_url, http_client=http_client)  # max_retries=0: one attempt
        self._client = client

    async def complete(self, request: ProviderRequest) -> ProviderResponse:
        messages = [{"role": "system", "content": request.system}, *(_wire_message(m) for m in request.messages)]
        options = {"model": request.model, "messages": messages, "temperature": request.temperature, "timeout": request.timeout_seconds}
        if request.tools:
            options["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in request.tools]
            names = {t.name for t in request.tools}
            options["tool_choice"] = {"type": "function", "function": {"name": request.tool_choice}} if request.tool_choice in names else "auto"
        if request.max_tokens is not None:
            options["max_tokens"] = request.max_tokens
        try:
            response = await self._client.chat.completions.create(**options)
        except Exception as exc:  # noqa: BLE001 - every SDK failure becomes a fixed-code ProviderError
            code = provider_error_code(exc)
            if code is None:
                raise
            raise ProviderError(code, retry_after=retry_after_seconds(exc)) from None
        return _parse(response, request.model)


def _wire_message(m: Message) -> dict:
    """One message in the OpenAI-compatible chat format (the Groq wire format)."""
    if m.role == "assistant" and m.tool_calls:
        return {"role": "assistant", "content": m.content or None,
                "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls]}
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
    return {"role": m.role, "content": m.content}


def retry_after_seconds(exc: BaseException) -> float | None:
    """The provider's own ``Retry-After`` (seconds or an HTTP date) from an SDK error response, bounded to [0, 3600]; None when absent or unusable."""
    try:
        headers = exc.response.headers  # type: ignore[attr-defined]
        raw = headers.get("retry-after") if headers is not None else None
    except Exception:  # noqa: BLE001 - no response, odd object: no hint
        return None
    if raw is None:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime
            from datetime import datetime, timezone

            seconds = (parsedate_to_datetime(str(raw)) - datetime.now(timezone.utc)).total_seconds()
        except Exception:  # noqa: BLE001
            return None
    return min(max(seconds, 0.0), 3600.0) if seconds == seconds else None


def provider_error_code(exc: BaseException) -> str | None:
    """Map a Groq SDK exception to a ProviderError code (None for anything that is not a Groq SDK failure). Used by the
    adapter and by the runtime for the built-in agents, which call the SDK directly."""
    try:
        import groq
    except ImportError:  # pragma: no cover - the SDK is a pinned dependency
        return None
    if isinstance(exc, groq.APITimeoutError):
        return "timeout"
    if isinstance(exc, groq.RateLimitError):
        return "rate_limited"
    if isinstance(exc, groq.APIConnectionError):
        return "unavailable"
    if isinstance(exc, (groq.AuthenticationError, groq.PermissionDeniedError)):
        return "auth"
    if isinstance(exc, groq.NotFoundError):
        return "unsupported_model"
    if isinstance(exc, (groq.BadRequestError, groq.UnprocessableEntityError, groq.ConflictError)):
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        code = str(error.get("code", "")) if isinstance(error, dict) else ""
        # a model that emitted a tool call the provider could not parse ("tool_use_failed") is a sampling failure, not a bad request: a later identical request usually works
        return "unsupported_model" if code in ("model_not_found", "model_decommissioned") else "malformed_tool_call" if code == "tool_use_failed" else "bad_request"
    if isinstance(exc, groq.InternalServerError):
        return "unavailable"
    if isinstance(exc, groq.APIStatusError):
        return "rate_limited" if exc.status_code == 429 else "unavailable" if exc.status_code >= 500 else "bad_request"
    if isinstance(exc, groq.APIError):
        return "invalid_response"
    return None


def _parse(response, requested_model: str) -> ProviderResponse:
    """Validate the provider payload; anything unexpected is ``invalid_response`` (never a crash, never a guess)."""
    try:
        choice = response.choices[0]
        text = choice.message.content
        finish = getattr(choice, "finish_reason", None)
        raw_calls = getattr(choice.message, "tool_calls", None) or ()
        calls = tuple(ToolCall(id=str(c.id)[:64], name=str(c.function.name)[:100], arguments=c.function.arguments if isinstance(c.function.arguments, str) else "") for c in raw_calls)
    except (AttributeError, IndexError, TypeError):
        raise ProviderError("invalid_response") from None
    if calls:
        text = text if isinstance(text, str) else ""
    elif not isinstance(text, str) or not text.strip():
        raise ProviderError("invalid_response")
    usage = getattr(response, "usage", None)
    return ProviderResponse(
        text=text, tool_calls=calls,
        model=str(getattr(response, "model", None) or requested_model)[:100],
        finish_reason=finish if isinstance(finish, str) else None,
        usage=Usage(getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None), getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", None)) if usage is not None else Usage(),
    )


class ProviderRegistry:
    """Providers by name, created lazily (so the server can start, and tests can run, without credentials)."""

    def __init__(self, factories: dict[str, Callable[[], Provider]]):
        self._factories = dict(factories)
        self._instances: dict[str, Provider] = {}

    def get(self, name: str) -> Provider:
        if name not in self._factories:
            raise ProviderError("unsupported_provider")
        if name not in self._instances:
            self._instances[name] = self._factories[name]()
        return self._instances[name]


def default_registry() -> ProviderRegistry:
    def groq_factory() -> Provider:
        from config import get_settings

        return GroqProvider(get_settings().groq_api_key.get_secret_value())

    return ProviderRegistry({"groq": groq_factory})
