"""The polarity guard, against explanations that assert the wrong direction.

The faithfulness guard (test_faithfulness.py) proves no fabricated *fact* reaches a reader. It
was never a proof of semantic faithfulness, and it cannot catch a fluent lie told entirely in
ordinary prose: "the catalog supports the claim" beside a Contradicted verdict names no false
fact, so every token in it is free. That is a real hole — it was reproduced against the live
explanation path — and this guard is what closes it.

The rule is narrow and deterministic: an explanation may assert only the DIRECTION the verdict
reached. Supported affirms, Contradicted denies, Insufficient-Coverage is silent. Prose that
affirms where the verdict denies (or denies where it affirms, or claims silence over a definite
verdict) is a mismatch. Factual prose that asserts no relationship at all is free — the verdict
field carries the direction, and the model is not obliged to editorialise on it.
"""

from __future__ import annotations

from tests.conftest import (
    ALICE,
    DANA,
    DOCUMENTED,
    OWNED_BY_CAROL,
    UNREVIEWED,
)

from attest import polarity
from attest.checkers import check_ownership
from attest.claims import CheckResult, Evidence, OwnershipClaim, Verdict
from attest.explain import template
from attest.polarity import OF_VERDICT, Polarity


def ownership_result(snapshot, urn=OWNED_BY_CAROL, owner=DANA) -> CheckResult:
    """support_tickets is carol's. A claim that dana owns it is Contradicted."""
    claim = OwnershipClaim(
        target_urn=urn,
        raw_text="The support_tickets table is owned by dana.wu.",
        owner_urn=owner,
    )
    return check_ownership(claim, snapshot(urn))


def result_with(verdict: Verdict) -> CheckResult:
    """A minimal CheckResult carrying a chosen verdict, for direction tests."""
    claim = OwnershipClaim(
        target_urn=OWNED_BY_CAROL, raw_text="owned by dana", owner_urn=DANA
    )
    return CheckResult(
        claim=claim,
        verdict=verdict,
        evidence=(Evidence(field="ownership.owners[].owner.urn", value=["x"]),),
        reason="reason",
    )


# --- the verdict->polarity mapping is total and correct ----------------------


def test_every_verdict_has_a_polarity():
    assert OF_VERDICT[Verdict.SUPPORTED] is Polarity.AFFIRM
    assert OF_VERDICT[Verdict.CONTRADICTED] is Polarity.DENY
    assert OF_VERDICT[Verdict.INSUFFICIENT_COVERAGE] is Polarity.SILENT
    assert set(OF_VERDICT) == set(Verdict), "a verdict with no polarity is unguardable"


# --- the fluent lie is caught, in every direction ----------------------------


def test_affirming_prose_beside_a_contradicted_verdict_is_a_violation():
    """THE REPRODUCED DEFECT, at the unit level. This exact prose shipped as source=model."""
    check = polarity.check("the catalog supports the claim", result_with(Verdict.CONTRADICTED))
    assert not check.ok
    assert check.violations[0].asserted is Polarity.AFFIRM
    assert check.violations[0].verdict_polarity is Polarity.DENY


def test_denying_prose_beside_a_supported_verdict_is_a_violation():
    check = polarity.check("the catalog contradicts the claim", result_with(Verdict.SUPPORTED))
    assert not check.ok
    assert check.violations[0].asserted is Polarity.DENY


def test_claiming_silence_over_a_definite_verdict_is_a_violation():
    """A Contradicted verdict is a positive disagreement, not the catalog being silent."""
    check = polarity.check(
        "the catalog is silent and holds no evidence", result_with(Verdict.CONTRADICTED)
    )
    assert not check.ok
    assert check.violations[0].asserted is Polarity.SILENT


def test_affirming_or_denying_over_insufficient_coverage_is_a_violation():
    silent = result_with(Verdict.INSUFFICIENT_COVERAGE)
    assert not polarity.check("the catalog confirms the claim", silent).ok
    assert not polarity.check("the catalog refutes the claim", silent).ok


# --- the direction the verdict DID reach is allowed --------------------------


def test_prose_matching_the_verdict_direction_passes():
    assert polarity.check("the catalog supports the claim", result_with(Verdict.SUPPORTED)).ok
    assert polarity.check(
        "the catalog contradicts the claim", result_with(Verdict.CONTRADICTED)
    ).ok
    assert polarity.check(
        "the catalog is silent here", result_with(Verdict.INSUFFICIENT_COVERAGE)
    ).ok


def test_negation_flips_the_direction():
    """"does not support" is a denial, and belongs beside a Contradicted verdict — not a
    Supported one. The guard reads the negation rather than the bare verb."""
    assert polarity.check(
        "the claim is not supported by the catalog", result_with(Verdict.CONTRADICTED)
    ).ok
    assert not polarity.check(
        "the claim is not supported by the catalog", result_with(Verdict.SUPPORTED)
    ).ok
    # ...and a double negation lands back where it started.
    assert polarity.check(
        "the catalog does not contradict the claim", result_with(Verdict.SUPPORTED)
    ).ok


# --- factual prose that asserts no relationship is free ----------------------


def test_factual_prose_with_no_relationship_word_passes(snapshot):
    """The model may describe the catalog without stating a direction; the verdict does that.

    This is what keeps the guard from rejecting every truthful explanation: "carol is the
    listed owner, dana is not" asserts a FACT, not a claim-relationship, and passes untouched.
    """
    result = ownership_result(snapshot)
    text = (
        "The catalog lists urn:li:corpuser:carol.davis as the owner of this dataset. "
        "The claim names urn:li:corpuser:dana.wu, who is not among the owners it lists."
    )
    assert polarity.check(text, result).ok, polarity.check(text, result).summary


# --- the template is polarity-safe by construction ---------------------------


def test_the_template_states_only_the_verdicts_own_direction(snapshot):
    """The floor leads with the verdict word from code, so it can never mis-state polarity.

    If this failed, the fallback the model's rejects degrade to would itself be unshippable —
    and there would be nothing true left to say.
    """
    for verdict, urn, owner in (
        (Verdict.CONTRADICTED, OWNED_BY_CAROL, DANA),
        (Verdict.SUPPORTED, DOCUMENTED, ALICE),
        (Verdict.INSUFFICIENT_COVERAGE, UNREVIEWED, DANA),
    ):
        result = ownership_result(snapshot, urn=urn, owner=owner)
        assert result.verdict is verdict, "test fixture drifted from the seeded catalog"
        check = polarity.check(template(result), result)
        assert check.ok, f"{verdict.value} template mis-stated its own polarity: {check.summary}"
