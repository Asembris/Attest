"""CLASSIFICATION against the live catalog.

The subtlest checker, so the tests here are less about the three verdicts (though
they cover them) and more about the two ways a naive checker gets classification
wrong:

  - certifying an UNREVIEWED table as PII-free, because it found no PII tag
  - flagging an unreviewed table as Contradicted, because it found no PII tag

Both mistake silence for a finding, in opposite directions. Both are tested against
below.
"""

from __future__ import annotations

from attest.checkers import check_classification
from attest.claims import ClassificationClaim, Verdict
from conftest import (
    DEPRECATED_UNOWNED,
    DOCUMENTED,
    EMAIL_TERM,
    NON_PII,
    NONPII_TRAP,
    PII,
    REVIEWED_CLEAN,
    TIER1,
    UNREVIEWED,
)

# --- the three verdicts -------------------------------------------------------


def test_tagged_pii_is_supported(snapshot) -> None:
    claim = ClassificationClaim(
        target_urn=DOCUMENTED, labels=(PII,), raw_text="The customer profile contains PII."
    )
    r = check_classification(claim, snapshot(DOCUMENTED))

    assert r.verdict is Verdict.SUPPORTED
    assert PII in r.evidence[0].value


def test_nonpii_tag_contradicts_a_pii_claim(snapshot) -> None:
    """The seed's trap: `recipient_email_hash` reads as PII and is tagged NonPII.

    An agent that reasons from the column NAME says PII. The catalog was explicit, so
    this is a genuine catch — not a coverage gap.
    """
    claim = ClassificationClaim(
        target_urn=NONPII_TRAP,
        labels=(PII,),
        field_path="recipient_email_hash",
        raw_text="The recipient email hash column contains PII.",
    )
    r = check_classification(claim, snapshot(NONPII_TRAP))

    assert r.verdict is Verdict.CONTRADICTED
    assert any(e.value == NON_PII for e in r.evidence)


def test_unclassified_table_is_insufficient(snapshot) -> None:
    claim = ClassificationClaim(
        target_urn=UNREVIEWED, labels=(PII,), raw_text="raw_events contains PII."
    )
    r = check_classification(claim, snapshot(UNREVIEWED))

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE


# --- the false-assurance failure mode ----------------------------------------


def test_pii_free_claim_on_an_unreviewed_table_is_NOT_supported(snapshot) -> None:
    """The most dangerous verdict Attest could get wrong.

    raw_events has no tags. A checker that asks "is PII attached? no -> the PII-free
    claim holds" returns Supported and certifies an unreviewed table as clean. That is
    a groundedness auditor actively manufacturing false assurance.

    Absence of a PII tag is not evidence of absence of PII. Nobody has looked.
    """
    claim = ClassificationClaim(
        target_urn=UNREVIEWED,
        labels=(PII,),
        present=False,
        raw_text="raw_events is PII-free.",
    )
    r = check_classification(claim, snapshot(UNREVIEWED))

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.verdict is not Verdict.SUPPORTED


def test_pii_free_claim_is_supported_when_the_catalog_affirms_it(snapshot) -> None:
    """email_campaign_stats is tagged NonPII: a positive statement, not an absence."""
    claim = ClassificationClaim(
        target_urn=NONPII_TRAP,
        labels=(PII,),
        present=False,
        raw_text="The campaign stats table is PII-free.",
    )
    r = check_classification(claim, snapshot(NONPII_TRAP))

    assert r.verdict is Verdict.SUPPORTED


def test_untagged_column_on_an_unreviewed_table_is_insufficient(snapshot) -> None:
    """legacy_accounts.email is untagged, and the table was never marked Verified.

    `email` is obviously PII to a human, and to a model. It is not PII to the CATALOG,
    which has never been told. Insufficient-Coverage, not Contradicted, not Supported:
    the checker must refuse to reason from the column's name. That refusal is the
    boundary between this layer and the semantic layer.
    """
    claim = ClassificationClaim(
        target_urn=DEPRECATED_UNOWNED,
        labels=(PII,),
        field_path="email",
        raw_text="The email column in legacy_accounts is PII.",
    )
    r = check_classification(claim, snapshot(DEPRECATED_UNOWNED))

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE


