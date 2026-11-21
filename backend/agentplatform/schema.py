"""Validated agent definition (M2-A).

A definition is DATA only: plain text and a few bounded options. There is no templating, no code, no URLs to fetch and
no credentials. Unknown fields are rejected everywhere (``extra="forbid"``), so a client cannot smuggle extra
configuration through. Probable secrets (API keys, private keys, bearer tokens, password assignments) are refused with a
message that never echoes the value.

Provider/model: the codebase has no provider abstraction yet (D005 is not implemented; every agent builds its own Groq
client), so ``model.provider`` is restricted to the one supported value and is configuration data for the future runtime.
Tools (M4-B, D014): ``allowed_tools`` names tools of the code-defined registry (``agentplatform.tools``), as ``"id"`` or ``"id@version"``.
On save each entry is normalised to the PINNED ``"id@version"`` of an executable tool, so an immutable agent version always names the exact
tool versions it may use; an unknown, disabled or not-yet-allowed tool is rejected rather than silently accepted and unenforced.
"""

import hashlib
import json
import re
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator

DEFAULT_MODEL = "openai/gpt-oss-120b"  # a model the provider currently serves with tool calling (llama-3.3-70b-versatile was retired; verified live 2026-11-19)
SUPPORTED_PROVIDERS = ("groq",)

BUILTIN_AGENT_KEYS = ("ceo", "sales", "marketing", "support", "research", "finance", "hiring")
RESERVED_SLUGS = frozenset(BUILTIN_AGENT_KEYS) | {"agent", "agents", "new", "default", "system", "admin", "template", "templates", "me", "api"}
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,38}[a-z0-9]$")  # 3-40 characters

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # everything except tab and newline
_LABEL = re.compile(r"^[a-z][a-z0-9 _-]{1,39}$")
_META_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_META_KEY_DENY = re.compile(r"(pass(word|wd)?|secret|token|credential|api[_.-]?key|private[_.-]?key|auth)", re.I)
_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

# Conservative heuristics (not a guarantee): obvious credential shapes are refused.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|gsk|pk|rk)[-_][A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token|auth[_-]?token)\s*[:=]\s*[\"']?[A-Za-z0-9/+_.-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@"),  # credentials inside a URL
)


def _text(value: Any, *, minimum: int, maximum: int, multiline: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("must be text")
    value = unicodedata.normalize("NFC", value).strip()
    if _CONTROL.search(value):
        raise ValueError("contains control characters")
    if not multiline and ("\n" in value or "\t" in value):
        raise ValueError("must be a single line")
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"must be {minimum}-{maximum} characters")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            raise ValueError("looks like a credential; remove it (definitions must not contain secrets)")
    return value


clean_text = _text  # the shared text validator (bounds, control characters, probable secrets) for other modules


def redact_secrets(value: str) -> str:
    """Copy of ``value`` with every probable credential replaced by ``[redacted]`` (used for what is stored about a task)."""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[redacted]", value)
    return value


def validate_slug(value: Any) -> str:
    if not isinstance(value, str) or not SLUG_PATTERN.fullmatch(value):
        raise ValueError("slug must be 3-40 characters: lowercase letters, digits and hyphens, starting with a letter")
    if "--" in value:
        raise ValueError("slug must not contain consecutive hyphens")
    if value in RESERVED_SLUGS:
        raise ValueError("slug is reserved")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelConfig(_Strict):
    provider: Literal["groq"]
    name: str
    temperature: StrictFloat | StrictInt = 0.7
    max_tokens: StrictInt | None = Field(default=None, ge=1, le=8192)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _MODEL_NAME.fullmatch(value):
            raise ValueError("invalid model name")
        return value

    @field_validator("temperature")
    @classmethod
    def _temperature(cls, value: float) -> float:
        if isinstance(value, bool) or not 0 <= value <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return float(value)


class AgentDefinition(_Strict):
    name: str
    description: str
    role: str
    purpose: str
    instructions: str
    capabilities: list[str] = Field(default_factory=list, max_length=12)
    allowed_tools: list[str] = Field(default_factory=list, max_length=8)
    input_expectations: str = ""
    output_expectations: str = ""
    model: ModelConfig
    metadata: dict[str, str] = Field(default_factory=dict, max_length=10)

    @field_validator("name")
    @classmethod
    def _name(cls, value: Any) -> str:
        return _text(value, minimum=2, maximum=80)

    @field_validator("description", "purpose")
    @classmethod
    def _short(cls, value: Any) -> str:
        return _text(value, minimum=10, maximum=500, multiline=True)

    @field_validator("role")
    @classmethod
    def _role(cls, value: Any) -> str:
        return _text(value, minimum=2, maximum=120)

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: Any) -> str:
        return _text(value, minimum=20, maximum=8000, multiline=True)

    @field_validator("input_expectations", "output_expectations")
    @classmethod
    def _expectations(cls, value: Any) -> str:
        return _text(value, minimum=0, maximum=1000, multiline=True)

    @field_validator("capabilities")
    @classmethod
    def _capabilities(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            label = _text(value, minimum=2, maximum=40).lower()
            if not _LABEL.fullmatch(label):
                raise ValueError("a capability is a short lowercase label (letters, digits, space, hyphen, underscore)")
            cleaned.append(label)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("capabilities must be unique")
        return cleaned

    @field_validator("allowed_tools")
    @classmethod
    def _tools(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("allowed_tools must be unique")
        from agentplatform.tools import default_registry  # imported here: the tools package imports this module

        pinned = []
        for value in values:
            try:
                pinned.append(default_registry().pin(value))  # "id" or "id@version" -> the exact, executable "id@version" (an agent version PINS its tools)
            except ValueError as exc:
                raise ValueError(str(exc)) from None
        if len(set(pinned)) != len(pinned) or len({p.partition("@")[0] for p in pinned}) != len(pinned):
            raise ValueError("allowed_tools must be unique")
        return pinned

    @field_validator("metadata")
    @classmethod
    def _metadata(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {}
        for key, item in value.items():
            if not _META_KEY.fullmatch(key) or _META_KEY_DENY.search(key):
                raise ValueError("invalid metadata key (credential-like names are not allowed)")
            cleaned[key] = _text(item, minimum=0, maximum=200)
        return cleaned


def canonical(definition: AgentDefinition) -> dict:
    """The validated definition as plain JSON-able data (what is stored)."""
    return definition.model_dump(mode="json")


def content_hash(definition: AgentDefinition | dict) -> str:
    data = canonical(definition) if isinstance(definition, AgentDefinition) else definition
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def parse_definition(data: Any) -> AgentDefinition:
    """Validate stored or submitted data (used when reading versions back, so a corrupted row cannot be served)."""
    return AgentDefinition.model_validate(data)


NoteText = Annotated[str, Field(max_length=200)]


def clean_note(value: str | None) -> str | None:
    if value is None:
        return None
    value = _text(value, minimum=0, maximum=200)
    return value or None
