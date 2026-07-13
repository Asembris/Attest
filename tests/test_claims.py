"""The claim schema. No catalog needed — these are pure validation tests."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from attest.claims import (
    CheckResult,
    Claim,
    ClaimType,
    ClassificationClaim,
    ColumnAssertion,
    Evidence,
    FreshnessClaim,
    OwnershipClaim,
    SchemaClaim,
    Verdict,
)
from conftest import ALICE, DOCUMENTED, PII

ADAPTER = TypeAdapter(Claim)


def test_claims_deserialize_by_discriminator() -> None:
    """Session 2's extractor will emit JSON. It must land on the right class."""
    claim = ADAPTER.validate_python(
        {
            "claim_type": "ownership",
            "target_urn": DOCUMENTED,
            "owner_urn": ALICE,
            "raw_text": "Alice Chen owns the customer profile table.",
        }
    )
    assert isinstance(claim, OwnershipClaim)
    assert claim.claim_type is ClaimType.OWNERSHIP
    assert claim.asserted_value == ALICE


def test_target_must_be_a_dataset_urn() -> None:
    """Every checker reads dataset aspects. A chart URN would fetch nothing and be
    scored Insufficient-Coverage — a lie: the catalog is not silent, we asked wrong."""
    with pytest.raises(ValidationError):
        FreshnessClaim(
            target_urn="urn:li:chart:(looker,dashboard_1)",
            max_age_hours=24,
            raw_text="The dashboard refreshes daily.",
        )

    with pytest.raises(ValidationError):
        FreshnessClaim(
            target_urn="the customer table", max_age_hours=24, raw_text="It is fresh."
        )


def test_labels_must_be_urns_not_names() -> None:
    """'PII' is a word; urn:li:tag:PII is a fact. Mapping one to the other is upstream."""
    with pytest.raises(ValidationError):
        ClassificationClaim(target_urn=DOCUMENTED, labels=("PII",), raw_text="It has PII.")


def test_a_freshness_window_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        FreshnessClaim(target_urn=DOCUMENTED, max_age_hours=0, raw_text="Updated never.")


def test_claims_are_frozen() -> None:
    """A checker must not be able to edit the claim it is judging."""
    claim = OwnershipClaim(target_urn=DOCUMENTED, owner_urn=ALICE, raw_text="Alice owns it.")
    with pytest.raises(ValidationError):
        claim.owner_urn = "urn:li:corpuser:mallory"  # type: ignore[misc]


def test_pii_free_is_a_distinct_claim_from_contains_pii() -> None:
    """Same label, opposite assertions. If `present` collapsed, both would verify alike."""
    contains = ClassificationClaim(target_urn=DOCUMENTED, labels=(PII,), raw_text="Has PII.")
    free = ClassificationClaim(
        target_urn=DOCUMENTED, labels=(PII,), present=False, raw_text="PII-free."
    )
    assert contains.present is True
    assert free.present is False
    assert contains != free


def test_a_verdict_requires_evidence() -> None:
    """Structural, not conventional: an unevidenced verdict cannot be constructed."""
    claim = SchemaClaim(
        target_urn=DOCUMENTED,
        columns=(ColumnAssertion(name="email"),),
        raw_text="It has an email column.",
    )
    with pytest.raises(ValidationError):
        CheckResult(claim=claim, verdict=Verdict.SUPPORTED, evidence=(), reason="Trust me.")

    ok = CheckResult(
        claim=claim,
        verdict=Verdict.SUPPORTED,
        evidence=(Evidence(field="schemaMetadata.fields[].fieldPath", value=["email"]),),
        reason="The schema lists an email column.",
    )
    assert ok.target_urn == DOCUMENTED


def test_evidence_may_hold_an_absence() -> None:
    """value=None is how a checker shows it looked and found nothing. It is not missing
    data — it is the justification for Insufficient-Coverage."""
    assert Evidence(field="ownership.owners[].owner.urn", value=None).value is None
