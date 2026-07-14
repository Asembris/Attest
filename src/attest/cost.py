"""What a run cost, in dollars.

The README quotes a cost per audit ("N claims in X seconds for $Y"). That number is a
receipt, so it has to be a measurement rather than an estimate: it is computed from the
token counts the API actually returned, priced against the table below.

**An unknown model costs `None`, never `0`.** This is the same distinction the catalog
layer makes between absent and empty, and it matters for the same reason. If someone
points ATTEST_MODEL_ENTAILMENT at a model that is not in this table, the honest report is
"this run's cost is unknown"; a report that quietly totals it as zero would be a lie of
exactly the kind Attest exists to catch, printed by Attest itself. So `price_of` returns
None, `Cost.usd` goes None the moment any priced step is unknown, and the README number
is only ever printed when every step in the run had a price.

Prices are per million tokens, USD, from OpenAI's public pricing as of 2026-07-14. They
are data, not truth: a stale entry here silently misreports a real number, so the table
records what it is and when, and the tests pin the arithmetic rather than the rates.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_AS_OF = "2026-07-14"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input_per_m: float
    output_per_m: float


# Keyed by the exact model id `settings.model_for(step)` resolves to. Deliberately not a
# prefix match: `gpt-4o` is not `gpt-4o-mini` and mispricing by 16x would go unnoticed.
PRICES: dict[str, Price] = {
    "gpt-4o-mini": Price(input_per_m=0.15, output_per_m=0.60),
    "gpt-4o": Price(input_per_m=2.50, output_per_m=10.00),
    "gpt-4.1-mini": Price(input_per_m=0.40, output_per_m=1.60),
    "gpt-4.1": Price(input_per_m=2.00, output_per_m=8.00),
}


def price_of(model: str) -> Price | None:
    """The price of a model, or None if we do not know it. None is not zero."""
    return PRICES.get(model)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """What one call cost, or None if this model has no price in the table."""
    price = price_of(model)
    if price is None:
        return None
    return (
        input_tokens * price.input_per_m + output_tokens * price.output_per_m
    ) / 1_000_000


@dataclass(frozen=True)
class Cost:
    """The totalled cost of a run.

    `usd` is None when ANY call in the run used a model with no price. A partial total
    would be worse than no total: it reads like a complete one.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    usd: float | None = 0.0
    unpriced_models: tuple[str, ...] = ()

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_known(self) -> bool:
        return self.usd is not None

    def display(self) -> str:
        if self.usd is None:
            unpriced = ", ".join(self.unpriced_models)
            return f"{self.total_tokens} tokens, cost unknown (unpriced: {unpriced})"
        return f"{self.total_tokens} tokens, ${self.usd:.6f}"
