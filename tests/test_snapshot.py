"""A malformed catalog response must surface as an error, never a verdict (Session 23, Hole 2).

snapshot.py preserves the distinction between an ABSENT aspect (None — the catalog is
silent) and a PRESENT-EMPTY one (() — the aspect exists and holds nothing). A structurally
broken response is a THIRD thing, and it must not be laundered into either:

  * A present-but-URL-less association entry (`{"owner": null}`) used to normalize to an
    empty-string URN — a populated-LOOKING list of garbage — which drove a confident
    Contradicted ("alice is not the owner; the catalog lists ''"). 2a.
  * A wrong-shaped response (a field with no `fieldPath`, a `lastModified` that is a list,
    a null `urn`) raised a raw KeyError / AttributeError / ValidationError, which the
    resolve path (catching only DataHubError) let crash the WHOLE run. 2b.

Both close the same way: a malformed response becomes a `MalformedResponseError` — a
`DataHubError`, so it lands as a `ClaimError` at resolve, the same class as entity-not-found.

THE BOUNDARY, and the line the fix must not cross: a legitimately EMPTY ([]) or ABSENT
(null) association is VALID and stays Insufficient-Coverage. Over-raising there would turn
a correct IC verdict into a crash — absence collapsed to malformed, the same sin one door
over.
"""

from __future__ import annotations

import pytest

from attest.checkers.ownership import check_ownership
from attest.claims import OwnershipClaim, Verdict
from attest.datahub import DataHubClient, DataHubError, MalformedResponseError
from attest.datahub.snapshot import DatasetSnapshot

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)"
ALICE = "urn:li:corpuser:alice"


def _client_returning(dataset: dict) -> DataHubClient:
    """A real client, exercised OFFLINE: __init__ opens no socket, get_dataset is stubbed."""
    client = DataHubClient(gms_url="http://localhost:0")
    client.get_dataset = lambda urn: dataset  # type: ignore[method-assign]
    return client


def _own_claim() -> OwnershipClaim:
    return OwnershipClaim(target_urn=URN, raw_text="alice owns it", owner_urn=ALICE)


# --- the boundary: absent and empty are VALID, never malformed ----------------


def test_an_absent_association_is_None_and_does_not_raise() -> None:
    snap = DatasetSnapshot.from_graphql({"urn": URN, "exists": True})
    assert snap.owners is None  # the catalog is SILENT about ownership
    assert check_ownership(_own_claim(), snap).verdict is Verdict.INSUFFICIENT_COVERAGE


def test_a_legitimately_empty_association_is_present_empty_and_does_not_raise() -> None:
    """`[]` is 'the aspect exists and lists nobody' — a VALID state, Insufficient-Coverage.

    THE BOUNDARY the Hole 2 fix must not overreach. `[]` (present-empty, valid, IC) is not
    `[{"owner": null}]` (present-broken, malformed). An over-aggressive raise here would
    convert a correct IC verdict into a crash.
    """
    snap = DatasetSnapshot.from_graphql(
        {"urn": URN, "exists": True, "ownership": {"owners": []}}
    )
    assert snap.owners == ()  # present, empty — DISTINCT from None
    assert snap.owners is not None
    assert check_ownership(_own_claim(), snap).verdict is Verdict.INSUFFICIENT_COVERAGE


# --- 2a: a present-but-URL-less entry is MALFORMED, not a garbage owner --------


@pytest.mark.parametrize(
    "ownership",
    [
        {"owners": [{"owner": None}]},  # owner object null
        {"owners": [{"owner": {"properties": {"x": "y"}}}]},  # owner present, no urn
        {"owners": [{"owner": {"urn": ""}}]},  # empty-string urn
    ],
    ids=["owner-null", "owner-no-urn", "urn-empty"],
)
def test_a_present_but_urnless_owner_is_malformed_not_a_garbage_owner(ownership: dict) -> None:
    # It used to normalize to ('',) — a populated list — and drive a confident Contradicted.
    with pytest.raises(MalformedResponseError):
        _client_returning({"urn": URN, "exists": True, "ownership": ownership}).fetch_dataset(URN)


def test_from_graphql_itself_rejects_the_urnless_entry() -> None:
    """Pin the rejection at the normalization boundary, not only at the client wrapper —
    so `_urns` cannot regress back to fabricating an empty-string URN."""
    with pytest.raises(ValueError):
        DatasetSnapshot.from_graphql(
            {"urn": URN, "exists": True, "ownership": {"owners": [{"owner": None}]}}
        )


# --- 2b: wrong-shaped responses become MalformedResponseError, never a crash ---


@pytest.mark.parametrize(
    "dataset",
    [
        # a schema field with no fieldPath -> KeyError, unhandled, crashed the run
        {"urn": URN, "exists": True, "schemaMetadata": {"fields": [{"nativeDataType": "X"}]}},
        # lastModified is a list, not an object -> AttributeError
        {"urn": URN, "exists": True, "properties": {"lastModified": ["2026"]}},
        # urn present but null -> pydantic ValidationError
        {"urn": None, "exists": True},
        # a null tag object in a present list -> the fabrication, other grain
        {"urn": URN, "exists": True, "tags": {"tags": [{"tag": None}]}},
    ],
    ids=["field-no-fieldpath", "lastmodified-wrong-shape", "urn-null", "tag-null"],
)
def test_a_structurally_broken_response_is_malformed_not_a_crash(dataset: dict) -> None:
    # MalformedResponseError is a DataHubError, which is exactly what resolve catches — so
    # a broken response for one claim becomes a ClaimError, not an uncaught crash.
    assert issubclass(MalformedResponseError, DataHubError)
    with pytest.raises(DataHubError):
        _client_returning(dataset).fetch_dataset(URN)


# --- the happy path still parses (regression on the hardening) ----------------


def test_a_well_formed_response_still_parses() -> None:
    snap = _client_returning(
        {
            "urn": URN,
            "exists": True,
            "name": "x",
            "ownership": {"owners": [{"owner": {"urn": ALICE}}]},
            "tags": {"tags": [{"tag": {"urn": "urn:li:tag:Tier1"}}]},
        }
    ).fetch_dataset(URN)
    assert snap.owners == (ALICE,)
    assert snap.tags == ("urn:li:tag:Tier1",)
    assert check_ownership(_own_claim(), snap).verdict is Verdict.SUPPORTED
