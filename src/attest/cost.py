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


# --- projecting a run forward ------------------------------------------------
#
# The measured run is a receipt. A receipt for three claims answers "does this work"; it
# does not answer "what does this cost to OPERATE", and that question has to be answered
# BEFORE continuous monitoring is built, not discovered afterwards.
#
# The token counts below are MEASURED, from `just live` against gpt-4o-mini and the seeded
# catalog — the same run the README quotes. They are not estimates, and they are not
# invented. They will drift when a prompt changes, which is exactly why the live test
# prints the projection: a stale constant here misreports a real number.


@dataclass(frozen=True)
class StepTokens:
    """The measured input/output tokens of one LLM step."""

    input: int
    output: int

    def usd(self, model: str) -> float | None:
        return cost_usd(model, self.input, self.output)


# Measured 2026-07-14, gpt-4o-mini, 3 ownership claims over 3 datasets, 1 correction.
#
# decompose is amortized per claim: it is ONE call for the whole agent output, so its cost
# per claim falls as an agent makes more claims in one breath. Charging it per claim is
# therefore the pessimistic reading, which is the right way to round a budget.
DECOMPOSE_PER_CLAIM = StepTokens(input=262, output=117)
EXPLAIN_PER_CLAIM = StepTokens(input=633, output=99)
# One revision attempt. The evidence goes back to the model, so this is the single most
# expensive call in the pipeline — and it fires only on Contradicted claims, which are
# exactly the ones anybody cares about.
REVISE_PER_ATTEMPT = StepTokens(input=1140, output=125)


@dataclass(frozen=True)
class Workload:
    """What an org actually puts through Attest in a day."""

    claims_per_day: int = 1_000
    # The share of claims the catalog contradicts. The correction loop fires ONLY here, so
    # this — not the claim count — is what makes a bill move.
    contradiction_rate: float = 0.10
    # Revision attempts spent per contradicted claim. Bounded by the retry cap (2), so the
    # worst case is bounded too, which is the entire point of making the cap a graph edge.
    attempts_per_contradiction: float = 1.0
    model: str = "gpt-4o-mini"


@dataclass(frozen=True)
class Projection:
    """What a workload costs. Derived from measured tokens, not from a guess."""

    workload: Workload
    per_claim_usd: float
    per_correction_usd: float
    daily_usd: float

    @property
    def monthly_usd(self) -> float:
        return self.daily_usd * 30

    @property
    def yearly_usd(self) -> float:
        return self.daily_usd * 365

    def __str__(self) -> str:
        w = self.workload
        return (
            f"{w.claims_per_day}/day at {w.contradiction_rate:.0%} contradicted: "
            f"${self.daily_usd:.2f}/day, ${self.monthly_usd:.0f}/mo, ${self.yearly_usd:.0f}/yr"
        )


def project(workload: Workload | None = None) -> Projection | None:
    """Cost a workload from the measured per-step tokens. None if the model has no price."""
    w = workload or Workload()

    baseline = cost_usd(
        w.model,
        DECOMPOSE_PER_CLAIM.input + EXPLAIN_PER_CLAIM.input,
        DECOMPOSE_PER_CLAIM.output + EXPLAIN_PER_CLAIM.output,
    )
    correction = REVISE_PER_ATTEMPT.usd(w.model)
    if baseline is None or correction is None:
        return None  # an unpriced model projects to an unknown cost, never to zero

    corrections = w.claims_per_day * w.contradiction_rate * w.attempts_per_contradiction
    daily = w.claims_per_day * baseline + corrections * correction

    return Projection(
        workload=w,
        per_claim_usd=baseline,
        per_correction_usd=correction,
        daily_usd=daily,
    )


def monitoring_projection() -> str:
    """The line the README quotes, next to the measured receipt."""
    nominal = project(Workload(claims_per_day=1_000, contradiction_rate=0.10))
    worst = project(
        Workload(
            claims_per_day=1_000,
            contradiction_rate=1.0,
            attempts_per_contradiction=2.0,  # the retry cap
        )
    )
    if nominal is None or worst is None:  # pragma: no cover — the default model is priced
        return "cost unknown: the configured model has no price"
    return (
        f"1000 claims/day, one org: ${nominal.daily_usd:.2f}/day "
        f"(${nominal.monthly_usd:.0f}/mo). Worst case — every claim contradicted, both "
        f"retries spent — ${worst.daily_usd:.2f}/day (${worst.monthly_usd:.0f}/mo)."
    )


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
