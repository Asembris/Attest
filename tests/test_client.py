"""The client's contract with the checkers: what it must never quietly return.

A groundedness auditor that reads an empty response and calls it Insufficient-Coverage
is broken in the specific way that is hardest to notice — every verdict still has the
right shape.
"""

from __future__ import annotations

import pytest

from attest.datahub import DataHubClient, EntityNotFoundError
from conftest import DOCUMENTED, NO_SCHEMA, NO_TIMESTAMP, UNREVIEWED

GHOST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,no.such.table,PROD)"


def test_nonexistent_dataset_raises_rather_than_returning_empty(client: DataHubClient) -> None:
    """The correctness bug this guards against is live, not hypothetical.

    DataHub answers dataset(urn:) for ANY well-formed dataset URN — it echoes back
    urn/name/platform parsed straight out of the URN string and nulls every aspect. So
    a dataset that was never ingested is byte-identical to a real one with no metadata.
    Without the `exists` check, a typo'd URN would be scored Insufficient-Coverage on
    every claim, the audit would look clean, and the bad URN would never surface.
    """
    with pytest.raises(EntityNotFoundError) as exc:
        client.fetch_dataset(GHOST)
    assert exc.value.urn == GHOST

    assert client.get_dataset(GHOST) is None
    assert client.get_dataset(DOCUMENTED) is not None


def test_a_missing_entity_looks_exactly_like_an_empty_one_on_the_wire(
    client: DataHubClient,
) -> None:
    """Pins the server behaviour the guard above exists for.

    If DataHub ever starts returning null for an unknown URN, this test fails — and
    that is the signal to simplify, not a regression. It is here so the `exists` check
    cannot be deleted as redundant by someone who tries one URN and sees it work.
    """
    raw = client.execute(client.DATASET_QUERY, {"urn": GHOST})["dataset"]

    assert raw is not None, "DataHub no longer fabricates a response for unknown URNs"
    assert raw["exists"] is False
    assert raw["urn"] == GHOST
    for aspect in ("properties", "ownership", "tags", "glossaryTerms", "schemaMetadata"):
        assert raw[aspect] is None


def test_snapshot_preserves_the_absent_aspects(client: DataHubClient) -> None:
    """None means the catalog is silent. Everything Insufficient-Coverage rests on this."""
    assert client.fetch_dataset(NO_TIMESTAMP).last_modified is None
    assert client.fetch_dataset(NO_SCHEMA).fields is None

    unreviewed = client.fetch_dataset(UNREVIEWED)
    assert not unreviewed.owners
    assert not unreviewed.has_classification
    # ...but it does have a schema. Silence on one aspect is not silence on all.
    assert unreviewed.fields is not None


def test_snapshot_reads_the_populated_aspects(client: DataHubClient) -> None:
    snap = client.fetch_dataset(DOCUMENTED)

    assert snap.last_modified is not None
    assert snap.owners == ("urn:li:corpuser:alice.chen",)
    assert "urn:li:tag:PII" in snap.tags
    assert snap.has_classification

    email = snap.field("email")
    assert email is not None
    assert email.native_type == "VARCHAR(255)"
    assert email.data_type == "STRING"
    assert "urn:li:tag:PII" in email.labels
    assert "urn:li:glossaryTerm:EmailAddress" in email.labels

    assert snap.field("no_such_column") is None
