"""``calendar.public_holidays``: AgentForge's first REAL external integration (M6). Read-only.

WHAT IT DOES. Looks up the official public holidays of one country and year from the public Nager.Date service (https://date.nager.at, no account, no key) so that a
launch planner can avoid a date nobody is at work. It is a genuine network call to a third party: nothing here is simulated, and the result says where it came from.

WHY IT IS SAFE TO RUN WITHOUT A HUMAN. It sends no user data (only a two-letter country code and a year, both validated), it cannot write or change anything anywhere, it
talks to ONE fixed https host over a URL built from validated parts (no user-controlled host, path or query: no request forgery), it follows no redirect, it enforces a connect/read
timeout and a hard cap on the bytes it will read, it validates the response against a strict schema before using any of it, and every failure ends as the fixed, content-free
``tool_error``. It is on the trusted auto-run allowlist as the exact version ``calendar.public_holidays@1`` and, like every tool, only runs for an agent that was GRANTED it.

LIMITS (stated, not hidden). The data is the third party's: AgentForge does not verify it. The service may be unavailable or rate limited; then the tool fails and the agent must
say so instead of guessing. Regional (county-level) holidays are not covered; only the national list the service publishes."""

import json
import re
import urllib.error
import urllib.request
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agentplatform.tools.registry import ToolInputError

HOST = "date.nager.at"
URL = "https://" + HOST + "/api/v3/PublicHolidays/{year}/{country}"
MAX_BYTES = 120_000  # a year of holidays is a few KB; anything bigger is refused unread
MAX_HOLIDAYS = 40
FETCH_TIMEOUT = 4.0  # seconds (the tool's own limit is slightly higher, see its ToolSpec)
_COUNTRY = re.compile(r"[A-Z]{2}")


class HolidaysIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2 country code, e.g. US, GB, DE, IN")
    year: int = Field(ge=2000, le=2100, description="Calendar year, e.g. 2026")


class Holiday(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    name: str
    local_name: str
    nationwide: bool


class HolidaysOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    country: str
    year: int
    count: int
    holidays: list[Holiday]
    truncated: bool
    source: str
    live: bool  # always True: this result came from the real service just now, not from a fixture


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could lead anywhere: refuse every one
        return None


def fetch(url: str, timeout: float = FETCH_TIMEOUT, max_bytes: int = MAX_BYTES) -> bytes:
    """The ONLY place that touches the network. Replaced by tests with a local stand-in; never by configuration. https only, no redirects, bounded."""
    if not url.startswith("https://" + HOST + "/"):
        raise ValueError("host not allowed")
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "agentforge-tool/1"})
    with opener.open(request, timeout=timeout) as response:  # certificate verification stays on (the default)
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError("response too large")
    return body


class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")  # the service adds fields over time; only the ones we declare are ever used
    date: str = Field(min_length=10, max_length=10)
    localName: str = Field(max_length=200)
    name: str = Field(max_length=200)
    global_: bool = Field(alias="global")


def public_holidays(args: HolidaysIn) -> HolidaysOut:
    country = args.country.upper()
    if not _COUNTRY.fullmatch(country):
        raise ToolInputError("The country must be a two-letter code such as US or DE.")
    url = URL.format(year=args.year, country=country)
    try:
        raw = fetch(url)  # looked up at call time so a test can replace it
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ToolInputError("The service does not know that country.") from None
        raise RuntimeError("upstream_error") from None
    except Exception:  # noqa: BLE001 - network, timeout, TLS, size: all end as the fixed tool_error
        raise RuntimeError("upstream_unavailable") from None
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError
        parsed = [_Row.model_validate(r) for r in rows]
        for row in parsed:
            date.fromisoformat(row.date)
            if not row.date.startswith(f"{args.year}-"):
                raise ValueError
    except (ValueError, ValidationError, RecursionError):
        raise RuntimeError("upstream_invalid") from None
    parsed.sort(key=lambda r: r.date)
    kept = parsed[:MAX_HOLIDAYS]
    return HolidaysOut(country=country, year=args.year, count=len(parsed), truncated=len(parsed) > len(kept), source=HOST, live=True,
                       holidays=[Holiday(date=r.date, name=r.name, local_name=r.localName, nationwide=r.global_) for r in kept])
