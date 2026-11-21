"""M6b / Priority 1: provider retry and backoff. Time and randomness are injected, so these tests take no real time and are exact."""

import asyncio

import pytest

from agentplatform.providers import ProviderError, ProviderRequest, ProviderResponse, retry_after_seconds
from agentplatform.resilience import CONTEXT, TRANSIENT, ResilientProvider, RetryPolicy


class Flaky:
    """A provider that fails with the queued errors (by code, optional Retry-After), then answers. Tracks concurrency."""

    name = "groq"

    def __init__(self, *failures, hold=0.0):
        self.failures, self.calls, self.in_flight, self.max_in_flight, self.hold = list(failures), 0, 0, 0, hold

    async def complete(self, request):
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.hold:
                await asyncio.sleep(self.hold)
            if self.failures:
                code, after = self.failures.pop(0)
                raise ProviderError(code, retry_after=after)
            return ProviderResponse(text="ok", model="m")
        finally:
            self.in_flight -= 1


REQ = ProviderRequest(model="m", system="s", messages=())
NO_JITTER = lambda: 0.5  # noqa: E731 - with jitter 0.25, rng 0.5 is exactly the unjittered value for the schedule


class Clock:
    """A fake clock that only moves when the (fake) sleep is called."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(round(seconds, 3))
        self.now += seconds


def provider(inner, clock, **policy):
    return ResilientProvider(inner, RetryPolicy(**policy), sleep=clock.sleep, clock=clock, rng=NO_JITTER)


def collect():
    events = []
    CONTEXT.set({"scope": None, "sink": lambda kind, ctx, fields: events.append((kind, fields.get("attempt"), fields.get("error_code"), fields.get("wait_ms"))), "provider": "groq"})
    return events


def run(coro):
    return asyncio.run(coro)


def test_a_rate_limit_that_clears_is_retried_waits_the_schedule_and_reports_recovery():
    async def go():
        events, clock = collect(), Clock()
        inner = Flaky(("rate_limited", None), ("rate_limited", None))
        response = await provider(inner, clock).complete(REQ)
        return response, inner, clock, events

    response, inner, clock, events = run(go())
    assert response.text == "ok" and inner.calls == 3
    assert clock.slept == [1.0, 2.0]  # base 1 s, doubled; jitter pinned at its midpoint
    assert events == [("waiting", 1, "rate_limited", 1000), ("retrying", 2, "rate_limited", None), ("waiting", 2, "rate_limited", 2000), ("retrying", 3, "rate_limited", None), ("recovered", 3, None, None)]


def test_the_providers_retry_after_wins_over_the_schedule_and_is_never_undercut():
    async def go():
        events, clock = collect(), Clock()
        await provider(Flaky(("rate_limited", 7.0)), clock).complete(REQ)
        return clock

    assert run(go()).slept == [7.0 * (1 + 0.25 * 0.5)]  # at least what it asked, plus jitter on top


def test_jitter_spreads_waits_within_bounds_and_the_wait_is_capped():
    policy = RetryPolicy()
    assert policy.delay(1, None, lambda: 0.0) == pytest.approx(0.75) and policy.delay(1, None, lambda: 1.0) == pytest.approx(1.25)
    assert policy.delay(20, None, lambda: 0.5) == pytest.approx(policy.max_wait)  # exponential growth is bounded
    assert policy.delay(1, 3.0, lambda: 0.0) == 3.0 and policy.delay(1, 3.0, lambda: 1.0) == pytest.approx(3.75)


def test_repeated_429s_exhaust_the_budget_and_the_original_error_is_raised_never_a_script():
    async def go():
        events, clock = collect(), Clock()
        inner = Flaky(*[("rate_limited", None)] * 10)
        with pytest.raises(ProviderError) as raised:
            await provider(inner, clock, max_attempts=4).complete(REQ)
        return raised.value, inner, clock, events

    error, inner, clock, events = run(go())
    assert error.code == "rate_limited" and inner.calls == 4 and clock.slept == [1.0, 2.0, 4.0]  # 4 attempts, 3 waits, then it stops
    assert events[-1] == ("exhausted", 4, "rate_limited", None) and [e[0] for e in events].count("waiting") == 3


def test_a_retry_after_longer_than_the_wait_cap_ends_at_once_without_sleeping():
    async def go():
        events, clock = collect(), Clock()
        inner = Flaky(("rate_limited", 120.0))
        with pytest.raises(ProviderError):
            await provider(inner, clock).complete(REQ)
        return inner, clock, events

    inner, clock, events = run(go())
    assert inner.calls == 1 and clock.slept == [] and events == [("exhausted", 1, "rate_limited", 120000)]


def test_the_total_wait_budget_is_respected():
    async def go():
        clock = Clock()
        inner = Flaky(*[("unavailable", None)] * 10)
        with pytest.raises(ProviderError) as raised:
            await provider(inner, clock, budget=2.5).complete(REQ)
        return raised.value, inner, clock

    error, inner, clock = run(go())
    assert error.code == "unavailable" and clock.slept == [1.0] and inner.calls == 2  # 1 s fits the 2.5 s budget, the next 2 s would not


def test_permanent_failures_are_raised_immediately_and_never_retried():
    async def go(code):
        events, clock = collect(), Clock()
        inner = Flaky((code, None))
        with pytest.raises(ProviderError):
            await provider(inner, clock).complete(REQ)
        return inner, clock, events

    for code in ("auth", "bad_request", "unsupported_model", "invalid_response", "unsupported_provider"):
        assert code not in TRANSIENT
        inner, clock, events = run(go(code))
        assert inner.calls == 1 and clock.slept == [] and events == [("failed", 1, code, None)], code


def test_a_malformed_tool_call_is_a_transient_sampling_failure_and_is_retried_without_a_cooldown():
    async def go():
        clock = Clock()
        inner = Flaky(("malformed_tool_call", None))
        wrapped = provider(inner, clock)
        response = await wrapped.complete(REQ)
        return response, wrapped, clock

    response, wrapped, clock = run(go())
    assert response.text == "ok" and clock.slept == [1.0] and wrapped._cooldown_until == 0.0  # only a rate limit stops other callers


def test_unavailable_and_timeout_are_retried_and_a_transient_failure_that_exhausts_is_reported_as_failed_not_rate_limit_exhaustion():
    async def go():
        events, clock = collect(), Clock()
        inner = Flaky(*[("timeout", None)] * 4)
        with pytest.raises(ProviderError) as raised:
            await provider(inner, clock, max_attempts=4).complete(REQ)
        return raised.value.code, events

    code, events = run(go())
    assert code == "timeout" and events[-1][0] == "failed" and "exhausted" not in [e[0] for e in events]  # distinct from rate-limit exhaustion


def test_concurrent_requests_never_exceed_the_provider_concurrency_limit():
    async def go():
        inner = Flaky(hold=0.02)
        wrapped = ResilientProvider(inner, RetryPolicy(concurrency=2))
        await asyncio.gather(*[wrapped.complete(REQ) for _ in range(8)])
        return inner

    inner = run(go())
    assert inner.calls == 8 and inner.max_in_flight == 2


def test_a_429_sets_a_provider_wide_cooldown_so_concurrent_callers_do_not_pile_on():
    async def go():
        clock = Clock()
        inner = Flaky(("rate_limited", 5.0))
        wrapped = ResilientProvider(inner, RetryPolicy(concurrency=4), sleep=clock.sleep, clock=clock, rng=NO_JITTER)
        first = await wrapped.complete(REQ)  # takes the 429, sleeps its wait, succeeds
        slept_for_first = list(clock.slept)
        clock._now_backup = clock.now
        # a second caller arriving while the cooldown is still in force waits for it instead of sending immediately
        wrapped._cooldown_until = clock.now + 3.0
        await wrapped.complete(REQ)
        return slept_for_first, clock.slept, first

    first_sleeps, all_sleeps, _ = run(go())
    assert first_sleeps == [5.0 * 1.125] and all_sleeps[-1] == pytest.approx(3.0)


def test_loops_are_independent_so_a_semaphore_from_one_event_loop_never_blocks_another():
    wrapped = ResilientProvider(Flaky(), RetryPolicy(concurrency=1))
    for _ in range(3):
        assert run(wrapped.complete(REQ)).text == "ok"


def test_retry_after_headers_are_parsed_safely():
    class Resp:
        def __init__(self, value):
            self.headers = {"retry-after": value} if value is not None else {}

    class Err(Exception):
        def __init__(self, value):
            self.response = Resp(value)

    assert retry_after_seconds(Err("12")) == 12.0 and retry_after_seconds(Err("0.5")) == 0.5
    assert retry_after_seconds(Err("-4")) == 0.0 and retry_after_seconds(Err("99999")) == 3600.0
    assert retry_after_seconds(Err("soon")) is None and retry_after_seconds(Err(None)) is None and retry_after_seconds(ValueError("no response")) is None
    assert retry_after_seconds(Err("nan")) is None


def test_the_policy_is_configurable_from_the_environment_and_ignores_bad_values():
    assert RetryPolicy.from_env({}) == RetryPolicy()
    custom = RetryPolicy.from_env({"PROVIDER_MAX_ATTEMPTS": "2", "PROVIDER_BASE_DELAY": "0.1", "PROVIDER_CONCURRENCY": "5", "PROVIDER_BUDGET": "x", "PROVIDER_MAX_WAIT": "-1"})
    assert (custom.max_attempts, custom.base_delay, custom.concurrency, custom.budget, custom.max_wait) == (2, 0.1, 5, 240.0, 90.0)


def test_a_failing_sink_never_changes_the_outcome():
    async def go():
        def boom(kind, ctx, fields):
            raise RuntimeError("db down")

        CONTEXT.set({"scope": None, "sink": boom})
        clock = Clock()
        return await provider(Flaky(("rate_limited", None)), clock).complete(REQ)

    assert run(go()).text == "ok"


def test_the_real_provider_adapter_passes_retry_after_and_codes_through(monkeypatch):
    """GroqProvider maps an SDK 429 with a Retry-After header to ProviderError(rate_limited, retry_after=...)."""
    import httpx
    import groq

    from agentplatform.providers import GroqProvider

    response = httpx.Response(429, headers={"retry-after": "9"}, request=httpx.Request("POST", "https://api.groq.com/x"), json={"error": {"message": "slow down"}})

    class Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    raise groq.RateLimitError("limit", response=response, body=None)

    with pytest.raises(ProviderError) as raised:
        run(GroqProvider(client=Client()).complete(REQ))
    assert raised.value.code == "rate_limited" and raised.value.retry_after == 9.0