# --- the completeness marker: where closed-world reasoning is licensed ---------


def test_reviewed_table_with_no_pii_tag_contradicts_a_pii_claim(snapshot) -> None:
    """orders_fact is tagged Verified and carries no PII tag anywhere.

    Verified means the governance team reviewed it and tagged what they found. On such
    a table the ABSENCE of a PII tag is a considered finding, so a PII claim is
    Contradicted. This is the only route by which a missing tag becomes a denial, and
    it is granted by the catalog, not assumed by Attest.
    """
    claim = ClassificationClaim(
        target_urn=REVIEWED_CLEAN, labels=(PII,), raw_text="orders_fact contains PII."
    )
    r = check_classification(claim, snapshot(REVIEWED_CLEAN))

    assert r.verdict is Verdict.CONTRADICTED
    assert "classification-complete" in r.reason


def test_the_completeness_marker_is_what_makes_the_difference(snapshot) -> None:
    """The load-bearing comparison. Same claim, same absent tag, opposite verdicts.

    Both tables lack a PII tag. orders_fact is Verified; support_tickets is not. If
    these two came back the same, then either Attest is crying wolf on every
    unreviewed table, or it is unable to ever confirm a clean one. The completeness
    marker is precisely what separates them, and this test fails the moment that rule
    is removed.
    """
    reviewed = ClassificationClaim(
        target_urn=REVIEWED_CLEAN, labels=(PII,), raw_text="It contains PII."
    )
    unreviewed = ClassificationClaim(
        target_urn=DEPRECATED_UNOWNED, labels=(PII,), raw_text="It contains PII."
    )

    assert check_classification(reviewed, snapshot(REVIEWED_CLEAN)).verdict is Verdict.CONTRADICTED
    assert (
        check_classification(unreviewed, snapshot(DEPRECATED_UNOWNED)).verdict
        is Verdict.INSUFFICIENT_COVERAGE
    )


# --- mechanics ----------------------------------------------------------------


def test_glossary_terms_count_as_classification(snapshot) -> None:
    """Claims range over tags AND glossary terms; a checker reading only tags misses half."""
    claim = ClassificationClaim(
        target_urn=DOCUMENTED,
        labels=(EMAIL_TERM,),
        field_path="email",
        raw_text="The email column carries the Email Address term.",
    )
    assert check_classification(claim, snapshot(DOCUMENTED)).verdict is Verdict.SUPPORTED


def test_a_multi_label_claim_is_a_conjunction(snapshot) -> None:
    """"PII and Tier1" needs both. One contradicted label sinks the whole claim."""
    both = ClassificationClaim(
        target_urn=DOCUMENTED, labels=(PII, TIER1), raw_text="It is Tier1 and contains PII."
    )
    assert check_classification(both, snapshot(DOCUMENTED)).verdict is Verdict.SUPPORTED

    # customer_id is explicitly NonPII, so the PII half is denied outright.
    mixed = ClassificationClaim(
        target_urn=DOCUMENTED,
        labels=(PII, EMAIL_TERM),
        field_path="customer_id",
        raw_text="customer_id is PII and is an email address.",
    )
    assert check_classification(mixed, snapshot(DOCUMENTED)).verdict is Verdict.CONTRADICTED


def test_claim_about_a_nonexistent_column_is_contradicted(snapshot) -> None:
    """The schema is exhaustive, so a claim about a column it omits is denied, not unknown."""
    claim = ClassificationClaim(
        target_urn=DOCUMENTED,
        labels=(PII,),
        field_path="social_security_number",
        raw_text="The SSN column is PII.",
    )
    r = check_classification(claim, snapshot(DOCUMENTED))

    assert r.verdict is Verdict.CONTRADICTED
    assert "does not exist" in r.reason
