"""The versioned tool registry (M4-B): trusted, code-defined tools and the policy that decides which of them may run.

WHAT A TOOL IS. A ``ToolSpec`` (stable id ``namespace.name``, integer ``version``, human title/summary/description, strict pydantic
INPUT and OUTPUT models, risk class, side-effect class, timeout, output cap, lifecycle) plus a handler: an ordinary function written in
this repository and registered at import time by trusted code. Nothing a user, a template, an agent definition or a model produces can
create, replace or extend a tool: a model can only REQUEST a registered tool by name, with arguments that are validated against the input
model. There is no ``eval``/``exec``/shell/dynamic import anywhere in this package, and no handler accepts code, a shell command, a file
path or a URL.

VERSIONS ARE IMMUTABLE FACTS. ``(id, version)`` identifies one input/output contract and one implementation. A change that alters the
contract or the behaviour is a NEW version; an old version can be ``deprecated`` (still executable, flagged) or ``disabled`` (never
executable) but is never edited into something else. An agent definition pins ``id@version`` when it is saved, so a later tool release
cannot silently change what an approved agent version can do.

THE POLICY BOUNDARY (the seam M4-C builds on). ``ToolPolicy.decide(spec)`` returns ``allow``, ``requires_approval`` or ``deny``. In M4-B
only ``risk == "read"`` with ``side_effect == "none"`` is ``allow``; every other combination is ``requires_approval`` (a human approval gate
does not exist yet, so the runtime treats it as refused and records ``approval_required``). Plan approval is NOT permission to run a
side-effecting tool. Every invocation record stores the tool id and version, the canonical validated arguments' hash, the execution and
the agent, which is exactly what an approval will have to be bound to.
"""

import re
import hashlib
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel

TOOL_ID = re.compile(r"^[a-z][a-z0-9]{1,23}\.[a-z][a-z0-9_]{1,31}$")
RISKS = ("read", "write", "external")
SIDE_EFFECTS = ("none", "idempotent", "non_idempotent")
LIFECYCLES = ("active", "deprecated", "disabled")
MAX_TOOLS_PER_AGENT = 8

# code -> fixed, user-safe text. Tool failures never carry exception text, argument values or output.
TOOL_ERRORS = {
    "invalid_input": "The tool arguments did not match the tool's input schema.",
    "unknown_tool": "That tool does not exist.",
    "not_permitted": "This agent is not permitted to use that tool.",
    "tool_unavailable": "That tool version is not available.",
    "approval_required": "This tool needs a human approval that is not available here, so it was not run.",
    "approval_rejected": "A human rejected this action, so it was not run.",
    "approval_expired": "No human approved this action in time, so it was not run.",
    "approval_invalid": "The approval no longer matches this exact action, so it was not run.",
    "timeout": "The tool did not finish within its time limit; no result was used.",
    "tool_error": "The tool could not complete the request.",
    "output_invalid": "The tool produced an unusable result.",
    "output_too_large": "The tool result was too large and was discarded.",
    "limit_exceeded": "The agent has reached its limit of tool calls for this task.",
    "duplicate_call": "This tool call was already dispatched.",
    "cancelled": "The tool call was cancelled before it finished.",
    "interrupted": "The server stopped before this tool call finished; no result was recorded.",
    "uncertain": "The outcome of this tool call is unknown: it may or may not have taken effect, so it will not be sent again automatically.",
    "not_dispatched": "The server stopped before this action was sent, so it was not performed.",
    "operation_in_flight": "An earlier attempt at this exact action may still be in progress, so it was not sent again.",
    "outcome_unknown": "An earlier attempt at this action has an unknown outcome and needs to be reconciled first, so it was not sent again.",
    "retry_not_permitted": "This action failed earlier and its tool does not allow an automatic retry, so it was not sent again.",
}


class ToolError(Exception):
    """A tool call was refused or failed; ``code`` is one of ``TOOL_ERRORS`` and ``message`` is its fixed text."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(TOOL_ERRORS[code])
        self.code, self.message, self.detail = code, TOOL_ERRORS[code], detail  # ``detail`` is safe structured text (field paths only)


class ToolInputError(ValueError):
    """Raised BY A TOOL when well-typed arguments are semantically unusable (a division by zero, an end date before the start date). The
    message is written by the tool's author and must never contain argument values; it becomes the ``invalid_input`` detail."""


