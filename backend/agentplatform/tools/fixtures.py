"""A DISPOSABLE, SIMULATED side-effecting tool for demonstrating and testing human approval (M4-C).

This is NOT an integration. ``demo.send_note`` sends nothing anywhere: its handler only returns a deterministic receipt and appends the call to an
in-memory list (``OUTBOX``) so tests can prove how many times it was dispatched. It is declared ``external`` / ``non_idempotent`` so the policy treats
it as a sensitive action that needs a human approval for every call. It is NOT part of the default registry: a process opts in explicitly by calling
``register_demo_tools`` (the test-suite and the browser-verification harness do; production does not)."""

import hashlib

from pydantic import BaseModel, ConfigDict, Field

from agentplatform.tools.registry import ToolRegistry, ToolSpec

OUTBOX: list = []  # every dispatch of the simulated tool, in order (process-local, for tests and the demo harness)


class SendNoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=3, max_length=80, description="Who the note is for (simulated: nobody receives anything)")
    subject: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=600)


class SendNoteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    simulated: bool
    delivered: bool
    receipt: str


REMOTE: dict = {}  # the SIMULATED remote service: idempotency key -> receipt (it deduplicates by key and can be queried, like a well-behaved real API)


def send_note(args: SendNoteIn, dispatch=None) -> SendNoteOut:
    receipt = hashlib.sha256(f"{args.recipient}|{args.subject}|{args.body}".encode()).hexdigest()[:12]
    key = getattr(dispatch, "idempotency_key", None)
    if key is not None and key in REMOTE:  # the simulated remote has already accepted this key: a repeat changes nothing
        return SendNoteOut(simulated=True, delivered=False, receipt=REMOTE[key])
    OUTBOX.append((args.recipient, args.subject, args.body))
    if key is not None:
        REMOTE[key] = receipt
    return SendNoteOut(simulated=True, delivered=False, receipt=receipt)


def lookup_note(idempotency_key: str) -> tuple:
    """Reconciliation: ask the simulated remote whether it accepted this idempotency key. (status, external reference, evidence)."""
    if idempotency_key in REMOTE:
        return "succeeded", REMOTE[idempotency_key], "The simulated remote service has a record of this idempotency key."
    return "not_found", None, "The simulated remote service has no record of this idempotency key."


DEMO_SEND_NOTE = ToolSpec(
    id="demo.send_note", version=1, title="Send a note (simulated)", category="Demo",
    summary="Pretends to send a note to a person. Simulated: nothing is sent anywhere.",
    description="A disposable demonstration of a sensitive action. It stands in for a tool that would contact someone or change something outside AgentForge, so every call "
                "needs a human approval of that exact call. It performs no real delivery and has no network access; it only returns a deterministic receipt.",
    input_model=SendNoteIn, output_model=SendNoteOut, risk="external", side_effect="non_idempotent", timeout_seconds=2.0, max_output_bytes=1000,
    capabilities=("approval-demo",), retry_policy="never", remote_idempotency=True, external_ref_field="receipt",
    delivery="SIMULATED. AgentForge sends at most once automatically; the simulated remote also deduplicates by idempotency key and can be queried, so an unknown outcome can be reconciled from its record.",
    limitations=("SIMULATED: nothing is delivered and nothing leaves AgentForge.", "A real tool of this kind could not be undone once it ran.", "Needs a human approval for every single call."),
    example={"recipient": "customer@example.com", "subject": "Your launch date", "body": "Hello, the launch is confirmed for next week."})


def register_demo_tools(registry: ToolRegistry) -> None:
    registry.register(DEMO_SEND_NOTE, send_note, lookup=lookup_note, takes_dispatch=True)
