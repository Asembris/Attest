"""Cost: the arithmetic, and the rule that an unknown price is not a free one.

What is pinned here is the ARITHMETIC and the None-handling, never the rates. A test that
asserted gpt-4o-mini costs $0.15/1M would go red the day OpenAI changes its price list —
reporting a bug in Attest when the truth is a bug in the table. The rates are data with a
date on them (cost.PRICES_AS_OF); the logic is what has to be right.
"""

from __future__ import annotations

from attest.cost import (
    DECOMPOSE_PER_CLAIM,
    EXPLAIN_PER_CLAIM,
    REVISE_PER_ATTEMPT,
    Cost,
    Workload,
    cost_usd,
    price_of,
    project,
    project_fetches,
)


def test_a_call_is_priced_from_its_own_token_counts():
    # 1M input at $0.15 and 1M output at $0.60 is $0.75, by construction.
    price = price_of("gpt-4o-mini")
    assert price is not None
    assert cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == price.input_per_m + price.output_per_m


def test_an_unknown_model_costs_none_and_none_is_not_zero():
    """The whole point of cost.py. A silent zero in a receipt is a lie.

    If someone points ATTEST_MODEL_ENTAILMENT at a model that is not in the table, the
    honest report is "this run's cost is unknown". A report that totalled it as zero would
    be Attest printing an unsupported claim about data — the exact thing it exists to catch.
    """
    assert price_of("some-model-that-does-not-exist") is None
    assert cost_usd("some-model-that-does-not-exist", 1_000, 1_000) is None


def test_one_unpriced_call_poisons_the_whole_total():
    """A partial sum printed as a receipt is worse than no sum: it reads like a complete one."""
    unknown = Cost(input_tokens=10, output_tokens=5, usd=None, unpriced_models=("mystery",))
    assert not unknown.is_known
    assert "cost unknown" in unknown.display()
    assert "mystery" in unknown.display()


def test_a_projection_is_the_measured_tokens_times_the_workload():
    """The projection is arithmetic over MEASURED constants, not a guess with a decimal point."""
    p = project(Workload(claims_per_day=1_000, contradiction_rate=0.0))
    assert p is not None

    per_claim = cost_usd(
        "gpt-4o-mini",
        DECOMPOSE_PER_CLAIM.input + EXPLAIN_PER_CLAIM.input,
        DECOMPOSE_PER_CLAIM.output + EXPLAIN_PER_CLAIM.output,
    )
    assert p.per_claim_usd == per_claim
    # No contradictions, so no corrections, so the bill is exactly the baseline.
    assert p.daily_usd == 1_000 * per_claim
    assert p.monthly_usd == p.daily_usd * 30


def test_the_correction_loop_is_the_cost_multiplier_and_the_cap_is_what_bounds_it():
    """The finding that matters for monitoring: corrections, not claims, are what move a bill.

    A revision sends the evidence back to the model, so it is the most expensive call in
    the pipeline — and it fires only on Contradicted claims, which are exactly the ones
    anyone cares about. Worst case is bounded ONLY by the retry cap, which is why the cap
    is a graph edge rather than a counter someone can forget.
    """
    p = project()
    assert p is not None
    assert p.per_correction_usd > 0

    quiet = project(Workload(claims_per_day=1_000, contradiction_rate=0.0))
    worst = project(
        Workload(claims_per_day=1_000, contradiction_rate=1.0, attempts_per_contradiction=2.0)
    )
    assert quiet is not None and worst is not None

    # Every claim contradicted and both retries spent is under 3x a run with no corrections
    # at all. Bounded, and bounded BY THE CAP: raise the cap and this number rises with it.
    multiplier = worst.daily_usd / quiet.daily_usd
    assert 2.0 < multiplier < 3.0, f"the correction loop's worst case moved: {multiplier:.2f}x"

    uncapped = project(
        Workload(claims_per_day=1_000, contradiction_rate=1.0, attempts_per_contradiction=10.0)
    )
    assert uncapped is not None
    assert uncapped.daily_usd > worst.daily_usd, "without the cap the bill has no ceiling"


def test_an_unpriced_model_projects_to_unknown_rather_than_free():
    assert project(Workload(model="some-model-that-does-not-exist")) is None


def test_the_measured_constants_are_present_and_nonzero():
    """Guards against a 'tidy-up' that zeroes the measured tokens and makes Attest look free."""
    for measured in (DECOMPOSE_PER_CLAIM, EXPLAIN_PER_CLAIM, REVISE_PER_ATTEMPT):
        assert measured.input > 0 and measured.output > 0

    # A revision is the priciest call in the pipeline: it hands the evidence back.
    assert REVISE_PER_ATTEMPT.input > EXPLAIN_PER_CLAIM.input


# --- the other cost: catalog reads -------------------------------------------


def test_the_catalog_load_scales_with_entities_not_with_claims():
    """The number the snapshot cache exists for, projected the same way the dollars are.

    Without a run-scoped cache the catalog is read once per CLAIM; with one, once per
    distinct DATASET. On the nominal workload that is 1000 fetches against 50 — the same
    verdicts, and a twentieth of the load on someone else's server.
    """
    projected = project_fetches(Workload(claims_per_day=1_000, datasets=50))

    assert projected.without_cache == 1_000
    assert projected.with_cache == 50
    assert projected.factor == 20.0


def test_a_run_whose_claims_are_all_about_different_tables_saves_nothing():
    """And the projection says so rather than flattering the cache.

    The cache cannot fetch fewer entities than the run has distinct ones. A projection that
    reported a saving here would be selling a number the code cannot deliver.
    """
    projected = project_fetches(Workload(claims_per_day=40, datasets=50))

    assert projected.with_cache == 40, "the cache cannot save what was never re-read"
    assert projected.without_cache == 40
    assert projected.factor == 1.0
