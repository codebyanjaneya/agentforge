"""M2-B: the provider interface and the Groq adapter. The REAL Groq SDK code path runs against an in-memory httpx mock
transport (request building, response parsing and the SDK's own error mapping): no network, no credentials, no live call."""

import asyncio
import json

import httpx
import pytest

from agentplatform.providers import (
    PROVIDER_ERRORS,
    GroqProvider,
    Message,
    ProviderError,
    ProviderRegistry,
    ProviderRequest,
    default_registry,
    provider_error_code,
)

KEY = "gsk_test_key_value_1234567890abcdef"
OK = {"id": "x", "object": "chat.completion", "created": 1, "model": "llama-test",
      "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello there"}, "finish_reason": "stop"}],
      "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


def build(handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return GroqProvider(KEY, base_url="https://api.invalid", http_client=client), calls


def request(**overrides):
    base = dict(model="llama-test", system="SYSTEM PROMPT", messages=(Message("user", "earlier question"), Message("assistant", "earlier answer"),
                Message("user", "the task")), temperature=0.3, max_tokens=256, timeout_seconds=12.5)
    base.update(overrides)
    return ProviderRequest(**base)


def run(coro):
    return asyncio.run(coro)


def test_success_parses_the_response_and_sends_exactly_one_well_formed_request():
    provider, calls = build(lambda r: httpx.Response(200, json=OK))
    response = run(provider.complete(request()))
    assert (response.text, response.model, response.finish_reason) == ("hello there", "llama-test", "stop")
    assert (response.usage.prompt_tokens, response.usage.completion_tokens) == (5, 2)
    assert len(calls) == 1
    sent = json.loads(calls[0].content)
    assert sent["model"] == "llama-test" and sent["temperature"] == 0.3 and sent["max_tokens"] == 256
    assert [(m["role"], m["content"]) for m in sent["messages"]] == [
        ("system", "SYSTEM PROMPT"), ("user", "earlier question"), ("assistant", "earlier answer"), ("user", "the task")]
    assert "stream" not in sent and "tools" not in sent and "functions" not in sent and "tool_choice" not in sent  # no action channel
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"  # the credential goes to the provider only
    assert calls[0].extensions["timeout"]["read"] == 12.5  # the request deadline reaches the transport


def test_max_tokens_is_omitted_when_not_configured():
    provider, calls = build(lambda r: httpx.Response(200, json=OK))
    run(provider.complete(request(max_tokens=None)))
    assert "max_tokens" not in json.loads(calls[0].content)


@pytest.mark.parametrize("status,body,code", [
    (429, {"error": {"message": "SECRET-UPSTREAM rate"}}, "rate_limited"),
    (401, {"error": {"message": "SECRET-UPSTREAM key"}}, "auth"),
    (403, {"error": {"message": "SECRET-UPSTREAM perm"}}, "auth"),
    (404, {"error": {"message": "SECRET-UPSTREAM model"}}, "unsupported_model"),
    (400, {"error": {"message": "SECRET-UPSTREAM", "code": "model_not_found"}}, "unsupported_model"),
    (400, {"error": {"message": "SECRET-UPSTREAM", "code": "model_decommissioned"}}, "unsupported_model"),
    (400, {"error": {"message": "SECRET-UPSTREAM", "code": "context_length_exceeded"}}, "bad_request"),
    (400, {"error": {"message": "SECRET-UPSTREAM", "code": "tool_use_failed"}}, "malformed_tool_call"),
    (400, {"error": "not an object"}, "bad_request"),
    (422, {"error": {"message": "SECRET-UPSTREAM"}}, "bad_request"),
    (409, {"error": {"message": "SECRET-UPSTREAM"}}, "bad_request"),
    (500, {"error": {"message": "SECRET-UPSTREAM boom"}}, "unavailable"),
    (502, {"error": {"message": "SECRET-UPSTREAM"}}, "unavailable"),
    (503, {"error": {"message": "SECRET-UPSTREAM"}}, "unavailable"),
    (418, {"error": {"message": "SECRET-UPSTREAM"}}, "bad_request"),
])
def test_http_errors_become_fixed_safe_errors_and_are_never_retried(status, body, code):
    provider, calls = build(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ProviderError) as info:
        run(provider.complete(request()))
    error = info.value
    assert error.code == code and error.message == PROVIDER_ERRORS[code][0] and error.retryable == PROVIDER_ERRORS[code][1]
    assert "SECRET-UPSTREAM" not in str(error) and KEY not in str(error) and error.__cause__ is None
    assert len(calls) == 1, "the SDK's own retries are off: one call is one attempt"


def test_timeouts_and_connection_failures():
    def timeout(r):
        raise httpx.ReadTimeout("slow SECRET-UPSTREAM", request=r)

    def refused(r):
        raise httpx.ConnectError("refused SECRET-UPSTREAM", request=r)

    for handler, code in ((timeout, "timeout"), (refused, "unavailable")):
        provider, calls = build(handler)
        with pytest.raises(ProviderError) as info:
            run(provider.complete(request()))
        assert info.value.code == code and "SECRET-UPSTREAM" not in str(info.value) and len(calls) == 1


@pytest.mark.parametrize("payload", [
    {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}, {"choices": [{"message": {"content": None}}]},
    {"choices": [{"message": {"content": ""}}]}, {"choices": [{"message": {"content": "   \n"}}]}, {"choices": [{"message": {"content": 5}}]},
    {"choices": "nope"},
])
def test_malformed_responses_are_invalid_response(payload):
    provider, calls = build(lambda r: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as info:
        run(provider.complete(request()))
    assert info.value.code == "invalid_response" and len(calls) == 1


def test_a_non_json_success_body_is_not_trusted():
    provider, _ = build(lambda r: httpx.Response(200, text="<html>oops</html>", headers={"content-type": "text/html"}))
    with pytest.raises(ProviderError) as info:
        run(provider.complete(request()))
    assert info.value.code == "invalid_response"


def test_the_model_name_falls_back_to_the_requested_one_only_for_display():
    payload = {**OK}
    payload.pop("model")
    provider, _ = build(lambda r: httpx.Response(200, json=payload))
    assert run(provider.complete(request(model="requested-model"))).model == "requested-model"


def test_cancellation_and_deadlines_pass_through_untouched():
    async def slow(r):
        await asyncio.sleep(5)
        return httpx.Response(200, json=OK)

    provider, calls = build(slow)

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):  # an outer deadline is not disguised as a provider error
            await asyncio.wait_for(provider.complete(request()), 0.05)

    run(scenario())
    assert len(calls) == 1


# ------------------------------------------------------------------------------------------------ registry and classification

def test_unknown_providers_fail_safely_without_fallback():
    created = []
    registry = ProviderRegistry({"groq": lambda: created.append(1) or object()})
    for name in ("openai", "", "GROQ", "groq ", "anthropic"):
        with pytest.raises(ProviderError) as info:
            registry.get(name)
        assert info.value.code == "unsupported_provider" and info.value.message == PROVIDER_ERRORS["unsupported_provider"][0]
    assert created == []  # nothing was built for an unknown name (and no other provider was substituted)
    first = registry.get("groq")
    assert registry.get("groq") is first and created == [1]  # created lazily, once


def test_the_default_registry_builds_nothing_until_used():
    registry = default_registry()  # must not read settings or credentials here
    with pytest.raises(ProviderError):
        registry.get("someone-else")


def test_error_codes_have_fixed_messages_and_classification_ignores_foreign_exceptions():
    assert set(PROVIDER_ERRORS) == {"timeout", "rate_limited", "unavailable", "auth", "unsupported_model", "unsupported_provider", "bad_request", "invalid_response", "malformed_tool_call", "required_action_missing", "side_effect_outcome_unknown", "side_effect_failed"}
    for code, (message, retryable) in PROVIDER_ERRORS.items():
        error = ProviderError(code)
        assert (error.code, error.message, error.retryable) == (code, message, retryable) and str(error) == message
    assert PROVIDER_ERRORS["timeout"][1] and PROVIDER_ERRORS["rate_limited"][1] and PROVIDER_ERRORS["unavailable"][1]
    assert not PROVIDER_ERRORS["auth"][1] and not PROVIDER_ERRORS["unsupported_model"][1]
    for foreign in (ValueError("x"), KeyError("y"), RuntimeError("z"), asyncio.TimeoutError()):
        assert provider_error_code(foreign) is None


def test_the_provider_does_not_expose_its_credential():
    provider, _ = build(lambda r: httpx.Response(200, json=OK))
    assert KEY not in repr(provider) and KEY not in str(vars(provider).keys())
