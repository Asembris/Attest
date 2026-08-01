"""One audit run's view of the catalog.

The cost model made the shape of the real load obvious, and it is not tokens. A thousand
claims spread over fifty datasets is a thousand GraphQL fetches for fifty distinct
entities — twenty reads of the same table, inside one audit, to answer twenty questions
that were all decided against the same metadata. The fix is a dict.

--------------------------------------------------------------------------------
Why this is a CONSISTENCY boundary, and only incidentally a cache
--------------------------------------------------------------------------------

The speed is the smaller half of the argument, and if it were the whole argument this
module would be optional. It is not.

Without a cache, two claims about the same dataset in the same audit are checked against
two SEPARATE reads of the catalog. If someone re-tags the table between them, Attest
returns "contains PII: Supported" and "PII-free: Supported" in one report, both correct
against the catalog as each one saw it, and the report is incoherent. A verification tool
that cannot say WHICH state of the world it verified against has not verified anything.

So a run resolves each entity exactly once, and every claim in that run is decided against
that one snapshot. The report then means something precise: *these verdicts hold against
the catalog as it stood at the moment this run read it.* The correction loop already
depended on this — revise.py re-checks a revision against the same snapshot, not a
re-fetch, so the agent is held to the facts it was shown — and this generalizes the same
rule from one claim to the whole run.

--------------------------------------------------------------------------------
Why the lifetime is ONE RUN, and why cross-run caching would be a liability
--------------------------------------------------------------------------------

The redundancy lives entirely inside a single audit, so the useful lifetime of an entry is
seconds. That is a dict, not a network service. Redis buys cross-process sharing, and
there is no cross-process requirement here: the only reader of a run's snapshots is the
run itself. Adding one would buy a deployment dependency and a serialization format in
exchange for nothing.

Worse, it would buy something actively harmful. A snapshot cached across runs is a claim
about the catalog that may no longer be true, and Attest would then be verifying an
agent's statements against a catalog that no longer exists — grading today's claim against
last week's metadata and reporting the result as ground truth. That is precisely the
failure this project was built to catch, committed by the tool that catches it. A stale
verdict is worse than a slow one.

So: **cache within an audit, for consistency; always re-fetch for a new audit, for
correctness.** That is a design decision, not a missing feature.

The scoping is structural rather than disciplined. A cache is created inside
`Pipeline.run()`, stored on that run's ledger, and dropped with it. `Pipeline` holds no
cache of its own, so there is no object for a second run to reach: sharing one across runs
is not forbidden by a convention here, it is unreachable from the code.

Misses are cached too. A URN that does not resolve is a fact about the catalog like any
other, and an agent that hallucinates the same bad URN into five claims should produce one
lookup and five identical errors — not five lookups, and never five *different* answers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from attest.datahub.client import CatalogUnavailable, DataHubError
from attest.datahub.snapshot import DatasetSnapshot

log = logging.getLogger(__name__)


class Reader(Protocol):
    """The one method a cache needs from a catalog client. Keeps the fake trivial."""

    def fetch_dataset(self, urn: str) -> DatasetSnapshot: ...


@dataclass(frozen=True)
class CacheStats:
    """What the run actually asked the catalog for, and what it was spared.

    A receipt, like the token counts: measured from the calls that happened, never
    estimated. `fetches` is the number of GraphQL round trips the run really made, and it
    is what a before/after comparison is made of.
    """

    lookups: int = 0
    fetches: int = 0
    hits: int = 0
    entities: int = 0

    @property
    def redundancy(self) -> float:
        """Lookups per distinct entity — how many times the average dataset was asked for.

        1.0 means every claim was about a different dataset and the cache saved nothing.
        The interesting number is what it climbs to on a real workload: 1000 claims over
        50 datasets is 20.0, and it is 20 fetches instead of 1000.
        """
        return self.lookups / self.entities if self.entities else 0.0

    def display(self) -> str:
        return (
            f"{self.lookups} lookups over {self.entities} datasets: "
            f"{self.fetches} catalog fetches, {self.hits} served from the run's snapshot"
        )


class SnapshotCache:
    """The catalog, as one audit run sees it. Read-through, in-process, run-scoped.

    Exposes `fetch_dataset` and nothing else, so it is a drop-in for DataHubClient
    wherever a checker's snapshot comes from — the pipeline cannot tell the difference,
    which is what keeps this out of the verdict path's business.
    """

    def __init__(self, client: Reader) -> None:
        self._client = client
        self._snapshots: dict[str, DatasetSnapshot] = {}
        # A URN that failed to resolve, and the error it failed with. Re-raised verbatim,
        # so the second claim about a bad URN gets the same answer as the first.
        self._misses: dict[str, DataHubError] = {}
        self._lookups = 0
        self._fetches = 0
        self._hits = 0

    def fetch_dataset(self, urn: str) -> DatasetSnapshot:
        """The run's snapshot of `urn`, fetching it once and only once."""
        self._lookups += 1

        if urn in self._snapshots:
            self._hits += 1
            return self._snapshots[urn]
        if urn in self._misses:
            self._hits += 1
            raise self._misses[urn]

        self._fetches += 1
        try:
            snapshot = self._client.fetch_dataset(urn)
        except CatalogUnavailable:
            # NOT memoized. A miss is cached because it is a FACT ABOUT THE URN — the same
            # bad URN in five claims must produce one lookup and five identical errors. An
            # unreachable catalog is not a fact about the URN; it is Attest never having
            # asked. Recording it here would turn a two-second outage into a permanent
            # property of that entity for the rest of the run, which is the same absence-read-
            # as-an-answer collapse this module's own miss cache exists to keep honest.
            raise
        except DataHubError as exc:
            self._misses[urn] = exc
            raise

        self._snapshots[urn] = snapshot
        return snapshot

    def __contains__(self, urn: str) -> bool:
        return urn in self._snapshots or urn in self._misses

    @property
    def stats(self) -> CacheStats:
        return CacheStats(
            lookups=self._lookups,
            fetches=self._fetches,
            hits=self._hits,
            entities=len(self._snapshots) + len(self._misses),
        )
