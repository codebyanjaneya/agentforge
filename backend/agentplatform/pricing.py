"""Versioned model pricing and the cost of a provider request (M6).

HONESTY RULES. A cost is shown as a cost only when it can be CALCULATED: the provider reported the tokens AND a price for that exact (provider, model) is configured in the version recorded
with the row. Otherwise the row says why it has no cost (``unknown_usage`` / ``unknown_pricing``), and aggregates say they are incomplete. The prices below are CONFIGURED BY AgentForge, in
US dollars per million tokens, as of ``PRICING_VERSION``: they are not invoices and are NOT verified against the provider's current price list by this code; a provider's own billing is the
authority. A price change is a NEW version (a new key in ``PRICES``): old rows keep the version they were calculated with and are never recalculated.

Amounts are integer MICRO-DOLLARS (millionths of a US dollar), computed with exact decimal arithmetic and rounded half-up once per request."""

from decimal import ROUND_HALF_UP, Decimal

PRICING_VERSION = "2026-11-19"

# version -> {(provider, model): (input USD per 1M tokens, output USD per 1M tokens)}
PRICES = {
    "2026-11-19": {
        ("groq", "openai/gpt-oss-120b"): (Decimal("0.15"), Decimal("0.75")),
        ("groq", "openai/gpt-oss-20b"): (Decimal("0.10"), Decimal("0.50")),
    },
}


def price_of(provider: str, model: str, version: str = PRICING_VERSION):
    return PRICES.get(version, {}).get((provider, model))


def cost_micros(provider: str, model: str, input_tokens, output_tokens, version: str = PRICING_VERSION):
    """(micro-dollars or None, basis). Basis is ``calculated``, ``unknown_usage`` (the provider did not report both token counts) or ``unknown_pricing`` (no configured price)."""
    if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
        return None, "unknown_usage"
    price = price_of(provider, model, version)
    if price is None:
        return None, "unknown_pricing"
    micros = (Decimal(input_tokens) * price[0] + Decimal(output_tokens) * price[1]).to_integral_value(rounding=ROUND_HALF_UP)
    return int(micros), "calculated"


def format_usd(micros: int) -> str:
    """Micro-dollars as dollars with enough precision to be honest about tiny amounts (at least 4 decimals below one cent)."""
    dollars = Decimal(micros) / Decimal(1_000_000)
    return f"${dollars:.2f}" if dollars >= Decimal("0.01") else f"${dollars:.6f}".rstrip("0").rstrip(".") if dollars else "$0"
