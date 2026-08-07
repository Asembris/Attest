"""The client's contract with the checkers: what it must never quietly return.

A groundedness auditor that reads an empty response and calls it Insufficient-Coverage
is broken in the specific way that is hardest to notice — every verdict still has the
right shape.

**This is the INTEGRATION tier, and it is live on purpose.** It exercises GMS's wire
format and `DatasetSnapshot.from_graphql` against a real server — the parsing the offline
tier deliberately does NOT cover, plus the structured-property fabrication landmine that
only a real GMS reproduces. It is allowed to skip when DataHub is down (loudly — see
`conftest.pytest_terminal_summary`), because there is nothing to fake here without faking
the very thing under test.
"""

from __future__ import annotations

import pytest

from attest.datahub import DataHubClient, EntityNotFoundError, MalformedResponseError
from conftest import (
    DOCUMENTED,
    GROUP_OWNED,
    NO_SCHEMA,
    NO_TIMESTAMP,
    PLATFORM_GROUP,
    UNREVIEWED,
)

pytestmark = pytest.mark.integration

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


GHOST_PROPERTY = "urn:li:structuredProperty:attest.no_such_property"


def test_an_undefined_structured_property_reads_as_none_not_as_a_hollow_definition(
    client: DataHubClient,
) -> None:
    """The same fabrication, on the entity Attest WRITES to — and it breaks writes, not reads.

    DataHub synthesizes a structuredProperty entity for any well-formed URN, complete with
    a definition whose `qualifiedName` is the empty string. A bootstrap that asks "is this
    property defined?" and trusts a non-null answer is told YES about a property that does
    not exist, skips creating it, and its first upsert then dies inside GMS with
    "Unexpected null value found for ... Structured Property Definition" — an error that
    names the write and says nothing about the read that caused it.

    This is the write-path twin of the `exists` check above, and it is here so nobody
    deletes the qualifiedName test as a redundant null-check.
    """
    assert client.get_structured_property(GHOST_PROPERTY) is None

    # Pins the server behaviour the guard exists for. If DataHub ever starts returning
    # null here, this fails — and that is the signal to simplify, not a regression.
    raw = client.execute(
        client.STRUCTURED_PROPERTY_QUERY, {"urn": GHOST_PROPERTY}
    )["entity"]
    assert raw is not None, "DataHub no longer fabricates undefined structured properties"
    assert raw["definition"]["qualifiedName"] == "", (
        "the empty qualifiedName is what distinguishes a fabricated definition from a real "
        "one; if that changed, get_structured_property must change with it"
    )


def test_snapshot_preserves_the_absent_aspects(client: DataHubClient) -> None:
    """None means the catalog is silent. Everything Insufficient-Coverage rests on this."""
    assert client.fetch_dataset(NO_TIMESTAMP).last_modified is None
    assert client.fetch_dataset(NO_SCHEMA).fields is None

    unreviewed = client.fetch_dataset(UNREVIEWED)
    assert not unreviewed.owners
    assert not unreviewed.has_classification
    # ...but it does have a schema. Silence on one aspect is not silence on all.
    assert unreviewed.fields is not None


def test_a_corpgroup_owner_is_read_from_REAL_GMS(client: DataHubClient) -> None:
    """The union arm, against the server that actually resolves it.

    Only this tier can prove it. The offline tests drive the shipped query over a
    MockTransport that emulates union-arm resolution — which is exactly the machinery a
    fake has to invent, and therefore exactly the machinery that could be wrong. Here GMS
    itself decides which arm matches, so a `CorpGroup` group owner arriving as a real URN
    is the server's answer, not our stub's.
    """
    snap = client.fetch_dataset(GROUP_OWNED)
    assert snap.owners == (PLATFORM_GROUP,)


def test_without_the_corpgroup_arm_REAL_GMS_returns_an_ownerless_entry(
    client: DataHubClient,
) -> None:
    """THE VACUITY CHECK, against the real server: the arm is load-bearing.

    Ask GMS the same question with only the `CorpUser` arm and the group owner comes back
    as `{}` — a present entry carrying no URN — which `_urns` refuses. That is verbatim
    the pre-fix failure, reproduced on live GMS rather than asserted about it, and it is
    why every claim about a group-owned dataset used to surface as a `ClaimError`.

    Without this, the test above could pass for reasons that have nothing to do with the
    query, and deleting the arm would break the product while the suite stayed green.
    """
    legacy = DataHubClient.DATASET_QUERY.replace("... on CorpGroup { urn }", "")
    assert "... on CorpGroup" not in legacy
    assert "... on CorpUser" in legacy, "the sabotage must remove ONLY the group arm"

    owners = client.execute(legacy, {"urn": GROUP_OWNED})["dataset"]["ownership"]["owners"]
    assert owners, "the ownership aspect itself must still be present"
    assert owners[0]["owner"] == {}, "an unmatched union arm must contribute no fields"

    # And that shape is refused rather than normalized into a garbage owner.
    client.DATASET_QUERY = legacy  # type: ignore[misc]
    try:
        with pytest.raises(MalformedResponseError):
            client.fetch_dataset(GROUP_OWNED)
    finally:
        # The client fixture is session-scoped: leave the shipped query in place.
        del client.DATASET_QUERY

    assert "... on CorpGroup" in client.DATASET_QUERY


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