@dataclass(frozen=True)
class ToolSpec:
    id: str
    version: int
    title: str
    summary: str  # one line, shown in lists and offered to the model
    description: str  # what it really does, for the user
    category: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: str = "read"
    side_effect: str = "none"
    timeout_seconds: float = 5.0
    max_output_bytes: int = 8000
    capabilities: tuple = ()
    limitations: tuple = ()  # honest list of what the tool does NOT do
    example: dict = field(default_factory=dict)  # a real, valid example of the input
    lifecycle: str = "active"
    # P0 safety sprint (side-effecting tools only):
    retry_policy: str = "never"  # "after_confirmed_failure": a CONFIRMED failure may be retried automatically; "never": it needs a person. An unknown outcome is never retried automatically.
    remote_idempotency: bool = False  # the remote service honours the idempotency key we send (a repeat is harmless there). False: AgentForge guarantees at most one automatic dispatch, not exactly-once.
    delivery: str = ""  # the delivery guarantee in plain words, shown to people and in the docs
    external_ref_field: str = ""  # the output field that holds the remote operation reference (a receipt, an id), recorded with the dispatch

    def __post_init__(self):
        if not TOOL_ID.fullmatch(self.id):
            raise ValueError("tool id must look like 'namespace.name'")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("tool version must be an integer >= 1")
        if self.risk not in RISKS or self.side_effect not in SIDE_EFFECTS or self.lifecycle not in LIFECYCLES:
            raise ValueError("unknown risk, side effect or lifecycle")
        for model in (self.input_model, self.output_model):
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise ValueError("input and output must be pydantic models")
        if self.retry_policy not in ("never", "after_confirmed_failure"):
            raise ValueError("unknown retry policy")
        if self.input_model.model_config.get("extra") != "forbid":
            raise ValueError("a tool input model must forbid unknown fields (extra='forbid')")
        if not 0 < self.timeout_seconds <= 30 or not 0 < self.max_output_bytes <= 64000:
            raise ValueError("timeout and output limits are out of range")

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def wire_name(self) -> str:
        """The function name offered to a model provider (providers accept letters, digits, '_' and '-': no '.' or '@')."""
        return f"{self.id.replace('.', '__')}__v{self.version}"

    def input_schema(self) -> dict:
        return self.input_model.model_json_schema()

    def output_schema(self) -> dict:
        return self.output_model.model_json_schema()


@dataclass(frozen=True)
class ToolPolicy:
    """The server-controlled execution policy (M4-C finalization). It is an ALLOWLIST: only the EXACT tool versions named in ``auto_run`` (``"id@version"``) may
    run without a human approval, and only while their declared class is still read-only with no side effect (a belt-and-braces consistency check, never a
    grant). Everything else (an unlisted tool, an unlisted new VERSION of a listed tool, an unknown classification, a tool whose metadata claims to be safe but is
    not on the list) needs a human approval for every call. A tool's own metadata describes its risk; it can never grant itself permission, and nothing a model
    produces can change this object (it is code, reviewed and released like any other code). ``fingerprint`` identifies the list and is recorded in the approval
    events, so a change of policy is visible in the history."""

    auto_run: frozenset = frozenset()

    def decide(self, spec: ToolSpec) -> str:
        if spec.ref in self.auto_run and spec.risk == "read" and spec.side_effect == "none":
            return "allow"
        return "requires_approval"

    @property
    def fingerprint(self) -> str:
        try:
            refs = sorted(self.auto_run)
        except TypeError:
            return "custom"
        return hashlib.sha256("\n".join(refs).encode()).hexdigest()[:16]


