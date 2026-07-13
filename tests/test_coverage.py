"""The coverage matrix: every claim type must be able to reach every verdict.

This file exists because the gap it guards was invisible until someone went looking.
The seed wrote `lastModified` and `schemaMetadata` onto every dataset unconditionally
while gating `ownership` and `tags` behind `if`s — so FRESHNESS and SCHEMA could only
ever return Supported or Contradicted. Their Insufficient-Coverage branch was dead
code against live data. Every test passed. Nothing was obviously wrong.

A claim type that cannot reach one of its verdicts is not a partial feature, it is a
silent misclassifier: every claim that should have landed in the unreachable cell
lands somewhere else instead, confidently. So the matrix is asserted, not assumed —
and it is asserted against the live catalog, because the failure mode was in the seed
data, not in the code, and no fixture would have caught it.

Delete a dataset, drop an aspect from the emitter, or reach for an `if` that collapses
two verdicts, and this fails by name.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from attest.checkers import check
from attest.claims import (
    Claim,
    ClaimType,
    ClassificationClaim,
    ColumnAssertion,
    FreshnessClaim,
    OwnershipClaim,
    SchemaClaim,
    Verdict,
)
from conftest import (
    ALICE,
    DOCUMENTED,
    NO_SCHEMA,
    NO_TIMESTAMP,
    NONPII_TRAP,
    OWNED_BY_CAROL,
    PII,
    REVIEWED_CLEAN,
    STALE,
    UNREVIEWED,
)

# One canonical claim per (claim type x verdict) cell, each against the seeded dataset
# purpose-built to produce it. Twelve cells; all twelve must be live.
MATRIX: list[tuple[ClaimType, Verdict, Claim]] = [
    # --- FRESHNESS ------------------------------------------------------------
    (
        ClaimType.FRESHNESS,
        Verdict.SUPPORTED,
        FreshnessClaim(
            target_urn=DOCUMENTED, max_age_hours=24, raw_text="Updated in the last 24 hours."
        ),
    ),
    (
        ClaimType.FRESHNESS,
        Verdict.CONTRADICTED,
        FreshnessClaim(target_urn=STALE, max_age_hours=24, raw_text="Rebuilt nightly."),
    ),
    (
        ClaimType.FRESHNESS,
        Verdict.INSUFFICIENT_COVERAGE,
        # No lastModified. Not stale — unknown.
        FreshnessClaim(target_urn=NO_TIMESTAMP, max_age_hours=24, raw_text="Refreshed daily."),
    ),
    # --- OWNERSHIP ------------------------------------------------------------
    (
        ClaimType.OWNERSHIP,
        Verdict.SUPPORTED,
        OwnershipClaim(target_urn=DOCUMENTED, owner_urn=ALICE, raw_text="Alice Chen owns it."),
    ),
    (
        ClaimType.OWNERSHIP,
        Verdict.CONTRADICTED,
        # Owned by carol.davis; the owners list is exhaustive.
        OwnershipClaim(
            target_urn=OWNED_BY_CAROL, owner_urn=ALICE, raw_text="Alice Chen owns it."
        ),
    ),
    (
        ClaimType.OWNERSHIP,
        Verdict.INSUFFICIENT_COVERAGE,
        # No owner assigned. Nobody is ruled out.
        OwnershipClaim(target_urn=UNREVIEWED, owner_urn=ALICE, raw_text="Alice Chen owns it."),
    ),
    # --- CLASSIFICATION -------------------------------------------------------
    (
        ClaimType.CLASSIFICATION,
        Verdict.SUPPORTED,
        ClassificationClaim(target_urn=DOCUMENTED, labels=(PII,), raw_text="It contains PII."),
    ),
    (
        ClaimType.CLASSIFICATION,
        Verdict.CONTRADICTED,
        # Tagged NonPII, which positively excludes PII.
        ClassificationClaim(target_urn=NONPII_TRAP, labels=(PII,), raw_text="It contains PII."),
    ),
    (
        ClaimType.CLASSIFICATION,
        Verdict.INSUFFICIENT_COVERAGE,
        # No tags, no terms, not marked reviewed.
        ClassificationClaim(target_urn=UNREVIEWED, labels=(PII,), raw_text="It contains PII."),
    ),
    # --- SCHEMA ---------------------------------------------------------------
    (
        ClaimType.SCHEMA,
        Verdict.SUPPORTED,
        SchemaClaim(
            target_urn=DOCUMENTED,
            columns=(ColumnAssertion(name="email", native_type="VARCHAR(255)"),),
            raw_text="It has an email column.",
        ),
    ),
    (
        ClaimType.SCHEMA,
        Verdict.CONTRADICTED,
        # The schema is exhaustive and has no such column.
        SchemaClaim(
            target_urn=REVIEWED_CLEAN,
            columns=(ColumnAssertion(name="ssn"),),
            raw_text="It has an ssn column.",
        ),
    ),
    (
        ClaimType.SCHEMA,
        Verdict.INSUFFICIENT_COVERAGE,
        # No schemaMetadata. Absence of a schema is not absence of a column.
        SchemaClaim(
            target_urn=NO_SCHEMA,
            columns=(ColumnAssertion(name="revenue_amount"),),
            raw_text="It has a revenue_amount column.",
        ),
    ),
]


@pytest.mark.parametrize(
    ("claim_type", "expected", "claim"),
    MATRIX,
    ids=[f"{t.value}-{v.value}" for t, v, _ in MATRIX],
)
def test_cell_is_reachable(
    claim_type: ClaimType, expected: Verdict, claim: Claim, snapshot, now: datetime
) -> None:
    """Each of the 12 cells, against the live catalog."""
    result = check(claim, snapshot(claim.target_urn), now=now)

    assert result.verdict is expected, (
        f"{claim_type.value} cannot reach {expected.value} against {claim.target_urn} "
        f"— got {result.verdict.value}. Either a checker collapsed two verdicts, or the "
        f"seed dataset that exercises this cell lost the aspect (or the absence) it was "
        f"built to have. See seed/generate_seed.py."
    )
    assert result.evidence, "a verdict with no evidence is not auditable"


def test_every_cell_in_the_matrix_is_covered() -> None:
    """Guards the guard: the parametrization above must actually span all 12 cells.

    Without this, deleting a MATRIX entry makes the suite greener, not redder.
    """
    covered = {(t, v) for t, v, _ in MATRIX}
    expected = {(t, v) for t in ClaimType for v in Verdict}

    missing = expected - covered
    assert not missing, "unexercised (claim type x verdict) cells: " + ", ".join(
        sorted(f"{t.value}/{v.value}" for t, v in missing)
    )
    assert len(MATRIX) == 12


def test_insufficient_coverage_is_never_a_swallowed_lookup_failure(snapshot, now: datetime) -> None:
    """Every Insufficient-Coverage verdict must name the field it found empty.

    An auditor is allowed to say "I don't know". It is not allowed to say "I don't
    know" without saying what it looked at — that is indistinguishable from a bug, and
    it is exactly what a swallowed query error would produce.
    """
    for claim_type, expected, claim in MATRIX:
        if expected is not Verdict.INSUFFICIENT_COVERAGE:
            continue
        result = check(claim, snapshot(claim.target_urn), now=now)

        assert result.evidence, claim_type
        assert any(e.value is None for e in result.evidence), (
            f"{claim_type.value} returned Insufficient-Coverage without pointing at the "
            f"absent field. The absence IS the evidence and must be reported."
        )
