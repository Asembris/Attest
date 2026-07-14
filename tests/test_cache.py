"""The run-scoped snapshot cache: fewer fetches, and — the real point — one ground truth.

Two properties are under test and only one of them is about speed.

**The fetch count drops.** Measured, not assumed: the fake catalog counts every call it
receives, so "20x redundant" is a number this file produces rather than a claim it repeats.

**A run is decided against ONE state of the catalog.** This is the correctness half, and
it is the reason the cache exists rather than a bonus that fell out of it. The catalog is
mutated MID-RUN below, and the audit must not notice: a verification tool that cannot say
which state of the world it verified against has not verified anything.

And the failure mode the whole design exists to make unreachable — a claim checked against
a snapshot from a DIFFERENT audit run — gets its own test. That is not a performance
regression, it is Attest reporting a verdict against a catalog that no longer exists,
which is the exact failure it was built to catch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from attest.claims import Verdict
from attest.datahub import DatasetSnapshot, EntityNotFoundError, SnapshotCache
from attest.graph import Pipeline
from attest.llm import LLM
from attest.report import CorrectionOutcome
from attest.trajectory import RESOLVE
from fakes import (
    FakeCatalog,
    FakeChat,
    claim_reply,
    dataset,
    explanation_reply,
    revision_reply,
)

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders.fact,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.nope.missing,PROD)"

ALICE = "urn:li:corpuser:alice.chen"
CAROL = "urn:li:corpuser:carol.davis"
PII = "urn:li:tag:PII"

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def catalog() -> dict[str, DatasetSnapshot]:
    """A fresh mutable catalog. Built per test: these tests change it on purpose."""
    return {
        SF: dataset(
            SF,
            last_modified=NOW - timedelta(hours=6),
            owners=(CAROL,),
            tags=(PII,),
            terms=(),
            custom_properties={},
        ),
        OTHER: dataset(OTHER, owners=(ALICE,), tags=(), terms=()),
    }


def ownership(owner: str, urn: str) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": urn,
        "raw_text": f"{urn} is owned by {owner}.",
        "owner_urn": owner,
    }


# --- the cache on its own ----------------------------------------------------


def test_one_entity_is_fetched_once_however_many_claims_ask_for_it():
    fake = FakeCatalog(catalog())
    cache = SnapshotCache(fake)

    first = cache.fetch_dataset(SF)
    for _ in range(9):
        assert cache.fetch_dataset(SF) is first, "a second read returned a different object"

    assert fake.fetched == [SF], "the catalog was queried more than once for one entity"
    assert cache.stats.lookups == 10
    assert cache.stats.fetches == 1
    assert cache.stats.hits == 9
    assert cache.stats.entities == 1
    assert cache.stats.redundancy == 10.0


def test_a_missing_entity_is_fetched_once_too_and_fails_the_same_way_every_time():
    """An agent that hallucinates one bad URN into five claims makes ONE lookup.

    And gets the same error five times. A miss is a fact about the catalog like any other,
    and re-asking could otherwise return a different answer to the same question inside one
    run — which is the incoherence this module exists to prevent, in its ugliest form.
    """
    fake = FakeCatalog(catalog())
    cache = SnapshotCache(fake)

    errors = []
    for _ in range(5):
        with pytest.raises(EntityNotFoundError) as exc:
            cache.fetch_dataset(GHOST)
        errors.append(exc.value)

    assert fake.fetched == [GHOST], "a URN that does not resolve was looked up again"
    assert all(e is errors[0] for e in errors), "the same bad URN produced different errors"
    assert cache.stats.fetches == 1
    assert cache.stats.hits == 4


def test_the_cache_does_not_hide_a_real_fetch_failure_behind_a_stale_hit():
    """A miss is remembered as a miss. It never resolves later in the same run."""
    fake = FakeCatalog(catalog())
    cache = SnapshotCache(fake)

    with pytest.raises(EntityNotFoundError):
        cache.fetch_dataset(GHOST)

    # The catalog gains the entity mid-run. The run must NOT see it: this run's ground
    # truth was fixed the moment it first looked, and half a report against each of two
    # catalog states is not a report.
    fake.snapshots[GHOST] = dataset(GHOST, owners=(ALICE,))

    with pytest.raises(EntityNotFoundError):
        cache.fetch_dataset(GHOST)
    assert fake.fetched == [GHOST]


# --- the cache in the pipeline -----------------------------------------------


def test_the_fetch_count_drops_and_the_report_says_by_how_much():
    """The receipt. Six claims over two datasets: two fetches, not six."""
    claims = [ownership(CAROL, SF) for _ in range(3)] + [
        ownership(ALICE, OTHER) for _ in range(3)
    ]
    fake = FakeCatalog(catalog())
    chat = FakeChat(
        replies=[
            claim_reply(claims),
            explanation_reply("the catalog lists an owner.", "Supported", []),
        ]
    )
    p = Pipeline(llm=LLM(client=chat), client=fake, now=NOW)
    report = p.run(f"{SF} and {OTHER} both have owners.")

    assert len(report.audits) == 6
    assert len(fake.fetched) == 2, f"6 claims over 2 datasets made {len(fake.fetched)} fetches"

    assert report.catalog.lookups == 6
    assert report.catalog.fetches == 2
    assert report.catalog.entities == 2
    assert report.catalog.redundancy == 3.0
    assert report.summary()["catalog_fetches"] == 2

    # And the trace shows which resolves were served from the run's own snapshot — the
    # step ran either way, so the trajectory is unbroken.
    resolves = report.trace.named(RESOLVE)
    assert len(resolves) == 6, "the resolve step must still run for every claim"
    assert sum(1 for s in resolves if s.outputs["cached"]) == 4


def test_every_claim_in_one_run_is_decided_against_the_same_snapshot():
    """THE CONSISTENCY PROPERTY. The catalog moves mid-run; the audit does not.

    Two claims about one dataset — 'carol owns it' and 'carol owns it' — with the owner
    swapped out from under the run in between. Without the cache the first is Supported and
    the second Contradicted, both correct against the catalog as each one saw it, and the
    report contradicts itself. This is a correctness bug in a verification tool, not a
    performance one.
    """
    fake = MutatingCatalog(catalog())
    chat = FakeChat(
        replies=[
            claim_reply([ownership(CAROL, SF), ownership(CAROL, SF)]),
            explanation_reply("the catalog lists an owner.", "Supported", []),
        ]
    )
    # Re-owned to alice the instant the first claim resolves.
    fake.after_first_fetch = lambda snaps: snaps.__setitem__(
        SF, dataset(SF, owners=(ALICE,), tags=(PII,), terms=(), custom_properties={})
    )

    p = Pipeline(llm=LLM(client=chat), client=fake, now=NOW)
    report = p.run(f"{SF} is owned by {CAROL}, and {SF} is owned by {CAROL}.")

    verdicts = {a.verdict for a in report.audits}
    assert len(report.audits) == 2
    assert verdicts == {Verdict.SUPPORTED}, (
        f"two claims about one dataset got different verdicts in one run: "
        f"{[a.verdict.value for a in report.audits]}. The catalog moved underneath the run."
    )
    assert report.catalog.fetches == 1


def test_a_new_run_re_fetches_and_never_reuses_the_last_run_s_snapshot():
    """THE FAILURE THIS FEATURE COULD INTRODUCE, and the assertion that it did not.

    A cache that outlived its run would verify today's claim against yesterday's catalog —
    Attest reporting a verdict against a world that no longer exists, which is precisely
    the failure it was built to catch. Cache within an audit for consistency; re-fetch for
    a new audit for correctness.

    The catalog is changed BETWEEN the two runs, and the second run must see the change.
    """
    fake = FakeCatalog(catalog())
    # The script twice over: ONE pipeline, run twice. The fake replays by call index, so
    # both runs get a decomposition and an explanation. Using a second Pipeline would prove
    # nothing — the point is that the SAME long-lived pipeline re-reads the catalog.
    chat = FakeChat(
        replies=[
            claim_reply([ownership(CAROL, SF)]),
            explanation_reply("the catalog lists an owner.", "Supported", []),
            claim_reply([ownership(CAROL, SF)]),
            explanation_reply("the catalog lists an owner.", "Contradicted", []),
        ]
    )
    p = Pipeline(llm=LLM(client=chat), client=fake, now=NOW, max_retries=0)

    first = p.run(f"{SF} is owned by {CAROL}.")
    assert first.audits[0].verdict is Verdict.SUPPORTED

    # Carol hands the table over. A stale snapshot would still say Supported.
    fake.snapshots[SF] = dataset(SF, owners=(ALICE,), tags=(PII,), terms=(), custom_properties={})

    second = p.run(f"{SF} is owned by {CAROL}.")
    assert second.audits[0].verdict is Verdict.CONTRADICTED, (
        "the second run reused the first run's snapshot and verified a claim against a "
        "catalog that no longer exists"
    )
    assert fake.fetched == [SF, SF], "the new run did not re-fetch"
    assert second.catalog.fetches == 1, "each run's receipt counts only its own fetches"


def test_the_correction_loop_re_checks_against_the_run_s_snapshot_not_a_re_fetch():
    """The rule revise.py already had, now a property of the whole run.

    The agent is held to the facts it was SHOWN. A re-fetch inside the loop would let the
    catalog move underneath it, and a correction could then pass or fail for a reason
    nobody in the transcript ever saw.
    """
    fake = FakeCatalog(catalog())
    chat = FakeChat(
        replies=[
            claim_reply([ownership(ALICE, SF)]),  # carol owns it, so: Contradicted
            explanation_reply("the catalog lists a different owner.", "Contradicted", []),
            revision_reply(ownership(CAROL, SF)),
        ]
    )
    p = Pipeline(llm=LLM(client=chat), client=fake, now=NOW)
    report = p.run(f"{SF} is owned by {ALICE}.")

    assert report.audits[0].correction.outcome is CorrectionOutcome.CORRECTED
    assert fake.fetched == [SF], "the correction loop re-fetched the catalog"
    assert report.catalog.fetches == 1


class MutatingCatalog(FakeCatalog):
    """A catalog that changes under the reader's feet, once, after the first fetch."""

    after_first_fetch = None

    def fetch_dataset(self, urn: str) -> DatasetSnapshot:
        snapshot = super().fetch_dataset(urn)
        if len(self.fetched) == 1 and self.after_first_fetch is not None:
            self.after_first_fetch(self.snapshots)
        return snapshot
