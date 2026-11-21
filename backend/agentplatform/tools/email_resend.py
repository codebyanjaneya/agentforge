"""``email.send@1``: AgentForge's first REAL side-effecting integration (sprint 4): sends one plain-text email through the Resend API (https://resend.com/docs/api-reference/emails/send-email).

WHAT THE PROVIDER DOCUMENTS (read from resend.com/docs on 2026-11-20; nothing below is assumed):
* ``POST https://api.resend.com/emails`` with ``Authorization: Bearer <key>``; body ``from``, ``to``, ``subject``, ``text``; success returns ``{"id": "<uuid>"}``.
* ``Idempotency-Key`` HTTP header: 1-256 characters, unique per API request, kept for 24 HOURS. The same key with the SAME payload returns the original response without sending again. The same key with a
  DIFFERENT payload fails with ``409 invalid_idempotent_request``; a key too short/long is ``400 invalid_idempotency_key``; a request while another with the same key is in progress is
  ``409 concurrent_idempotent_requests`` ("safe to retry later").
* ``GET /emails/{id}`` returns the email including ``last_event``. NOT documented (so not relied on): any lookup BY idempotency key, the possible ``last_event`` values on the page we read, and what a missing id returns.
* Test addresses ``delivered@resend.dev``, ``bounced@resend.dev``, ``complained@resend.dev``, ``suppressed@resend.dev`` simulate outcomes without hurting domain reputation (they still count against quota).

THE GUARANTEE AGENTFORGE MAKES (and does not). AgentForge dispatches at most ONCE automatically per logical operation (D066/D067) and sends the operation's STABLE idempotency key, so even a duplicate request that
escaped our protocol inside 24 hours would be deduplicated by Resend (documented, not independently verified against the live service). It does NOT claim exactly-once delivery: the email itself can still be
delayed, bounced or lost after Resend accepted it, and after 24 hours the key no longer protects anything. There is NO key-based lookup, so ``lookup`` is not provided: an UNKNOWN outcome (timeout, lost response, 5xx,
409 concurrent) can only be reconciled by a person checking the Resend dashboard / ``GET /emails/{id}`` and recording what they saw (an attestation, never proof). Replaying the same key to "ask" Resend is
deliberately NOT implemented as reconciliation: it would DISPATCH (it sends if Resend never saw the first request) under a reconciliation record.

OUTCOME MAPPING (``DefinitiveFailure`` = proven not performed): 2xx with an id = success; 400/401/403/404/422 = definitive failure (rejected before sending); 429 = definitive (rejected, nothing was processed);
409 ``invalid_idempotent_request`` / ``concurrent_idempotent_requests``, 5xx, timeouts, connection errors, any unreadable 2xx = UNKNOWN (the request may have been accepted).

SAFETY. The API key is a deployment secret (``RESEND_API_KEY``): it lives only in this module's configuration, is sent only in the Authorization header to ONE fixed https host, and never appears in tool input,
output, events, logs or errors. The sender is fixed by configuration. SANDBOX BY DEFAULT: recipients must belong to ``EMAIL_ALLOWED_DOMAINS`` (default ``resend.dev``, the provider's test addresses); real recipients
need an explicit operator decision. Every call needs a human's exact-action approval (the tool is not on the auto-run allowlist). Redirects are refused, the response is size-capped and schema-checked."""

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentplatform.tools import dispatch as dp
from agentplatform.tools.registry import ToolInputError, ToolRegistry, ToolSpec

API_URL = "https://api.resend.com/emails"
MAX_RESPONSE_BYTES = 4000
TIMEOUT = 8.0
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}")


@dataclass(frozen=True)
class EmailConfig:
    api_key: str
    sender: str
    allowed_domains: tuple = ("resend.dev",)
    url: str = API_URL
    allow_insecure_test_url: bool = False  # tests only: a loopback http mock that models the documented semantics


_config: EmailConfig | None = None


def configure(config: EmailConfig | None) -> None:
    global _config
    if config is not None and not config.allow_insecure_test_url and not config.url.startswith("https://"):
        raise ValueError("the email API URL must be https")
    _config = config


class SendEmailIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    to: str = Field(min_length=6, max_length=254, description="ONE recipient address, e.g. delivered@resend.dev (the sandbox default only allows the provider's test domain)")
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000, description="Plain-text body")

    @field_validator("to")
    @classmethod
    def _one_address(cls, value: str) -> str:
        if not _EMAIL.fullmatch(value):
            raise ValueError("a single valid email address is required")
        return value

    @field_validator("subject")
    @classmethod
    def _no_header_injection(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("the subject must be one line")
        return value


class SendEmailOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_id: str
    provider: str = "resend"
    live: bool = True  # a real request was made to the provider (to a test address it simulates delivery; see the tool's limitations)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _post(config: EmailConfig, key: str, payload: dict) -> tuple:
    request = urllib.request.Request(config.url, method="POST", data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + config.api_key, "Content-Type": "application/json", "Idempotency-Key": key, "User-Agent": "agentforge/1"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)


def send_email(args: SendEmailIn, dispatch=None) -> SendEmailOut:
    config = _config
    if config is None:
        raise dp.DefinitiveFailure()  # not configured: nothing was sent
    domain = args.to.rsplit("@", 1)[1].lower()
    if domain not in config.allowed_domains:
        raise ToolInputError(f"This deployment only allows recipients at: {', '.join(config.allowed_domains)}.")
    key = getattr(dispatch, "idempotency_key", None)
    if not key or not 1 <= len(key) <= 256:
        raise dp.DefinitiveFailure()  # refuse to send without the operation's stable key
    try:
        status, body = _post(config, key, {"from": config.sender, "to": [args.to], "subject": args.subject, "text": args.text})
    except Exception:  # noqa: BLE001 - timeout, reset, TLS, DNS: the request may have been accepted
        raise RuntimeError("upstream_unavailable") from None  # NOT a DefinitiveFailure: unknown outcome
    if 200 <= status < 300:
        try:
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError
            email_id = json.loads(body)["id"]
            if not isinstance(email_id, str) or not 1 <= len(email_id) <= 100:
                raise ValueError
        except Exception:  # noqa: BLE001 - a 2xx we cannot read: it may have been sent
            raise RuntimeError("upstream_unreadable") from None
        return SendEmailOut(email_id=email_id)
    if status in (400, 401, 403, 404, 422, 429):
        raise dp.DefinitiveFailure()  # rejected before anything was sent
    raise RuntimeError(f"upstream_status_{status // 100}xx")  # 409 (key reuse / concurrent), 5xx, anything else: unknown


EMAIL_SEND = ToolSpec(
    id="email.send", version=1, title="Send an email (Resend)", category="Communication",
    summary="Sends one plain-text email through Resend. A REAL external action: it needs a person's approval for every call.",
    description="Sends a single plain-text email from the deployment's configured sender address to ONE recipient through the Resend API. In the default sandbox the recipient must be a Resend test address "
                "(for example delivered@resend.dev). Every call is shown to a workspace owner or admin with the exact recipient, subject and text, and runs only after they approve that exact call.",
    input_model=SendEmailIn, output_model=SendEmailOut, risk="external", side_effect="non_idempotent", timeout_seconds=12.0, max_output_bytes=2000,
    capabilities=("email",), retry_policy="never", remote_idempotency=True, external_ref_field="email_id",
    delivery="At most one automatic dispatch per operation (never retried after an unknown outcome). Resend documents that the same Idempotency-Key with the same payload is not sent twice for 24 hours; AgentForge sends the "
             "operation's stable key. NOT exactly-once: delivery can still fail after acceptance, the key protects only 24 hours, and an unknown outcome can only be reconciled by a person checking Resend (there is no key lookup).",
    limitations=("Plain text only, one recipient, no attachments.", "Sandbox by default: only the configured recipient domains (default resend.dev test addresses) are accepted.",
                 "An unknown outcome (timeout, lost response, 5xx, 409) is never retried automatically and cannot be proven by AgentForge: reconcile it from the Resend dashboard.",
                 "Test addresses count against the provider's quota."),
    example={"to": "delivered@resend.dev", "subject": "Your launch date", "text": "Hello, the launch is confirmed for next week."})


def register(registry: ToolRegistry, config: EmailConfig) -> None:
    """Make the tool available (call once at start-up when RESEND_API_KEY is configured)."""
    configure(config)
    registry.register(EMAIL_SEND, send_email, takes_dispatch=True)  # no lookup: the provider documents none by idempotency key
