"""The first tools of the registry (M4-B). Every one is read-only, deterministic, local (no network, no filesystem, no credentials, no
clock-dependent or random behaviour) and bounded in input size and work. Each documents what it really does and what it does not do.

They were chosen because a startup's agents need them: a finance agent that does arithmetic and unit economics instead of guessing
numbers (``math.calculate``, ``finance.unit_economics``), a marketing/support agent that measures its own copy (``text.analyze``), a
research/finance agent that summarises a pasted table (``data.summarize_csv``) and a planning agent that does calendar arithmetic
correctly (``time.date_math``). Language models are unreliable at exactly these tasks.
"""

import ast
import csv
import io
import math
import re
from collections import Counter
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agentplatform.tools.registry import ToolInputError, ToolRegistry, ToolSpec


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------------------------------------ math.calculate

class CalculateIn(_In):
    expression: str = Field(min_length=1, max_length=200, description="An arithmetic expression, e.g. (1200 * 0.85 - 300) / 12")


class CalculateOut(_Out):
    value: float
    formatted: str
    expression: str


_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "min": min, "max": max, "log": math.log, "log10": math.log10, "floor": math.floor, "ceil": math.ceil}
_CONSTS = {"pi": math.pi, "e": math.e}
_BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
        ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}
_LIMIT = 1e15


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError("Only numbers are allowed in an expression.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ToolInputError("The result is not a finite number.")
    if abs(value) > 1e300:
        raise ToolInputError("The result is too large.")
    return value


def _walk(node, state):
    state["nodes"] += 1
    if state["nodes"] > 80:
        raise ToolInputError("The expression is too complicated.")
    if isinstance(node, ast.Expression):
        return _walk(node.body, state)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)) or abs(node.value) > _LIMIT:
            raise ToolInputError("Only plain numbers up to 1e15 are allowed.")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ToolInputError("Unknown name in the expression (only pi and e are available).")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _num(_walk(node.operand, state))
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = _num(_walk(node.left, state)), _num(_walk(node.right, state))
        if isinstance(node.op, ast.Pow):
            if abs(right) > 64 or abs(left) > 1e12:
                raise ToolInputError("The power is too large.")
            try:
                return _num(left ** right)
            except (OverflowError, ZeroDivisionError):
                raise ToolInputError("The power cannot be calculated.") from None
        operation = _BIN.get(type(node.op))
        if operation is None:
            raise ToolInputError("That operator is not allowed.")
        try:
            return _num(operation(left, right))
        except ZeroDivisionError:
            raise ToolInputError("Division by zero.") from None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords and 1 <= len(node.args) <= 3:
        args = [_num(_walk(arg, state)) for arg in node.args]
        try:
            return _num(_FUNCS[node.func.id](*args))
        except (ValueError, TypeError, OverflowError):
            raise ToolInputError("That function cannot be applied to these numbers.") from None
    raise ToolInputError("Only arithmetic is allowed: numbers, + - * / // % ^, parentheses, pi, e and sqrt/abs/round/min/max/log/log10/floor/ceil.")


