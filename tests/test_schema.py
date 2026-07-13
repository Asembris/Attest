"""SCHEMA against the live catalog. All three verdicts."""

from __future__ import annotations

from attest.checkers import check_schema
from attest.claims import ColumnAssertion, SchemaClaim, Verdict
from conftest import DOCUMENTED, NO_SCHEMA, REVIEWED_CLEAN


def test_existing_column_is_supported(snapshot) -> None:
    claim = SchemaClaim(
        target_urn=DOCUMENTED,
        columns=(ColumnAssertion(name="email", native_type="VARCHAR(255)"),),
        raw_text="The customer profile has an email column of type VARCHAR(255).",
    )
    r = check_schema(claim, snapshot(DOCUMENTED))

    assert r.verdict is Verdict.SUPPORTED
    assert "email" in r.evidence[0].value


def test_hallucinated_column_is_contradicted(snapshot) -> None:
    """The schema lists every column, so one it omits is one that does not exist."""
    claim = SchemaClaim(
        target_urn=REVIEWED_CLEAN,
        columns=(ColumnAssertion(name="ssn"),),
        raw_text="orders_fact has an ssn column.",
    )
    r = check_schema(claim, snapshot(REVIEWED_CLEAN))

    assert r.verdict is Verdict.CONTRADICTED
    assert "ssn" not in r.evidence[0].value


def test_missing_schema_is_insufficient_not_contradicted(snapshot) -> None:
    """external_report has no schemaMetadata at all.

    Absence of a schema is not absence of a column. Contradicted here would accuse the
    agent of hallucinating a column that very likely exists — the catalog simply never
    ingested the schema. This is the difference between "your agent made that up" and
    "we never looked".
    """
    claim = SchemaClaim(
        target_urn=NO_SCHEMA,
        columns=(ColumnAssertion(name="revenue_amount"),),
        raw_text="The external report has a revenue_amount column.",
    )
    r = check_schema(claim, snapshot(NO_SCHEMA))

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.verdict is not Verdict.CONTRADICTED
    assert r.evidence[0].value is None


def test_wrong_type_is_contradicted(snapshot) -> None:
    claim = SchemaClaim(
        target_urn=REVIEWED_CLEAN,
        columns=(ColumnAssertion(name="order_total", native_type="BOOLEAN"),),
        raw_text="order_total is a boolean flag.",
    )
    assert check_schema(claim, snapshot(REVIEWED_CLEAN)).verdict is Verdict.CONTRADICTED


def test_either_type_vocabulary_is_accepted(snapshot) -> None:
    """DataHub records 'VARCHAR(36)' and 'STRING' for the same column. A claim may use
    either, and precision arguments are not a claim anyone made."""
    snap = snapshot(DOCUMENTED)

    for asserted in ("VARCHAR(36)", "varchar", "string", "STRING"):
        claim = SchemaClaim(
            target_urn=DOCUMENTED,
            columns=(ColumnAssertion(name="customer_id", native_type=asserted),),
            raw_text=f"customer_id is a {asserted}.",
        )
        assert check_schema(claim, snap).verdict is Verdict.SUPPORTED, asserted


def test_existence_only_claims_ignore_type(snapshot) -> None:
    claim = SchemaClaim(
        target_urn=DOCUMENTED,
        columns=(ColumnAssertion(name="signup_ts"), ColumnAssertion(name="is_active")),
        raw_text="It has signup_ts and is_active columns.",
    )
    assert check_schema(claim, snapshot(DOCUMENTED)).verdict is Verdict.SUPPORTED


def test_a_multi_column_claim_is_a_conjunction(snapshot) -> None:
    """One hallucinated column sinks the claim, however many real ones sit beside it."""
    claim = SchemaClaim(
        target_urn=DOCUMENTED,
        columns=(
            ColumnAssertion(name="email"),
            ColumnAssertion(name="full_name"),
            ColumnAssertion(name="passport_number"),
        ),
        raw_text="It has email, full_name, and passport_number columns.",
    )
    r = check_schema(claim, snapshot(DOCUMENTED))

    assert r.verdict is Verdict.CONTRADICTED
    assert "passport_number" in r.reason
