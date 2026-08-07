"""CorpGroup ownership: a group owner is an owner (Session 32).

THE DEFECT, and it is one line. `Owner.owner` is the GraphQL union `CorpUser | CorpGroup`,
and `client.DATASET_QUERY` selected `... on CorpUser` and nothing else. A union arm that
matches nothing contributes nothing, so a group-owned dataset came back

    {"owners": [{"owner": {}, "ownershipType": {...}}]}

— a present entry carrying no URN, which Session 23's `_urns` guard correctly refuses to
normalize. The result was `MalformedResponseError` -> `ClaimError`: the claim was never
scored, and the diagnosis blamed the RESPONSE when the response was fine and the QUERY was
incomplete. Measured on a catalog Attest did not author (CLAUDE.md §23): **15 of 67 datasets
unauditable**, invisible from inside because `seed/generate_seed.py` emitted `make_user_urn`
owners exclusively — the offline tier, the fixtures, the live tier and the 12-cell matrix
all contained corpuser owners and nothing else. A seed cannot exercise a shape it never
emits, which is Session 5's fake rule one level up.

--------------------------------------------------------------------------------
Why nothing below the query had to change
--------------------------------------------------------------------------------

The canonical representation of an owner is already its DataHub URN, and the URN carries its
own entity type in its prefix. So:

  * `OwnershipClaim.owner_urn` already accepts `urn:li:corpGroup:` (claims.py).
  * `DatasetSnapshot.owners` is `tuple[str, ...]` — opaque URN strings.
  * `_urns` reads `owner.urn`; it never knew which arm produced it and still does not.
  * `check_ownership` is set membership over those strings.

A group owner is therefore represented EXACTLY as a user owner is, and this file pins that:
no `__typename`, no typed owner model, no display-name comparison, no dedup, no checker
branch. The whole fix is one inline fragment.

--------------------------------------------------------------------------------
The vacuity check, and why it is a MockTransport rather than a hand-built dict
--------------------------------------------------------------------------------

`_gms` below resolves inline fragments the way GraphQL does: an owner's `urn` is emitted
only if the query TEXT carries the arm for that owner's type. So these tests drive the
SHIPPED `DATASET_QUERY` over a real `httpx` client, and deleting the CorpGroup arm makes
the group cases go red on their own — the fix is proven load-bearing offline, with no
separate sabotage command to remember (§18/§20/§22's precedent). Same technique, and same
reason, as `test_catalog_unavailable.py`: only the wire is faked, because the wire is the
one part that cannot be exercised offline.
"""

from __future__ import annotations

import json

import httpx
import pytest

from attest.checkers.ownership import check_ownership
from attest.claims import OwnershipClaim, Verdict
from attest.datahub import DataHubClient, MalformedResponseError
from attest.datahub.snapshot import DatasetSnapshot

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.x.y,PROD)"

ALICE = "urn:li:corpuser:alice.chen"
BOB = "urn:li:corpuser:bob.martinez"
PLATFORM = "urn:li:corpGroup:data-platform"
GOVERNANCE = "urn:li:corpGroup:governance"

# An owner entity type the query has no arm for. DataHub's OwnerType union is
# CorpUser | CorpGroup today; this stands in for whatever a future server adds.
UNSUPPORTED = "urn:li:serviceAccount:ingest-bot"

_ARMS = {
    "urn:li:corpuser:": "CorpUser",
    "urn:li:corpGroup:": "CorpGroup",
}


def _arm(owner_urn: str) -> str | None:
    for prefix, type_name in _ARMS.items():
        if owner_urn.startswith(prefix):
            return type_name
    return None