def calculate(args: CalculateIn) -> CalculateOut:
    try:
        tree = ast.parse(args.expression.strip().replace("^", "**"), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ToolInputError("The expression is not valid arithmetic.") from None
    value = float(_num(_walk(tree, {"nodes": 0})))
    return CalculateOut(value=value, formatted=f"{value:.12g}", expression=args.expression.strip())


# ------------------------------------------------------------------------------------------------ finance.unit_economics

class UnitEconomicsIn(_In):
    monthly_price: float = Field(gt=0, le=1e9, description="Revenue per customer per month")
    gross_margin_pct: float = Field(gt=0, le=100, description="Gross margin in percent")
    monthly_churn_pct: float = Field(gt=0, le=100, description="Share of customers lost per month, in percent")
    cac: float = Field(ge=0, le=1e9, description="Customer acquisition cost")


class UnitEconomicsOut(_Out):
    gross_profit_per_month: float
    expected_lifetime_months: float
    ltv: float
    ltv_to_cac: float | None
    cac_payback_months: float | None
    assessment: Literal["no_cac_given", "healthy", "marginal", "unprofitable"]
    rule: str


def unit_economics(args: UnitEconomicsIn) -> UnitEconomicsOut:
    gross = args.monthly_price * args.gross_margin_pct / 100
    lifetime = 100 / args.monthly_churn_pct
    ltv = gross * lifetime
    ratio = ltv / args.cac if args.cac > 0 else None
    payback = args.cac / gross if args.cac > 0 else None
    if ratio is None:
        verdict = "no_cac_given"
    elif ratio >= 3 and payback <= 12:
        verdict = "healthy"
    elif ratio >= 1:
        verdict = "marginal"
    else:
        verdict = "unprofitable"
    return UnitEconomicsOut(gross_profit_per_month=round(gross, 2), expected_lifetime_months=round(lifetime, 2), ltv=round(ltv, 2),
                            ltv_to_cac=None if ratio is None else round(ratio, 2), cac_payback_months=None if payback is None else round(payback, 2), assessment=verdict,
                            rule="LTV = price x margin / churn; healthy = LTV/CAC >= 3 and payback <= 12 months; marginal = LTV/CAC >= 1; otherwise unprofitable")


# ------------------------------------------------------------------------------------------------ text.analyze

class TextAnalyzeIn(_In):
    text: str = Field(min_length=1, max_length=20000)
    top_keywords: int = Field(default=8, ge=1, le=15)


class Keyword(_Out):
    word: str
    count: int


class TextAnalyzeOut(_Out):
    characters: int
    words: int
    sentences: int
    paragraphs: int
    average_words_per_sentence: float
    longest_sentence_words: int
    reading_time_minutes: float
    flesch_reading_ease: float | None
    readability: Literal["very_easy", "easy", "fairly_easy", "standard", "fairly_difficult", "difficult", "very_difficult", "not_enough_text"]
    top_keywords: list[Keyword]


_STOP = frozenset("a about above after again all also am an and any are as at be because been before being below between both but by can could did do does doing down during each "
                  "few for from further had has have having he her here hers him his how i if in into is it its just me more most my no nor not now of off on once only or other our "
                  "out over own same she should so some such than that the their them then there these they this those through to too under until up us very was we were what when "
                  "where which while who whom why will with would you your yours".split())
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_SENTENCE = re.compile(r"[^.!?]+[.!?]*")


def _syllables(word: str) -> int:
    groups = re.findall(r"[aeiouy]+", word.lower())
    count = len(groups) - (1 if word.lower().endswith("e") and len(groups) > 1 else 0)
    return max(1, count)


def analyze_text(args: TextAnalyzeIn) -> TextAnalyzeOut:
    text = args.text.strip()
    words = _WORD.findall(text)
    sentences = [s for s in (m.group().strip() for m in _SENTENCE.finditer(text)) if _WORD.search(s)]
    per_sentence = [len(_WORD.findall(s)) for s in sentences]
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    flesch = None
    label = "not_enough_text"
    if len(words) >= 10 and sentences:
        syll = sum(_syllables(w) for w in words)
        flesch = round(206.835 - 1.015 * (len(words) / len(sentences)) - 84.6 * (syll / len(words)), 1)
        bands = ((90, "very_easy"), (80, "easy"), (70, "fairly_easy"), (60, "standard"), (50, "fairly_difficult"), (30, "difficult"))
        label = next((name for floor, name in bands if flesch >= floor), "very_difficult")
    counts = Counter(w.lower().strip("'’-") for w in words if len(w) > 2 and w.lower() not in _STOP)
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top_keywords]
    return TextAnalyzeOut(characters=len(text), words=len(words), sentences=len(sentences), paragraphs=len(paragraphs),
                          average_words_per_sentence=round(len(words) / len(sentences), 1) if sentences else 0.0, longest_sentence_words=max(per_sentence, default=0),
                          reading_time_minutes=round(len(words) / 200, 1), flesch_reading_ease=flesch, readability=label, top_keywords=[Keyword(word=w, count=c) for w, c in top])


# ------------------------------------------------------------------------------------------------ data.summarize_csv

class SummarizeCsvIn(_In):
    csv_text: str = Field(min_length=1, max_length=100000, description="The table as text (not a file path or a URL)")
    delimiter: Literal[",", ";", "\t", "|"] = ","
    has_header: bool = True


class ColumnSummary(_Out):
    name: str
    kind: Literal["number", "text"]
    non_empty: int
    distinct: int
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    total: float | None = None
    top_values: list[str] = Field(default_factory=list)


class SummarizeCsvOut(_Out):
    rows: int
    columns: list[ColumnSummary]
    truncated: bool


MAX_ROWS, MAX_COLUMNS = 5000, 30