class ToolRegistry:
    def __init__(self, policy: ToolPolicy | None = None, *, allow_gated: bool = False):
        self.policy = policy or ToolPolicy()
        self.allow_gated = allow_gated  # M4-C: may an agent be GRANTED a tool that needs a human approval to run (the approval itself is a separate, per-call check)
        self._tools: dict = {}  # (id, version) -> (spec, handler)
        self._lookups: dict = {}  # (id, version) -> lookup(idempotency_key) for reconciliation (optional)
        self._takes_dispatch: set = set()  # tools whose handler also receives the DispatchInfo (the idempotency key to send)

    def register(self, spec: ToolSpec, handler: Callable, *, lookup: Callable | None = None, takes_dispatch: bool = False) -> ToolSpec:
        if not callable(handler) or (lookup is not None and not callable(lookup)):
            raise ValueError("a tool handler must be a function")
        key = (spec.id, spec.version)
        if key in self._tools:
            raise ValueError(f"{spec.ref} is already registered: a changed tool is a NEW version")
        if (lookup is not None or takes_dispatch) and spec.side_effect == "none":
            raise ValueError("only a side-effecting tool has a dispatch protocol")
        self._tools[key] = (spec, handler)
        if lookup is not None:
            self._lookups[key] = lookup
        if takes_dispatch:
            self._takes_dispatch.add(key)
        return spec

    def lookup(self, spec: ToolSpec):
        """The tool's remote-operation lookup (``lookup(idempotency_key) -> (status, external_ref, evidence)``), or None when the remote service cannot be queried."""
        return self._lookups.get((spec.id, spec.version))

    def takes_dispatch(self, spec: ToolSpec) -> bool:
        return (spec.id, spec.version) in self._takes_dispatch

    def get(self, tool_id: str, version: int | None = None) -> ToolSpec | None:
        """One spec. Without a version: the newest ACTIVE version, else the newest deprecated one (never a disabled one)."""
        if version is not None:
            found = self._tools.get((tool_id, version))
            return found[0] if found else None
        versions = sorted((v for (i, v) in self._tools if i == tool_id), reverse=True)
        for wanted in ("active", "deprecated"):
            for v in versions:
                if self._tools[(tool_id, v)][0].lifecycle == wanted:
                    return self._tools[(tool_id, v)][0]
        return None

    def handler(self, spec: ToolSpec) -> Callable:
        return self._tools[(spec.id, spec.version)][1]

    def executable(self, spec: ToolSpec) -> bool:
        """May this exact version run now: not disabled and allowed by the policy (no approval path exists yet)."""
        return spec.lifecycle != "disabled" and self.policy.decide(spec) == "allow"

    def gated(self, spec: ToolSpec) -> bool:
        """Does this exact version need a human approval for EVERY call? Anything the policy does not explicitly allow does (fail closed)."""
        return spec.lifecycle != "disabled" and self.policy.decide(spec) != "allow"

    def list_specs(self, *, include_disabled: bool = True) -> list:
        specs = [s for s, _h in self._tools.values() if include_disabled or s.lifecycle != "disabled"]
        return sorted(specs, key=lambda s: (s.category, s.id, -s.version))

    def pin(self, reference: str) -> str:
        """``"id"`` or ``"id@version"`` -> the pinned, canonical ``"id@version"`` of an EXECUTABLE tool. Raises ``ValueError`` otherwise
        (used when an agent definition is saved: an unknown, disabled or not-yet-allowed tool cannot be granted)."""
        if not isinstance(reference, str) or len(reference) > 40:
            raise ValueError("unknown tool")
        tool_id, _, raw = reference.partition("@")
        if raw and not raw.isdigit():
            raise ValueError("unknown tool")
        spec = self.get(tool_id, int(raw)) if raw else self.get(tool_id)
        if spec is None:
            raise ValueError("unknown tool")
        if not (self.executable(spec) or (self.allow_gated and self.gated(spec))):
            raise ValueError("this tool is not available to agents")
        return spec.ref

    def resolve_pin(self, pinned: str) -> ToolSpec | None:
        """The exact spec of a pinned ``id@version`` (None if it is unknown). Executability is checked separately, at call time."""
        tool_id, _, raw = pinned.partition("@")
        return self.get(tool_id, int(raw)) if raw.isdigit() else None
