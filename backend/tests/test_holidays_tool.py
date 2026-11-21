"""M6 / Priority 1: the real, read-only ``calendar.public_holidays`` integration. The network is replaced by a local stand-in (the unit tests never touch the internet); what a
real call returned is recorded separately as live evidence, never here."""

import asyncio
import json
import urllib.error

import pytest

from agentplatform.tools import default_registry, holidays
from agentplatform.tools.invoker import ToolInvoker, ToolStore
from agentplatform.tools.registry import ToolInputError
from tests.auth_support import auth_db  # noqa: F401  (fixture used by world)
from tests.test_plan_runs import world  # noqa: F401  (a fixture)
from tests.test_tools import context, invoke, rows

SAMPLE = [{"date": "2026-12-25", "localName": "Christmas Day", "name": "Christmas Day", "countryCode": "US", "fixed": True, "global": True, "counties": None, "launchYear": None, "types": ["Public"]},
          {"date": "2026-01-01", "localName": "New Year's Day", "name": "New Year's Day", "countryCode": "US", "fixed": True, "global": True, "counties": None, "launchYear": None, "types": ["Public"]}]


def stub(monkeypatch, body=None, error=None, seen=None):
    def fake(url, timeout=holidays.FETCH_TIMEOUT, max_bytes=holidays.MAX_BYTES):
        if seen is not None:
            seen.append(url)
        if error is not None:
            raise error
        return body if isinstance(body, bytes) else json.dumps(body).encode()

    monkeypatch.setattr(holidays, "fetch", fake)


def run(country="US", year=2026):
    return holidays.public_holidays(holidays.HolidaysIn(country=country, year=year))


def test_a_valid_response_is_validated_sorted_and_labelled_live(monkeypatch):
    seen = []
    stub(monkeypatch, SAMPLE, seen=seen)
    out = run("us")
    assert [h.date for h in out.holidays] == ["2026-01-01", "2026-12-25"] and out.count == 2 and out.truncated is False
    assert out.source == "date.nager.at" and out.live is True and out.country == "US"
    assert seen == ["https://date.nager.at/api/v3/PublicHolidays/2026/US"]  # fixed host; the URL is built only from validated parts


def test_inputs_are_validated_before_any_request(monkeypatch):
    seen = []
    stub(monkeypatch, SAMPLE, seen=seen)
    for bad in (dict(country="USA", year=2026), dict(country="U", year=2026), dict(country="US", year=1999), dict(country="US", year=2101), dict(country="US", year="2026; DROP"),
                dict(country="US", year=2026, host="evil.example")):
        with pytest.raises(Exception):
            holidays.HolidaysIn(**bad)  # (a 3-character value such as "../" never gets past the schema)
    for sneaky in ("U/", "..", "U?", "1 ", "ü!"):
        with pytest.raises(ToolInputError):
            run(sneaky)  # two characters that are not letters never reach the URL
    assert seen == []


def test_the_fetcher_refuses_any_other_host_and_every_redirect():
    with pytest.raises(ValueError):
        holidays.fetch("https://evil.example/api/v3/PublicHolidays/2026/US")
    with pytest.raises(ValueError):
        holidays.fetch("http://date.nager.at/api/v3/PublicHolidays/2026/US")  # plain http is not https
    with pytest.raises(ValueError):
        holidays.fetch("https://date.nager.at.evil.example/x")
    assert holidays._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/") is None


def test_upstream_failures_end_as_a_fixed_content_free_error_never_a_guess(monkeypatch):
    for error in (TimeoutError(), OSError("boom secret-token"), urllib.error.URLError("dns"), urllib.error.HTTPError("u", 429, "rate", {}, None), urllib.error.HTTPError("u", 503, "x", {}, None)):
        stub(monkeypatch, error=error)
        with pytest.raises(RuntimeError) as caught:
            run()
        assert str(caught.value) in ("upstream_unavailable", "upstream_error") and "secret" not in str(caught.value)
    stub(monkeypatch, error=urllib.error.HTTPError("u", 404, "nf", {}, None))
    with pytest.raises(ToolInputError):
        run("ZZ")


def test_malformed_wrong_year_or_oversized_responses_are_refused_unused(monkeypatch):
    for body in (b"not json", b"{}", b'"x"', json.dumps([{"date": "2026-02-30", "localName": "x", "name": "x", "global": True}]).encode(),
                 json.dumps([{"date": "2025-12-25", "localName": "x", "name": "x", "global": True}]).encode(),  # not the requested year
                 json.dumps([{"date": "2026-12-25", "name": "x"}]).encode(), json.dumps([{"date": "2026-12-25", "localName": "x" * 500, "name": "x", "global": True}]).encode(),
                 json.dumps([SAMPLE[0]] * 201).encode()):
        stub(monkeypatch, body)
        with pytest.raises(RuntimeError) as caught:
            run()
        assert str(caught.value) == "upstream_invalid"


def test_the_result_is_bounded(monkeypatch):
    many = [{"date": f"2026-{m:02d}-{d:02d}", "localName": "H", "name": "H", "global": True} for m in range(1, 13) for d in range(1, 6)]  # 60 entries
    stub(monkeypatch, many)
    out = run()
    assert len(out.holidays) == holidays.MAX_HOLIDAYS and out.count == 60 and out.truncated is True


def test_it_is_on_the_trusted_allowlist_read_only_and_runs_through_the_normal_invoker_only_with_a_grant(world, monkeypatch):
    registry = default_registry()
    spec = registry.get("calendar.public_holidays")
    assert (spec.risk, spec.side_effect, spec.ref) == ("read", "none", "calendar.public_holidays@1") and registry.executable(spec) and not registry.gated(spec)
    assert spec.timeout_seconds > holidays.FETCH_TIMEOUT  # the tool's own deadline is never shorter than the fetch's
    assert any("not verified" in text for text in spec.limitations)  # the limits are stated, not hidden
    seen = []
    stub(monkeypatch, SAMPLE, seen=seen)
    invoker = ToolInvoker(ToolStore(world["sf"]), registry)
    # NOT granted to this agent: refused, and the network is never touched
    refused = invoke(world, invoker, context(world, []), spec.wire_name, {"country": "US", "year": 2026})
    assert (refused.ok, refused.error_code) == (False, "not_permitted") and seen == []
    # granted: it runs, is recorded like every tool call, and the output says it is live
    out = invoke(world, invoker, context(world, [spec]), spec.wire_name, {"country": "US", "year": 2026})
    assert out.ok and json.loads(out.content)["live"] is True and len(seen) == 1
    assert rows(world, "SELECT tool_id, status, error_code FROM tool_invocations ORDER BY created_at")[-1] == ("calendar.public_holidays", "succeeded", None)
    # an upstream failure is recorded as a failed call with the fixed code and no upstream text
    stub(monkeypatch, error=OSError("secret-token upstream detail"))
    failed = invoke(world, invoker, context(world, [spec]), spec.wire_name, {"country": "US", "year": 2026}, seq=1)
    assert (failed.ok, failed.error_code) == (False, "tool_error") and "secret" not in failed.content
    # a model-supplied argument outside the schema never reaches the fetcher
    seen.clear()
    stub(monkeypatch, SAMPLE, seen=seen)
    bad = invoke(world, invoker, context(world, [spec]), spec.wire_name, {"country": "US", "year": 2026, "url": "https://evil.example"})
    assert (bad.ok, bad.error_code) == (False, "invalid_input") and seen == []
