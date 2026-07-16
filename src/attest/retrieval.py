"""Reading claim artifacts back out of the catalog. The inheritance half of the thesis.

Session 15 built the write path: every verdict a human publishes lands in DataHub as its
own non-overwriting claim artifact. But an artifact nobody can query is unfinished — the
whole point of writing it back is that *the next agent inherits it*, and inheriting means
retrieving.

Challenge 1's thesis has two halves and they are not equally hard:

    "Attest writes results back"          -- Session 15. Proven live.
    "the next agent can retrieve them"    -- this module.

--------------------------------------------------------------------------------
THE THREE-STATE READ, and why an absent verdict is not one fact but three
--------------------------------------------------------------------------------

This is the subtle, load-bearing part, and it is Attest's own thesis pointed at its own
read path.

The catalog can be silent about a claim's verdict for three completely different reasons,
and they are indistinguishable from DataHub alone:

    the write landed, and the index has not caught up yet  -> PENDING_LAG  (transient)
    the write FAILED at a step, so no verdict is coming     -> INCOMPLETE   (actionable)
    nothing here can say what was attempted                 -> UNKNOWN      (honest)

**Measured, on the pinned Core v1.5.0.6, before this was designed:** after a verdict is
accepted it takes a median 2.1s (max 3.2s across five trials) to become readable. So
PENDING_LAG is not hypothetical — it is the NORMAL state for the first couple of seconds
after every approval, and a reader that called it "broken" would be wrong several times a
day, loudly, on the happy path.

**The rule, and it is not negotiable: never render INCOMPLETE from DataHub's silence
alone.** Attest exists to say that absence is not an answer — an untagged table does not
mean "PII-free", it means nobody looked (policy.py). A read path that saw no verdict and
conveniently concluded "half-written" would be committing this project's cardinal sin in
this project's own output, which is exactly the mistake §10a caught in the write path.

**So the two questions are asked of two different systems, because they are two different
questions:**

    DataHub   "what does the catalog HOLD?"    -> the verdict, or silence
    the store "what did Attest DO?"            -> the write landed / failed at a step / nothing

Neither answers the other's question. The store cannot tell you what a second agent will
find, and the catalog cannot tell you whether anyone ever tried. `store.write_states`
supplies the second half; `ClaimArtifact.complete` supplies the first. That split IS the
disambiguation — remove either side and the state collapses back into a guess.

--------------------------------------------------------------------------------
THE BOUNDARY, NAMED: what a second agent reading DataHub ALONE cannot do
--------------------------------------------------------------------------------

**A reader with no access to Attest's store cannot distinguish PENDING_LAG from
INCOMPLETE.** Both look identical from the catalog: an artifact with `runEvents.total = 0`
and no `attest.verdict`. There is no field that separates them, and this module does not
pretend otherwise — the separation comes from Attest's own record, which by definition a
second agent does not have.

This is a real limit and it is stated rather than papered over. What makes it tolerable,
and NOT a hole in the thesis:

  * **It self-resolves in about two seconds.** A second agent that reads an artifact with
    no verdict and reads it again shortly after has its answer, because a lagging write
    completes and a failed one does not.
  * **It is confined to the write's own tail.** Every verdict that ever landed reads back
    completely and unambiguously, forever. The ambiguity exists only for a claim written
    seconds ago or written wrongly — never for the catalog's settled contents.
  * **The thesis question does not touch it.** *"Show me what the next agent inherits"* is
    `dataset.assertions(urn)` returning claims with their verdicts and full history — one
    server-side query, no Attest process, no ambiguity. What a second agent cannot do is
    diagnose Attest's own half-finished write, which is Attest's problem to report and not
    a fact the catalog owes anyone.

--------------------------------------------------------------------------------
HOW MUCH IS SERVER-SIDE, and the refusal to oversell it
--------------------------------------------------------------------------------

*"Retrievable from DataHub"* is TRUE. *"Fully queryable in DataHub"* would be FALSE, and
the difference is the sort of thing this project exists to catch, so every response says
exactly which predicates the catalog applied and which Attest applied afterwards.

The two server-side entry points are **DISJOINT and do not compose** (measured, spike §7):

    dataset.assertions(urn)          scopes to a dataset, filters NOTHING
    searchAcrossEntities([ASSERTION]) filters customType + tags, scopes to NOTHING

There is no server-side dataset scoping for assertions at all — nine candidate field names,
every one returning total=0 against a baseline that returns everything. That is an absence
in the index, not a name nobody guessed.

So: **DataHub does the scoping; Attest does most of the filtering.** What keeps that
honest rather than embarrassing is that the client-side half is a list comprehension over
one already-narrowed response — the run events come back inline, so there is no pagination
loop and no per-claim fetch.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from attest.claims import ClaimType
from attest.datahub import DataHubClient
from attest.store import AuditStore, WriteState
from attest.writeback import (
    CUSTOM_TYPE_PREFIX,
    ClaimArtifact,
    artifact_from_graphql,
    custom_type_for,
    verdict_tag_urn,
)

log = logging.getLogger(__name__)

ALL_CLAIM_TYPES: tuple[str, ...] = tuple(t.value for t in ClaimType)


class ReadState(StrEnum):
    """What a claim artifact's verdict actually IS in the catalog, right now.

    Four states, not two, and the third and fourth are the whole point: an absent verdict
    is not one fact. See the module docstring — and note that COMPLETE is decided by
    DataHub alone, while the other three are decided by what Attest's store says it did.
    """

    # The catalog holds the verdict. Read it. (DataHub is authoritative here whatever the
    # store says: a write recorded as failed whose verdict is nonetheless present IS
    # present — the catalog holds what it holds.)
    COMPLETE = "complete"
    # Attest's record says the write landed, and the catalog does not show it yet. The
    # index is catching up. TRANSIENT: measured at ~2s. Nothing to repair.
    PENDING_LAG = "pending-lag"
    # Attest's record says the write FAILED at a named step. The verdict is not coming on
    # its own. ACTIONABLE: POST /audit/{run_id}/writeback repairs it, idempotently.
    INCOMPLETE = "incomplete"
    # There is no verdict, and Attest's own record cannot say whether one was ever
    # attempted. NOT a synonym for INCOMPLETE, and calling it one would be reading absence
    # as an answer. This is the state a reader with only the catalog is ALWAYS in.
    UNKNOWN = "unknown"


def read_state(artifact: ClaimArtifact, wrote: WriteState | None) -> ReadState:
    """The three-state read, and it is deliberately four lines long.

    The whole disambiguation lives here, so there is exactly one place to look and exactly
    one place to break. Two systems are consulted because there are two questions:
    `artifact.complete` is *what the catalog holds*; `wrote.ok` is *what Attest did*.

    Order matters. The catalog is asked FIRST and its answer is final, because a verdict
    that is present is present — a write Attest recorded as failed may have landed anyway
    (the failure could be a timeout on a call the server completed), and reporting a
    readable verdict as broken because of our own bookkeeping would be trusting the record
    over the thing it describes.

    Only when the catalog is silent does the store's answer matter, and then it decides
    everything: the same silence is a transient, a bug, or an open question depending on
    it. `wrote is None` and `wrote.ok is None` are the same answer — nothing was written,
    so nothing here can explain the silence — and that is UNKNOWN, never INCOMPLETE.
    """
    if artifact.complete:
        return ReadState.COMPLETE
    if wrote is None or wrote.ok is None:
        return ReadState.UNKNOWN
    return ReadState.PENDING_LAG if wrote.ok else ReadState.INCOMPLETE


# --- what the catalog can and cannot filter -----------------------------------

DATASET_ASSERTIONS = "dataset.assertions"
SEARCH_ASSERTIONS = "searchAcrossEntities"

# WHICH CALLER PREDICATES EACH ENTRY POINT CAN PUSH DOWN. **DECLARED, not derived from the
# code that builds the query.** Read it off the query builder and it agrees with itself by
# construction and asserts nothing — the same reason trajectory.py declares the expected
# path instead of reading it off the graph's edges. This table is a CLAIM about what the
# server can do, and `test_retrieval.py` holds it to what actually goes over the wire.
#
# `reviewer` and `since` appear nowhere, and cannot: an assertion indexes `customType` and
# `tags` and nothing else, so there is no filter to push them into at any entry point.
PUSHES_DOWN: dict[str, frozenset[str]] = {
    DATASET_ASSERTIONS: frozenset({"target_urn"}),
    SEARCH_ASSERTIONS: frozenset({"verdict", "claim_type"}),
}


@dataclass(frozen=True)
class ClaimQuery:
    """What a caller asked for. Empty fields are not predicates."""

    target_urn: str = ""
    verdict: str = ""
    claim_type: str = ""
    reviewer: str = ""
    since: datetime | None = None

    def named(self) -> tuple[str, ...]:
        """The predicates the caller actually supplied, in a stable order."""
        present = (
            ("target_urn", self.target_urn),
            ("verdict", self.verdict),
            ("claim_type", self.claim_type),
            ("reviewer", self.reviewer),
            ("since", self.since),
        )
        return tuple(name for name, value in present if value)


@dataclass(frozen=True)
class Retrieval:
    """WHERE each predicate was applied. Part of the response, not a debug log.

    A caller who cannot see where the filtering happened cannot judge what it costs, and —
    worse — would reasonably read "I filtered by verdict" as "DataHub filtered by verdict".
    That is the difference between *retrievable from DataHub*, which is true, and *queryable
    in DataHub*, which is not. Attest does not get to blur that line in its own API.
    """

    entry_point: str
    pushed_down: tuple[str, ...]
    filtered_locally: tuple[str, ...]
    note: str = ""
    # How many artifacts the catalog returned BEFORE Attest filtered them. The cost of the
    # local half, stated: `considered - len(claims)` is what Attest threw away.
    considered: int = 0


@dataclass(frozen=True)
class RetrievedClaim:
    """One claim artifact as a reader sees it: what the catalog holds, and its state."""

    artifact: ClaimArtifact
    state: ReadState
    # Attest's own record of the write, when it has one. The reason `state` is not a guess,
    # and None exactly when the store has nothing to say (which is what UNKNOWN means).
    wrote: WriteState | None = None

    @property
    def repairable(self) -> bool:
        """Is there a failed write here that `POST /audit/{id}/writeback` would repair?

        **Keyed off the FAILED WRITE, not off the state**, and that distinction was found by
        running the checkpoint rather than by thinking about it. Gating on `INCOMPLETE` looks
        equivalent and is not, because a claim artifact is content-addressed: re-auditing a
        claim appends to the artifact it already has. So a claim audited last month, audited
        again today, whose new verdict FAILED to write, still shows last month's verdict —
        it reads COMPLETE (the catalog does hold a verdict; that is not a lie) while Attest's
        latest write sits recorded as broken. Gated on the state, that claim could never be
        repaired, and the stale verdict would stand indefinitely with the failure visible in
        the response and actionable from nowhere.

        A failed write is repairable whether or not an older verdict happens to be showing.

        What must NOT get a repair, and this is the rule that stays: PENDING_LAG. Its write
        landed (`ok is True`), there is nothing to fix, and offering a retry would invite a
        human to "fix" a two-second wait. UNKNOWN gets none either — there is no recorded
        write to re-run, and acting on absence as though it were a diagnosis is the whole
        mistake this module refuses.
        """
        return self.wrote is not None and self.wrote.ok is False


@dataclass(frozen=True)
class ClaimPage:
    """Claim artifacts, and an honest account of how they were found."""

    claims: tuple[RetrievedClaim, ...]
    retrieval: Retrieval


class ClaimReader:
    """Claim artifacts, out of DataHub, with Attest's record consulted only to explain
    silence.

    **The claims come from the CATALOG, never from the store**, and that is the whole
    thesis rather than an implementation detail: if this answered from SQLite the demo
    would prove nothing about what a second agent inherits. The store is consulted for
    exactly one thing — what Attest DID about a claim whose verdict the catalog is not
    showing — and it can never add a claim, remove a claim, or change a verdict.
    """

    def __init__(self, client: DataHubClient, store: AuditStore | None = None) -> None:
        self.client = client
        # Optional, and its absence is a supported mode rather than a broken one: a reader
        # with no store is exactly the second agent this feature exists for, and it gets
        # UNKNOWN where Attest would have said PENDING_LAG or INCOMPLETE. That is the
        # boundary in the module docstring, reachable in code.
        self.store = store

    def list(self, query: ClaimQuery, limit: int = 50) -> ClaimPage:
        """Claim artifacts matching `query`, and where each predicate was applied."""
        if query.target_urn:
            artifacts, retrieval = self._by_dataset(query, limit)
        else:
            artifacts, retrieval = self._by_search(query, limit)

        kept = tuple(a for a in artifacts if self._matches_locally(a, query, retrieval))
        return ClaimPage(
            claims=self._with_state(kept),
            retrieval=Retrieval(
                entry_point=retrieval.entry_point,
                pushed_down=retrieval.pushed_down,
                filtered_locally=retrieval.filtered_locally,
                note=retrieval.note,
                considered=len(artifacts),
            ),
        )

    def get(self, claim_urn: str) -> RetrievedClaim | None:
        """One claim artifact and its whole verdict history. None if the catalog has none."""
        node = self.client.get_assertion(claim_urn)
        if not node:
            return None
        artifact = artifact_from_graphql(node)
        return self._with_state((artifact,))[0]

    # --- the two entry points -------------------------------------------------

    def _by_dataset(
        self, query: ClaimQuery, limit: int
    ) -> tuple[tuple[ClaimArtifact, ...], Retrieval]:
        """Scoped by the catalog; everything else filtered here. THE THESIS QUERY.

        The dataset is the most selective predicate there is, so when a caller names one it
        is the one worth spending the server on — even though it means every other
        predicate comes back to Attest. `dataset.assertions` cannot filter at all.
        """
        nodes = self.client.list_dataset_assertions(query.target_urn, count=limit)
        artifacts = tuple(
            artifact_from_graphql(n)
            for n in nodes
            if ((n.get("info") or {}).get("customAssertion") or {})
            .get("type", "")
            .startswith(CUSTOM_TYPE_PREFIX)
        )
        return artifacts, self._declare(DATASET_ASSERTIONS, query, note=(
            "DataHub scoped this to the dataset; Attest applied the rest. "
            "`dataset.assertions` is a relationship read and cannot filter — there is no "
            "server-side dataset scoping for assertions in search, so these two entry "
            "points do not compose. Every claim on the dataset came back in one query and "
            "the remaining predicates are a filter over that response."
        ))

    def _by_search(
        self, query: ClaimQuery, limit: int
    ) -> tuple[tuple[ClaimArtifact, ...], Retrieval]:
        """Filtered by the catalog's index; scoped to nothing, because it cannot be."""
        nodes = self.client.search_assertions(
            custom_types=(
                [custom_type_for(query.claim_type)]
                if query.claim_type
                else [custom_type_for(t) for t in ALL_CLAIM_TYPES]
            ),
            tags=[verdict_tag_urn(query.verdict)] if query.verdict else [],
            count=limit,
        )
        artifacts = tuple(artifact_from_graphql(n) for n in nodes)

        note = (
            "DataHub filtered this in its index: claim type is `customType` and the latest "
            "verdict is a tag, which are the only two indexed fields on an assertion. It "
            "is scoped to nothing — search cannot restrict assertions to a dataset at all "
            "— so this is catalog-wide."
        )
        if query.verdict:
            # The honest caveat, and it is not theoretical: writeback's third step is the
            # tag, so a claim whose `tag` step failed has a CORRECT verdict and a stale
            # index. Filtering server-side by tag therefore misses it, while the same
            # filter applied to a dataset read finds it. Same question, two answers,
            # depending on an entry point the caller did not choose. Say so.
            note += (
                " NOTE: the verdict tag is a derived search index, not the verdict itself "
                "— the authoritative verdict is on the run event. A claim whose tag write "
                "failed (WriteResult.failed_step == 'tag') has a correct verdict and a "
                "stale tag, so it is missing from THIS result and would be found by a "
                "read scoped to its dataset. A stale tag costs findability, never "
                "correctness."
            )
        return artifacts, self._declare(SEARCH_ASSERTIONS, query, note=note)

    def _declare(self, entry_point: str, query: ClaimQuery, note: str) -> Retrieval:
        """Split the caller's predicates by what THIS entry point is declared to push down.

        Reads `PUSHES_DOWN`, which is a declaration about the server — deliberately not the
        query builder above, which would only ever agree with itself.
        """
        capable = PUSHES_DOWN[entry_point]
        named = query.named()
        return Retrieval(
            entry_point=entry_point,
            pushed_down=tuple(p for p in named if p in capable),
            filtered_locally=tuple(p for p in named if p not in capable),
            note=note,
        )

    # --- the local half -------------------------------------------------------

    def _matches_locally(
        self, artifact: ClaimArtifact, query: ClaimQuery, retrieval: Retrieval
    ) -> bool:
        """Apply exactly the predicates this entry point could not.

        Driven by `retrieval.filtered_locally` rather than by re-testing every field, so
        the report and the behaviour cannot come apart: a predicate reported as pushed down
        is NOT re-applied here, which means if the report were wrong the results would be
        wrong too, loudly, rather than the report quietly lying while the answer stayed
        right.
        """
        local = set(retrieval.filtered_locally)

        if "claim_type" in local and artifact.claim_type != query.claim_type:
            return False
        if "target_urn" in local and artifact.target_urn != query.target_urn:
            return False
        if "verdict" in local:
            # The LATEST verdict — the same thing the tag means, so the server-side and
            # local paths answer the same question. An artifact with no verdict at all
            # (INCOMPLETE, or lagging) matches no verdict filter: it has none to match.
            if artifact.verdict != query.verdict:
                return False
        if "reviewer" in local and not any(
            e.reviewer == query.reviewer for e in artifact.history
        ):
            # ANY verdict in the history, not just the latest: "what did alice sign off"
            # is a question about the whole history, and a reviewer whose verdict was later
            # superseded still signed it.
            return False
        if "since" in local and query.since is not None:
            cutoff = query.since.timestamp() * 1000
            if not any(e.at >= cutoff for e in artifact.history):
                return False
        return True

    def _with_state(
        self, artifacts: Sequence[ClaimArtifact]
    ) -> tuple[RetrievedClaim, ...]:
        """Join what the catalog holds to what Attest's record says it did."""
        states: dict[str, WriteState] = {}
        if self.store is not None and artifacts:
            states = self.store.write_states([a.claim_urn for a in artifacts])
        return tuple(
            RetrievedClaim(
                artifact=a,
                state=read_state(a, states.get(a.claim_urn)),
                wrote=states.get(a.claim_urn),
            )
            for a in artifacts
        )
