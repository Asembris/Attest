"""The guard, against explanations that lie.

"Who audits the auditor" is the sharpest question this product faces, and this file is
the answer. Every test here runs against a REAL CheckResult built from the live catalog,
so the evidence the guard checks against is the evidence a user would actually get.

The hallucinations are the interesting part. Each one is a lie a real model plausibly
tells — a confidently wrong owner, a column that does not exist, a date nobody recorded,
a unit conversion nobody asked for — and each must be caught by code, not by a model.
"""

from __future__ import annotations

from tests.conftest import ALICE, DANA, DOCUMENTED, OWNED_BY_CAROL, STALE

from attest import faithfulness
from attest.checkers import check_freshness, check_ownership
from attest.claims import CheckResult, Evidence, FreshnessClaim, OwnershipClaim, Verdict
from attest.explain import template


def ownership_result(snapshot, urn=OWNED_BY_CAROL, owner=DANA) -> CheckResult:
    """support_tickets is carol's. A claim that dana owns it is Contradicted."""
    claim = OwnershipClaim(
        target_urn=urn,
        raw_text="The support_tickets table is owned by dana.wu.",
        owner_urn=owner,
    )
    return check_ownership(claim, snapshot(urn))


def stale_result(snapshot, now) -> CheckResult:
    claim = FreshnessClaim(
        target_urn=STALE,
        raw_text="revenue_daily is refreshed daily.",
        max_age_hours=24,
    )
    return check_freshness(claim, snapshot(STALE), now=now)


# --- truthful explanations pass ----------------------------------------------


def test_a_truthful_explanation_passes(snapshot):
    result = ownership_result(snapshot)
    text = (
        "The catalog lists urn:li:corpuser:carol.davis as the owner of this dataset. "
        "The claim asserts urn:li:corpuser:dana.wu, who is not an owner. The owners "
        "list in DataHub is exhaustive, so this is a positive disagreement."
    )
    assert faithfulness.check(text, result).ok


def test_the_template_is_always_faithful(snapshot, now):
    """The fallback must pass its own guard, or the floor is not a floor."""
    for result in (ownership_result(snapshot), stale_result(snapshot, now)):
        verdict = faithfulness.check(template(result), result)
        assert verdict.ok, verdict.summary


# --- hallucinations are caught ------------------------------------------------


def test_a_hallucinated_owner_is_caught(snapshot):
    """The classic: a confident, plausible, entirely invented name."""
    result = ownership_result(snapshot)
    text = "This dataset is owned by Sarah Jennings on the analytics team."

    verdict = faithfulness.check(text, result)
    assert not verdict.ok
    assert {v.token for v in verdict.violations} >= {"Sarah", "Jennings"} or any(
        "Sarah" in v.token for v in verdict.violations
    ), verdict.summary


def test_a_hallucinated_column_is_caught(snapshot):
    result = ownership_result(snapshot)
    text = "The `ssn` column suggests this dataset is sensitive."

    verdict = faithfulness.check(text, result)
    assert not verdict.ok
    assert any(v.token == "ssn" for v in verdict.violations), verdict.summary


def test_a_hallucinated_date_is_caught(snapshot, now):
    result = stale_result(snapshot, now)
    text = "The catalog says this table was last modified on 2019-04-02."

    verdict = faithfulness.check(text, result)
    assert not verdict.ok
    assert any("2019" in v.token for v in verdict.violations), verdict.summary


def test_derived_numbers_are_rejected_even_when_correct(snapshot, now):
    """The evidence speaks in hours. "417 days" is arithmetic nobody verified.

    This is the guard being deliberately strict. The conversion may well be right — but
    the guard cannot tell a correct derivation from a plausible one, and one that accepts
    plausible arithmetic accepts hallucinated arithmetic. If we want days said, a checker
    must put days in the evidence.
    """
    result = stale_result(snapshot, now)
    text = "The table was last modified 417 days ago, far outside the 24-hour window."

    verdict = faithfulness.check(text, result)
    assert not verdict.ok
    assert any(v.token == "417" for v in verdict.violations), verdict.summary

    # And the honest phrasing — quoting the figure it was given — passes.
    hours = f"{(now - snapshot(STALE).last_modified).total_seconds() / 3600:.1f}"
    honest = f"The table was last modified {hours} hours ago, outside the 24-hour window."
    assert faithfulness.check(honest, result).ok


# --- the guard is not a rubber stamp -----------------------------------------


def test_pii_does_not_match_inside_nonpii(snapshot):
    """The case that would quietly turn the guard into decoration.

    A substring check would find "PII" inside "NonPII" and wave through an explanation
    asserting PII on a table the catalog says is NonPII — the exact hallucination this
    product exists to catch, passing the guard that exists to catch it.
    """
    claim = OwnershipClaim(
        target_urn=OWNED_BY_CAROL, raw_text="owned by dana", owner_urn=DANA
    )
    result = CheckResult(
        claim=claim,
        verdict=Verdict.CONTRADICTED,
        evidence=(
            Evidence(
                field="tags.tags[].tag.urn",
                value=["urn:li:tag:NonPII"],
                note="Tagged NonPII.",
            ),
        ),
        reason="Tagged NonPII.",
    )

    assert not faithfulness.check("This column is tagged PII.", result).ok
    assert faithfulness.check("This column is tagged NonPII.", result).ok


def test_a_column_name_cannot_be_assembled_from_parts(snapshot):
    """`customer_email` must not pass just because `customer_id` and an `email` exist.

    Matching is by contiguous word sequence for exactly this reason. A bag-of-words guard
    would accept any recombination of real tokens into a fake identifier.
    """
    result = ownership_result(snapshot, urn=DOCUMENTED, owner=ALICE)
    assert result.verdict is Verdict.SUPPORTED

    # customer_profile is the target and it has an `email` column, but there is no
    # `customer_email` anywhere in the catalog.
    verdict = faithfulness.check("The `customer_email` column holds addresses.", result)
    assert not verdict.ok
    assert any(v.token == "customer_email" for v in verdict.violations), verdict.summary


def test_prose_is_free_but_facts_are_not(snapshot):
    """The model may phrase however it likes; it may not introduce a fact."""
    result = ownership_result(snapshot)

    florid = (
        "Unfortunately, the assertion does not survive contact with the catalog, which "
        "is unambiguous on the point and lists a different individual entirely."
    )
    assert faithfulness.check(florid, result).ok, "prose with no new facts must pass"

    assert not faithfulness.check(
        f"{florid} The real owner is bob.martinez.", result
    ).ok, "a fact smuggled into fluent prose must still be caught"
