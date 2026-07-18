"""OWNERSHIP against the live catalog. All three verdicts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from attest.checkers import check_ownership
from attest.claims import OwnershipClaim, Verdict
from conftest import ALICE, CAROL, DANA, DEPRECATED_UNOWNED, DOCUMENTED, OWNED_BY_CAROL, UNREVIEWED


def test_correct_owner_is_supported(snapshot) -> None:
    claim = OwnershipClaim(
        target_urn=DOCUMENTED,
        owner_urn=ALICE,
        raw_text="Alice Chen owns the customer profile table.",
    )
    r = check_ownership(claim, snapshot(DOCUMENTED))

    assert r.verdict is Verdict.SUPPORTED
    assert ALICE in r.evidence[0].value


def test_wrong_owner_is_contradicted(snapshot) -> None:
    """support_tickets is carol's. A claim naming dana is not unverifiable — it is wrong.

    Ownership is the one aspect where a populated list licenses closed-world reasoning
    with no completeness marker: DataHub's owners list IS the set of owners, not a
    sample of them. So naming someone absent from it is a positive denial.
    """
    claim = OwnershipClaim(
        target_urn=OWNED_BY_CAROL,
        owner_urn=DANA,
        raw_text="Dana Wu owns the support tickets table.",
    )
    r = check_ownership(claim, snapshot(OWNED_BY_CAROL))

    assert r.verdict is Verdict.CONTRADICTED
    assert CAROL in r.evidence[0].value
    assert DANA not in r.evidence[0].value


@pytest.mark.parametrize("urn", [UNREVIEWED, DEPRECATED_UNOWNED])
def test_unowned_dataset_is_insufficient_not_contradicted(snapshot, urn: str) -> None:
    """Nobody is assigned, so nobody is ruled out.

    Contradicted would mean "the catalog says Alice is not the owner". It says no such
    thing — it says nothing at all. An agent naming an owner for an unowned table has
    not been caught in an error; it has outrun the catalog.
    """
    claim = OwnershipClaim(
        target_urn=urn, owner_urn=ALICE, raw_text="Alice Chen owns this table."
    )
    r = check_ownership(claim, snapshot(urn))

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.evidence[0].value is None


def test_owner_must_be_an_actor_urn() -> None:
    """'Sarah' is not a URN. Resolving a name to one is Session 2's job, and it is
    kept out of here so that a bad resolution cannot masquerade as a bad catalog."""
    with pytest.raises(ValidationError):
        OwnershipClaim(target_urn=DOCUMENTED, owner_urn="Sarah", raw_text="Sarah owns it.")

    with pytest.raises(ValidationError):
        OwnershipClaim(
            target_urn=DOCUMENTED,
            owner_urn="urn:li:dataset:(x,y,PROD)",
            raw_text="A dataset owns it.",
        )


def test_the_completeness_marker_does_not_license_ownership_closed_world() -> None:
    """`Verified` is a CLASSIFICATION-completeness marker; it says nothing about whether
    OWNERSHIP is complete (Session 23, Hole 3, the correct-by-design half). An unowned
    Verified table is Insufficient-Coverage, not Contradicted — the closed-world rule stays
    scoped to the checker that declares it. This pin goes red if it ever leaks here."""
    from attest.datahub.snapshot import DatasetSnapshot

    urn = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.h3.own,PROD)"
    snap = DatasetSnapshot(urn=urn, tags=("urn:li:tag:Verified",))  # Verified, no owners
    claim = OwnershipClaim(target_urn=urn, owner_urn=ALICE, raw_text="alice owns it")
    assert check_ownership(claim, snap).verdict is Verdict.INSUFFICIENT_COVERAGE