def _gms(owner_urns: list[str]) -> DataHubClient:
    """A REAL DataHubClient whose socket is a MockTransport that RESOLVES UNION ARMS.

    This is the load-bearing part. GraphQL emits an inline fragment's selections only for
    the concrete type that matched; an owner whose type has no arm in the query contributes
    an EMPTY object. So this handler reads the query Attest actually sent and emits `urn`
    only when the corresponding `... on <Type>` arm is present in it.

    Consequence: these tests exercise the shipped `DATASET_QUERY`. Remove the CorpGroup arm
    and every group case below fails, which is what makes the fix demonstrably load-bearing
    rather than merely present.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]
        owners = []
        for owner_urn in owner_urns:
            arm = _arm(owner_urn)
            selected = (
                {"urn": owner_urn}
                if arm is not None and f"... on {arm} {{" in query
                else {}
            )
            owners.append(
                {
                    "owner": selected,
                    "ownershipType": {"urn": "urn:li:ownershipType:__system__technical_owner"},
                }
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "dataset": {
                        "urn": URN,
                        "exists": True,
                        "name": "analytics.x.y",
                        "ownership": {"owners": owners},
                    }
                }
            },
        )

    client = DataHubClient(gms_url="http://gms.test")
    client._client = httpx.Client(
        base_url="http://gms.test", transport=httpx.MockTransport(handler)
    )
    return client


def _claim(owner_urn: str) -> OwnershipClaim:
    return OwnershipClaim(
        target_urn=URN, raw_text=f"{owner_urn} owns it", owner_urn=owner_urn
    )


# --- the gap: a group owner is an owner ---------------------------------------


def test_a_single_corpgroup_owner_is_read_as_an_owner() -> None:
    """THE HEADLINE. Before the CorpGroup arm this raised MalformedResponseError."""
    snap = _gms([PLATFORM]).fetch_dataset(URN)
    assert snap.owners == (PLATFORM,)


def test_a_corpgroup_owner_can_be_SUPPORTED() -> None:
    """The claim schema already allows a corpGroup URN; only the read was missing."""
    snap = _gms([PLATFORM]).fetch_dataset(URN)
    r = check_ownership(_claim(PLATFORM), snap)
    assert r.verdict is Verdict.SUPPORTED
    assert PLATFORM in r.evidence[0].value


def test_the_wrong_group_is_CONTRADICTED_under_the_UNCHANGED_ownership_semantics() -> None:
    """A populated owners list is exhaustive, so naming a group that is not in it is a
    positive denial — the same rule, unchanged, that already applied to users. The real
    group URN must appear in the evidence so the denial can be explained."""
    snap = _gms([PLATFORM]).fetch_dataset(URN)
    r = check_ownership(_claim(GOVERNANCE), snap)
    assert r.verdict is Verdict.CONTRADICTED
    assert PLATFORM in r.evidence[0].value
    assert GOVERNANCE not in r.evidence[0].value


def test_mixed_user_and_group_ownership_reads_BOTH_in_catalog_order() -> None:
    """Both owners, both verifiable, order exactly as the catalog listed them. Run in both
    orderings, so nothing can depend on which union arm happens to come first."""
    assert _gms([ALICE, PLATFORM]).fetch_dataset(URN).owners == (ALICE, PLATFORM)
    assert _gms([PLATFORM, ALICE]).fetch_dataset(URN).owners == (PLATFORM, ALICE)

    snap = _gms([ALICE, PLATFORM]).fetch_dataset(URN)
    assert check_ownership(_claim(ALICE), snap).verdict is Verdict.SUPPORTED
    assert check_ownership(_claim(PLATFORM), snap).verdict is Verdict.SUPPORTED
    assert check_ownership(_claim(BOB), snap).verdict is Verdict.CONTRADICTED


def test_multiple_groups_are_all_read() -> None:
    snap = _gms([PLATFORM, GOVERNANCE]).fetch_dataset(URN)
    assert snap.owners == (PLATFORM, GOVERNANCE)
    assert check_ownership(_claim(GOVERNANCE), snap).verdict is Verdict.SUPPORTED


# --- canonical identity: the URN, and only the URN ----------------------------


def test_a_group_display_name_cannot_affect_canonical_identity() -> None:
    """The CorpGroup arm requests `urn` and NOTHING ELSE, so there is no display name in the
    response to compare against. A parser cannot read what the query never asked for — the
    same enforcement style as `upsert_custom_assertion` having no `fieldPath` parameter.

    Pinned at both grains: the owners tuple AND `snapshot.identity`, which is the stored
    `snapshot_id` a published artifact carries. A rename in DataHub must not silently
    re-key every verdict decided before it.
    """
    renamed = {
        "urn": URN,
        "exists": True,
        "name": "analytics.x.y",
        "ownership": {
            "owners": [
                {
                    "owner": {
                        "urn": PLATFORM,
                        # Present in the payload, absent from the query, ignored by the parser.
                        "properties": {"displayName": "Data Platform (renamed 2026)"},
                    }
                }
            ]
        },
    }
    plain = {
        "urn": URN,
        "exists": True,
        "name": "analytics.x.y",
        "ownership": {"owners": [{"owner": {"urn": PLATFORM}}]},
    }

    a = DatasetSnapshot.from_graphql(renamed)
    b = DatasetSnapshot.from_graphql(plain)
    assert a.owners == b.owners == (PLATFORM,)
    assert a.identity == b.identity


def test_the_group_urn_is_preserved_VERBATIM_including_case() -> None:
    """`corpGroup` is mixed-case in DataHub's own URN scheme. Nothing may normalize it: a
    lowercased URN is a different URN and would silently stop matching the claim."""
    mixed = "urn:li:corpGroup:Data_Platform.EMEA"
    snap = _gms([mixed]).fetch_dataset(URN)
    assert snap.owners == (mixed,)
    assert check_ownership(_claim(mixed), snap).verdict is Verdict.SUPPORTED


# --- PRESERVATION: none of this may move --------------------------------------


def test_a_single_corpuser_owner_is_unchanged() -> None:
    """The CorpUser arm is untouched, character for character. This is the pin that says so."""
    snap = _gms([ALICE]).fetch_dataset(URN)
    assert snap.owners == (ALICE,)
    assert check_ownership(_claim(ALICE), snap).verdict is Verdict.SUPPORTED
    assert check_ownership(_claim(BOB), snap).verdict is Verdict.CONTRADICTED


def test_duplicate_owner_entries_are_PRESERVED_not_deduplicated() -> None:
    """`_urns` normalization semantics are unchanged, deliberately.

    DataHub can list one URN twice under two ownership types. Collapsing them would be a
    real change to snapshot and evidence content (`Catalog lists N owner(s)`) for CorpUser
    as well as CorpGroup, and it has nothing to do with reading a group. It is not made
    here; this pin fails if someone slips it in alongside.
    """
    assert _gms([PLATFORM, PLATFORM]).fetch_dataset(URN).owners == (PLATFORM, PLATFORM)
    assert _gms([ALICE, ALICE]).fetch_dataset(URN).owners == (ALICE, ALICE)


@pytest.mark.parametrize(
    "owners",
    [[UNSUPPORTED], [ALICE, UNSUPPORTED], [PLATFORM, UNSUPPORTED]],
    ids=["alone", "beside-a-user", "beside-a-group"],
)
def test_an_owner_type_with_no_arm_STILL_FAILS_CLOSED(owners: list[str]) -> None:
    """Adding one arm must not teach the parser to shrug at the arms it lacks.

    An owner type the query cannot select contributes `{}` — a present entry with no URN —
    and Session 23's guard still refuses it. The alternative failure mode is the dangerous
    one: silently SKIPPING the unreadable entry would shorten the owners list and turn a
    malformed response into a confident Contradicted. So the whole read fails, loudly.
    """
    with pytest.raises(MalformedResponseError):
        _gms(owners).fetch_dataset(URN)


def test_an_unsupported_owner_never_enters_the_owners_tuple() -> None:
    """The same rule stated at the normalization boundary rather than the client wrapper,
    so `_urns` cannot regress into fabricating or dropping the unreadable entry."""
    with pytest.raises(ValueError):
        DatasetSnapshot.from_graphql(
            {"urn": URN, "exists": True, "ownership": {"owners": [{"owner": {}}]}}
        )


def test_absent_and_empty_ownership_are_still_INSUFFICIENT_COVERAGE() -> None:
    """Absence is not contradiction, and it is not malformation either. Unmoved.

    Three states, still three: absent (`None`), present-empty (`()`), present-broken (raise).
    A group-shaped claim against a silent catalog is under-covered, never denied.
    """
    absent = DatasetSnapshot.from_graphql({"urn": URN, "exists": True})
    assert absent.owners is None
    r = check_ownership(_claim(PLATFORM), absent)
    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.evidence[0].value is None

    empty = DatasetSnapshot.from_graphql(
        {"urn": URN, "exists": True, "ownership": {"owners": []}}
    )
    assert empty.owners == ()
    r = check_ownership(_claim(PLATFORM), empty)
    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.evidence[0].value is None


# --- the vacuity check ---------------------------------------------------------


def test_the_corpgroup_arm_is_what_makes_the_group_readable() -> None:
    """THE VACUITY CHECK, in the suite rather than in a command someone has to remember.

    `_gms` resolves union arms off the query text, so this asks the same server the same
    question with the arm stripped out — reproducing EXACTLY what GMS returned before the
    fix — and requires the old refusal back. If the CorpGroup arm is ever deleted from
    `DATASET_QUERY`, the tests above go red on their own; this one says why, and fails if
    the group cases above ever start passing for some reason other than the arm.
    """
    legacy = DataHubClient.DATASET_QUERY.replace(
        "... on CorpGroup { urn }\n", ""
    ).replace("... on CorpGroup { urn }", "")
    assert "... on CorpGroup" not in legacy
    assert "... on CorpUser" in legacy, "the sabotage must remove ONLY the group arm"

    client = _gms([PLATFORM])
    client.DATASET_QUERY = legacy  # type: ignore[misc]

    with pytest.raises(MalformedResponseError):
        client.fetch_dataset(URN)