def _as_number(raw: str):
    try:
        value = float(raw.strip().replace(",", "") if raw.strip().count(",") and "." not in raw else raw.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def summarize_csv(args: SummarizeCsvIn) -> SummarizeCsvOut:
    try:
        rows = list(csv.reader(io.StringIO(args.csv_text), delimiter=args.delimiter))
    except csv.Error:
        raise ToolInputError("The text is not valid delimited data.") from None
    rows = [r for r in rows if any(cell.strip() for cell in r)]
    if not rows:
        raise ToolInputError("The table is empty.")
    header = rows[0] if args.has_header else [f"column_{i + 1}" for i in range(len(rows[0]))]
    body = rows[1:] if args.has_header else rows
    if not 1 <= len(header) <= MAX_COLUMNS:
        raise ToolInputError(f"The table must have between 1 and {MAX_COLUMNS} columns.")
    truncated = len(body) > MAX_ROWS
    body = body[:MAX_ROWS]
    columns = []
    for index, name in enumerate(header):
        cells = [r[index].strip() for r in body if index < len(r) and r[index].strip()]
        numbers = [_as_number(c) for c in cells]
        numeric = bool(cells) and sum(n is not None for n in numbers) / len(cells) >= 0.9
        distinct = len(set(cells))
        if numeric:
            values = [n for n in numbers if n is not None]
            columns.append(ColumnSummary(name=(name.strip() or f"column_{index + 1}")[:60], kind="number", non_empty=len(cells), distinct=distinct, minimum=round(min(values), 6),
                                         maximum=round(max(values), 6), mean=round(sum(values) / len(values), 6), total=round(sum(values), 6)))
        else:
            top = [value[:40] for value, _n in Counter(cells).most_common(3)]
            columns.append(ColumnSummary(name=(name.strip() or f"column_{index + 1}")[:60], kind="text", non_empty=len(cells), distinct=distinct, top_values=top))
    return SummarizeCsvOut(rows=len(body), columns=columns, truncated=truncated)


# ------------------------------------------------------------------------------------------------ time.date_math

class DateMathIn(_In):
    operation: Literal["add_days", "add_business_days", "days_between", "business_days_between", "weekday_of"]
    start: date
    days: int | None = Field(default=None, ge=-36500, le=36500)
    end: date | None = None


class DateMathOut(_Out):
    result_date: str | None
    number: int | None
    weekday: str | None
    explanation: str


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _add_business_days(start: date, days: int) -> date:
    step, remaining, current = (1 if days >= 0 else -1), abs(days), start
    while remaining:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _weekdays_through(day: date) -> int:
    """Weekdays (Mon-Fri) among all days from 0001-01-01 (a Monday, ordinal 1) up to and including ``day``."""
    n = day.toordinal()
    return 5 * (n // 7) + min(n % 7, 5)


def date_math(args: DateMathIn) -> DateMathOut:
    op, start = args.operation, args.start
    if op in ("add_days", "add_business_days"):
        if args.days is None:
            raise ToolInputError("This operation needs 'days'.")
        try:
            result = start + timedelta(days=args.days) if op == "add_days" else _add_business_days(start, args.days)
        except OverflowError:
            raise ToolInputError("The resulting date is out of range.") from None
        kind = "calendar" if op == "add_days" else "business"
        return DateMathOut(result_date=result.isoformat(), number=None, weekday=_DAYS[result.weekday()],
                           explanation=f"{start.isoformat()} plus {args.days} {kind} days is {result.isoformat()} ({_DAYS[result.weekday()]}). Weekends are the only business-day exclusion; holidays are not known.")
    if op == "weekday_of":
        return DateMathOut(result_date=None, number=None, weekday=_DAYS[start.weekday()], explanation=f"{start.isoformat()} is a {_DAYS[start.weekday()]}.")
    if args.end is None:
        raise ToolInputError("This operation needs 'end'.")
    end = args.end
    if op == "days_between":
        n = (end - start).days
        return DateMathOut(result_date=None, number=n, weekday=None, explanation=f"There are {n} calendar days from {start.isoformat()} to {end.isoformat()} (negative when the end is earlier).")
    low, high, sign = (start, end, 1) if end >= start else (end, start, -1)
    count = _weekdays_through(high) - _weekdays_through(low)  # closed form: constant time however far apart the dates are
    return DateMathOut(result_date=None, number=sign * count, weekday=None,
                       explanation=f"{abs(count)} weekdays after {low.isoformat()} up to and including {high.isoformat()} (Monday to Friday; holidays are not known).")


# ------------------------------------------------------------------------------------------------ the registry

def build_default_registry() -> ToolRegistry:
    from agentplatform.tools.policy import default_policy

    registry = ToolRegistry(default_policy(), allow_gated=True)  # no shipped tool needs approval; an agent may be GRANTED one that does, and each call still needs its own approval
    registry.register(ToolSpec(
        id="math.calculate", version=1, title="Calculator", category="Calculation",
        summary="Evaluates an arithmetic expression exactly instead of estimating it.",
        description="Parses the expression with a strict whitelist (numbers, + - * / // % ^, parentheses, pi, e and a few functions) and computes the result in a bounded way. "
                    "Use it whenever a task involves arithmetic that must be right.",
        input_model=CalculateIn, output_model=CalculateOut, timeout_seconds=2.0, max_output_bytes=2000, capabilities=("arithmetic", "numbers"),
        limitations=("Only plain arithmetic: no variables, no units, no currencies, no percent literals.", "Numbers up to 1e15 and powers up to 64.", "It does not run any code."),
        example={"expression": "(1200 * 0.85 - 300) / 12"}), calculate)
    registry.register(ToolSpec(
        id="finance.unit_economics", version=1, title="Unit economics", category="Finance",
        summary="Computes LTV, LTV:CAC and CAC payback from price, margin, churn and CAC.",
        description="Applies the standard subscription formulas: gross profit per month = price x margin, lifetime = 1 / monthly churn, LTV = gross profit x lifetime, "
                    "payback = CAC / gross profit. Returns the numbers and a plain assessment with the rule that produced it.",
        input_model=UnitEconomicsIn, output_model=UnitEconomicsOut, timeout_seconds=2.0, max_output_bytes=2000, capabilities=("pricing", "saas-metrics"),
        limitations=("Assumes constant churn and margin, no expansion revenue and no discounting.", "It is arithmetic on the numbers it is given; it cannot judge whether they are realistic."),
        example={"monthly_price": 49, "gross_margin_pct": 80, "monthly_churn_pct": 3, "cac": 400}), unit_economics)
    registry.register(ToolSpec(
        id="text.analyze", version=1, title="Text analyzer", category="Writing",
        summary="Measures a text: length, reading time, readability and the words it leans on.",
        description="Counts words, sentences and paragraphs, estimates reading time (200 words a minute), computes the Flesch reading ease score with a syllable heuristic "
                    "and lists the most frequent non-trivial words.",
        input_model=TextAnalyzeIn, output_model=TextAnalyzeOut, timeout_seconds=3.0, max_output_bytes=4000, capabilities=("readability", "keywords"),
        limitations=("English only (the readability score and the stop-word list assume English).", "Texts up to 20,000 characters.", "The syllable count is a heuristic."),
        example={"text": "Our launch email should be short. It should say what changed and why it matters to you.", "top_keywords": 5}), analyze_text)
    registry.register(ToolSpec(
        id="data.summarize_csv", version=1, title="Table summary", category="Data",
        summary="Summarises a pasted table: rows, column types, ranges, totals and common values.",
        description="Reads delimited text (comma, semicolon, tab or pipe), detects numeric and text columns and reports non-empty and distinct counts, "
                    "min/max/mean/total for numbers and the most common values for text.",
        input_model=SummarizeCsvIn, output_model=SummarizeCsvOut, timeout_seconds=4.0, max_output_bytes=12000, capabilities=("tables", "statistics"),
        limitations=("The table must be passed as text; it cannot open files or URLs.", "At most 5,000 rows and 30 columns are read.", "No dates, no joins, no charts."),
        example={"csv_text": "plan,price,seats\nstarter,19,3\nteam,49,10\nscale,199,50", "delimiter": ",", "has_header": True}), summarize_csv)
    registry.register(ToolSpec(
        id="time.date_math", version=1, title="Date arithmetic", category="Planning",
        summary="Adds days or business days to a date, counts days between dates, names weekdays.",
        description="Exact calendar arithmetic for plans and deadlines: add calendar or business days, count days or business days between two dates, "
                    "find the weekday of a date. Business days exclude Saturdays and Sundays.",
        input_model=DateMathIn, output_model=DateMathOut, timeout_seconds=2.0, max_output_bytes=2000, capabilities=("deadlines", "scheduling"),
        limitations=("Public holidays and time zones are not known.", "Dates only (no times)."),
        example={"operation": "add_business_days", "start": "2026-12-01", "days": 10}), date_math)
    from agentplatform.tools import holidays

    registry.register(ToolSpec(
        id="calendar.public_holidays", version=1, title="Public holidays", category="Planning",
        summary="Looks up the official public holidays of a country and year from a live public source (date.nager.at).",
        description="A REAL, read-only lookup of a third-party service (Nager.Date, no account or key): it sends only a two-letter country code and a year, never any of your data, "
                    "and returns the national public holidays with their dates and names so a launch can avoid them. If the service is unavailable the call fails and the agent must say so.",
        input_model=holidays.HolidaysIn, output_model=holidays.HolidaysOut, timeout_seconds=6.0, max_output_bytes=12000, capabilities=("holidays", "calendar", "live-data"),
        limitations=("Needs the internet and the third-party service to be up; it can fail or be rate limited.", "National holidays only: regional or company holidays are not included.",
                     "The data comes from the service and is not verified by AgentForge.", "At most 40 holidays are returned."),
        example={"country": "US", "year": 2026}), holidays.public_holidays)
    return registry


_DEFAULT = None


def default_registry() -> ToolRegistry:
    """The process-wide registry of trusted tools (built once)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = build_default_registry()
    return _DEFAULT
