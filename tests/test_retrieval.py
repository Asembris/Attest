"""The retrieval path: what a reader gets back out of the catalog, and how honest it is.

Offline and free. Two things are under test and they fail in opposite directions:

**THE THREE-STATE READ.** A claim artifact with no verdict is not one fact. It is an index
still catching up (transient), a write that failed (actionable), or a question this store
cannot answer (honest). Each state is constructed DELIBERATELY below — there is no test
here that waits for a real lag and hopes — and the sabotage at the bottom proves the
distinction comes from the disambiguation rather than from luck.

**THE PUSH-DOWN REPORT.** Every `/claims` response says which predicates DataHub's index
applied and which Attest applied afterwards. That report is a CLAIM, and this project does
not ship claims nothing checks: `test_the_push_down_report_cannot_lie` holds it to what
actually went over the wire, in both directions.

What is NOT here, and cannot be: the real index lag, the fieldPath trap, the eventual
consistency. A fake has no index, so none of it executes. That is the Session 5 rule —
a fake cannot fail in a way the real thing fails through machinery the fake does not have
— and `just live` is the evidence for those. This tier proves the SHAPE of the read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from attest import retrieval, writeback
from attest.retrieval import ClaimQuery, ClaimReader, ReadState, read_state
from attest.store import AuditStore, WriteState
from fakes import FakeDataHub, dataset

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD)"

ALICE = "urn:li:corpuser:alice.chen"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def ownership_claim(owner: str = ALICE, urn: str = SF) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": urn,
        "owner_urn": owner,
        "raw_text": f"{urn} is owned by {owner}",
    }


def freshness_claim(urn: str = SF) -> dict:
    return {
        "claim_type": "freshness",
        "target_urn": urn,
        "max_age_hours": 24,
        "raw_text": f"{urn} is updated daily",
    }


@pytest.fixture
def catalog() -> FakeDataHub:
    return FakeDataHub({SF: dataset(SF), OTHER: dataset(OTHER)})


@pytest.fixture
def store(tmp_path) -> AuditStore:
    with AuditStore(tmp_path / "attest.db") as s:
        yield s


def upsert(catalog: FakeDataHub, claim: dict) -> str:
    """Put the CLAIM in the catalog and no verdict. A half-written artifact, exactly."""
    urn = writeback.claim_urn(claim)
    catalog.upsert_custom_assertion(
        urn=urn,
        entity_urn=claim["target_urn"],
        custom_type=writeback.custom_type_for(claim["claim_type"]),
        description=f"a claim about {claim['target_urn']}",
        platform_urn=writeback.ATTEST_PLATFORM_URN,
        logic=json.dumps({"claim_type": claim["claim_type"], "grain": "table"}),
    )
    return urn


def publish(catalog: FakeDataHub, claim: dict, verdict: str, at: datetime = NOW) -> str:
    """A COMPLETE write: the claim, its verdict, and its tag. What approval does."""
    result = writeback.write_claim_artifact(
        catalog,
        claim=claim,
        verdict=verdict,
        run_id="run-1",
        checked_at=at,
        reviewer="alice@example.com",
    )
    assert result.ok, result.detail
    return result.claim_urn


def wrote(claim_urn: str, ok: bool | None, step: str | None = None) -> WriteState:
    """What Attest's record says it did. The half of the read DataHub cannot supply."""
    return WriteState(
        claim_urn=claim_urn,
        run_id="run-1",
        claim_index=0,
        target_urn=SF,
        verdict="Supported",
        published=True,
        reviewer="alice@example.com",
        ok=ok,
        failed_step=step,
        at=NOW,
    )


# --- the three-state read, each state built on purpose -------------------------


def test_a_verdict_in_the_catalog_reads_COMPLETE(catalog):
    urn = publish(catalog, ownership_claim(), "Supported")

    claim = ClaimReader(catalog).get(urn)

    assert claim.state is ReadState.COMPLETE
    assert claim.artifact.verdict == "Supported"
    assert not claim.repairable, "a finished claim was offered a repair"


def test_a_landed_write_the_index_has_not_shown_yet_reads_PENDING_LAG(catalog):
    """The claim is written, the verdict is written, and the catalog does not show it yet.

    MEASURED at a median 2.1s on the pinned server, so this is the normal state for the
    first seconds after every approval — not an exotic failure. The catalog cannot tell
    this from a half-written claim; Attest's own record can, and that is the only reason
    this state exists rather than being reported as a bug several times a day.
    """
    urn = upsert(catalog, ownership_claim())  # the claim, deliberately without its verdict

    state = read_state(writeback.read_claim_artifact(catalog, urn), wrote(urn, ok=True))

    assert state is ReadState.PENDING_LAG


def test_a_write_that_FAILED_reads_INCOMPLETE_and_names_the_step(catalog, store):
    """The verdict is never coming, and the reader has to be told which step to repair.

    A failed `report` leaves a claim with no verdict; a failed `tag` leaves a verdict that
    is correct and merely not findable. Different facts, different fixes — which is why
    WriteResult names the step rather than carrying a boolean.
    """
    urn = upsert(catalog, ownership_claim())

    claim = retrieval.RetrievedClaim(
        artifact=writeback.read_claim_artifact(catalog, urn),
        state=read_state(
            writeback.read_claim_artifact(catalog, urn), wrote(urn, ok=False, step="report")
        ),
        wrote=wrote(urn, ok=False, step="report"),
    )

    assert claim.state is ReadState.INCOMPLETE
    assert claim.wrote.failed_step == "report"
    assert claim.repairable, "a half-written claim was not offered a repair"


def test_no_verdict_and_no_record_reads_UNKNOWN_and_NEVER_incomplete(catalog):
    """THE RULE, and it is the one this project exists to enforce.

    A verdict is absent and Attest's store cannot say whether one was ever attempted. That
    is not evidence the write failed — it is the absence of evidence, and calling it
    INCOMPLETE would be reading absence as an answer, in the read path of the tool built to
    catch exactly that. It is the same mistake as concluding an untagged table is PII-free.

    This is also the state a SECOND AGENT — reading DataHub with no Attest store, which is
    the whole scenario this feature exists for — is always in.
    """
    urn = upsert(catalog, ownership_claim())

    # No store at all: the second agent, by construction.
    claim = ClaimReader(catalog, store=None).get(urn)

    assert claim.state is ReadState.UNKNOWN
    assert claim.state is not ReadState.INCOMPLETE
    assert not claim.repairable, (
        "a claim nobody can vouch for was offered a repair — which would invite a human to "
        "'fix' a write that may never have been attempted"
    )


def test_a_claim_nobody_published_is_UNKNOWN_not_a_failed_write(catalog, store):
    """`writeback_ok = NULL` is 'nothing was written'. None-is-not-zero, at the read.

    A reviewer who withheld a verdict left no failed write to repair. Reporting one would
    invite a human to override a decision another human deliberately took.
    """
    urn = upsert(catalog, ownership_claim())

    state = read_state(writeback.read_claim_artifact(catalog, urn), wrote(urn, ok=None))

    assert state is ReadState.UNKNOWN


def test_a_RE_AUDITED_claim_whose_new_write_failed_is_still_repairable(catalog):
    """FOUND BY RUNNING THE CHECKPOINT, not by thinking about it.

    A claim artifact is content-addressed, so re-auditing a claim APPENDS to the artifact it
    already has — that is the feature. Which means a claim audited last month and audited
    again today, whose new verdict FAILED to write, still shows last month's verdict. It
    reads COMPLETE, and that is not a lie: the catalog does hold a verdict.

    But its latest write is broken, and if repairability were gated on `state is INCOMPLETE`
    this claim could never be repaired — the failure would be visible in the response and
    actionable from nowhere, and a stale verdict would stand indefinitely while Attest's own
    record said the newest audit never landed.

    So repairability keys off the FAILED WRITE. `state` says what the catalog holds;
    `repairable` says whether Attest has a write to re-run. Two questions again.
    """
    claim = ownership_claim()
    urn = publish(catalog, claim, "Supported", at=NOW - timedelta(days=30))

    # Today's audit reaches a new verdict, and its write breaks.
    broken = retrieval.RetrievedClaim(
        artifact=writeback.read_claim_artifact(catalog, urn),
        state=read_state(
            writeback.read_claim_artifact(catalog, urn), wrote(urn, ok=False, step="report")
        ),
        wrote=wrote(urn, ok=False, step="report"),
    )

    assert broken.state is ReadState.COMPLETE, "the catalog does hold last month's verdict"
    assert broken.artifact.verdict == "Supported"
    assert broken.repairable, (
        "a claim with a recorded FAILED write is not repairable because an older verdict "
        "happens to be showing — the stale verdict would stand forever"
    )


def test_a_lagging_claim_is_never_repairable_however_it_reads(catalog):
    """The rule that must survive keying repairability off the write.

    PENDING_LAG's write LANDED. There is nothing to fix, and a retry control there invites a
    human to "fix" a two-second wait. UNKNOWN has no recorded write to re-run at all.
    """
    urn = upsert(catalog, ownership_claim())

    lagging = retrieval.RetrievedClaim(
        artifact=writeback.read_claim_artifact(catalog, urn),
        state=ReadState.PENDING_LAG,
        wrote=wrote(urn, ok=True),
    )
    unknown = retrieval.RetrievedClaim(
        artifact=writeback.read_claim_artifact(catalog, urn),
        state=ReadState.UNKNOWN,
        wrote=None,
    )

    assert not lagging.repairable, "a landed write was offered a repair"
    assert not unknown.repairable, "absence was treated as a diagnosis"


def test_the_catalog_wins_when_it_holds_a_verdict_whatever_the_record_says(catalog):
    """A verdict that is present is present.

    Attest's record can say a write failed when it landed anyway — the failure may have
    been a timeout on a call the server completed. Trusting our own bookkeeping over the
    thing it describes would report a perfectly readable verdict as broken, and offer a
    repair for a claim that needs none.
    """
    urn = publish(catalog, ownership_claim(), "Contradicted")

    state = read_state(
        writeback.read_claim_artifact(catalog, urn), wrote(urn, ok=False, step="report")
    )

    assert state is ReadState.COMPLETE


def test_INSUFFICIENT_COVERAGE_is_a_verdict_and_never_an_incomplete_write(catalog):
    """The trap the design measured, at the read path.

    An Insufficient-Coverage claim and a half-written one BOTH read `succeeded=0 failed=0`
    in DataHub's rollup. If the state came off those counts, the catalog being silent about
    a claim (a verdict, and the load-bearing third one) would be indistinguishable from
    Attest never finishing (a bug). The discriminator is whether a verdict is STORED.
    """
    silent = publish(catalog, ownership_claim(), "Insufficient-Coverage")
    half = upsert(catalog, freshness_claim())

    reader = ClaimReader(catalog, store=None)
    verdict_claim = reader.get(silent)
    half_written = reader.get(half)

    assert verdict_claim.state is ReadState.COMPLETE
    assert verdict_claim.artifact.verdict == "Insufficient-Coverage"
    assert not verdict_claim.repairable, "a valid third verdict was reported as a bug"
    assert half_written.artifact.verdict is None
    assert half_written.state is not ReadState.COMPLETE


def test_the_store_can_only_explain_a_silence_it_can_never_change_a_verdict(catalog, store):
    """The claims come from the CATALOG. The store's reach stops at explaining absence.

    If the store could add, remove, or alter a claim, `/claims` would be Attest showing you
    its own notes and the thesis — "the next agent inherits this from DataHub" — would be
    unproven by its own demo.
    """
    publish(catalog, ownership_claim(), "Supported")

    with_store = ClaimReader(catalog, store).list(ClaimQuery(target_urn=SF))
    without = ClaimReader(catalog, store=None).list(ClaimQuery(target_urn=SF))

    assert [c.artifact.claim_urn for c in with_store.claims] == [
        c.artifact.claim_urn for c in without.claims
    ]
    assert [c.artifact.verdict for c in with_store.claims] == [
        c.artifact.verdict for c in without.claims
    ]


# --- the vacuity check: does any of the above BITE? ----------------------------


def test_collapsing_the_disambiguation_collapses_the_read(catalog, monkeypatch):
    """THE VACUITY CHECK. Revert the disambiguation and these states must stop separating.

    The naive read — "no verdict means half-written" — is what this module exists to refuse,
    and it is what a reasonable person writes if nobody explains why not. So: sabotage
    `read_state` back to it and prove the four states collapse into two.

    A test suite where the three-state read passes for a reason other than the three-state
    read is a green light wired to nothing. This is what proves it is wired to something.
    """
    lagging = upsert(catalog, ownership_claim())
    finished = publish(catalog, freshness_claim(), "Supported")

    healthy = ClaimReader(catalog, store=None)
    assert healthy.get(lagging).state is ReadState.UNKNOWN
    assert healthy.get(finished).state is ReadState.COMPLETE

    def naive(artifact, wrote):
        # "It has no verdict, so the write must have broken." Reading absence as an answer.
        return ReadState.COMPLETE if artifact.complete else ReadState.INCOMPLETE

    monkeypatch.setattr(retrieval, "read_state", naive)

    sabotaged = ClaimReader(catalog, store=None)
    assert sabotaged.get(lagging).state is ReadState.INCOMPLETE, (
        "the sabotage did not take — this test is not exercising read_state at all"
    )
    # A claim nobody can vouch for is now reported as broken, to every reader and to the UI:
    # the honest "we cannot say" has become a confident accusation, from no new evidence.
    assert sabotaged.get(finished).state is ReadState.COMPLETE, (
        "the sabotage broke more than the disambiguation — a verdict that IS in the catalog "
        "must still read complete, or this test proves nothing about the interesting case"
    )
    # Note what does NOT follow, and it is deliberate defence in depth: `repairable` keys off
    # the recorded WRITE rather than off the state, so even this sabotage cannot conjure a
    # repair button for a claim with no recorded failure. The naive read still misinforms
    # every reader; it just cannot also invite them to act on it.
    assert not sabotaged.get(lagging).repairable


# --- the push-down report ------------------------------------------------------


def sent_to_the_server(catalog: FakeDataHub) -> str:
    """Everything actually asked of DataHub, as one searchable blob."""
    return json.dumps({"reads": catalog.dataset_reads, "searches": catalog.searches})


def test_naming_a_dataset_pushes_the_dataset_down_and_nothing_else(catalog):
    """`dataset.assertions` scopes and filters NOTHING, so everything else comes back here.

    The dataset is the most selective predicate there is, so it is the one worth the
    server — but naming it means the verdict filter is Attest's job, because the two entry
    points are disjoint and do not compose.
    """
    publish(catalog, ownership_claim(), "Supported")
    publish(catalog, freshness_claim(), "Contradicted")

    page = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, verdict="Contradicted"))

    assert page.retrieval.entry_point == "dataset.assertions"
    assert page.retrieval.pushed_down == ("target_urn",)
    assert page.retrieval.filtered_locally == ("verdict",)
    assert page.retrieval.considered == 2, "the catalog returned both claims on the dataset"
    assert len(page.claims) == 1, "Attest did not apply the filter it said it applied"
    assert page.claims[0].artifact.verdict == "Contradicted"


def test_without_a_dataset_the_index_filters_verdict_and_claim_type(catalog):
    publish(catalog, ownership_claim(), "Supported")
    publish(catalog, freshness_claim(), "Contradicted")

    page = ClaimReader(catalog).list(ClaimQuery(verdict="Contradicted"))

    assert page.retrieval.entry_point == "searchAcrossEntities"
    assert page.retrieval.pushed_down == ("verdict",)
    assert page.retrieval.filtered_locally == ()
    assert len(page.claims) == 1


def test_reviewer_and_since_can_NEVER_be_pushed_down(catalog):
    """An assertion indexes `customType` and `tags`. There is no third filter to reach for.

    Reported as local at BOTH entry points, because it is true at both — a caller who saw
    `reviewer` under `pushed_down` would believe DataHub can answer a question it cannot.
    """
    publish(catalog, ownership_claim(), "Supported")

    scoped = ClaimReader(catalog).list(
        ClaimQuery(target_urn=SF, reviewer="alice@example.com")
    )
    searched = ClaimReader(catalog).list(
        ClaimQuery(verdict="Supported", reviewer="alice@example.com", since=NOW - timedelta(days=1))
    )

    assert "reviewer" in scoped.retrieval.filtered_locally
    assert "reviewer" in searched.retrieval.filtered_locally
    assert "since" in searched.retrieval.filtered_locally
    assert searched.retrieval.pushed_down == ("verdict",)


def test_the_push_down_report_cannot_lie(catalog):
    """THE ANTI-LIE TEST. Hold the report to what actually went over the wire.

    The report is the one part of `/claims` a caller cannot verify for themselves, and it
    is exactly the sort of claim this project refuses to ship unchecked: "DataHub filtered
    this" reads as *the catalog answered your question* and would be a false statement about
    where the evidence came from — published by the tool built to catch false statements
    about where evidence came from.

    Both directions, because a report can lie in both:
      * a predicate reported as PUSHED DOWN must appear in what was sent, or Attest is
        taking credit the server never gave it;
      * a predicate reported as FILTERED LOCALLY must NOT appear in what was sent, or the
        report is understating the server and the note is noise.
    """
    publish(catalog, ownership_claim(), "Supported")

    values = {
        "target_urn": SF,
        "verdict": writeback.verdict_tag_urn("Supported"),
        "claim_type": writeback.custom_type_for("ownership"),
        "reviewer": "alice@example.com",
    }

    queries = [
        ClaimQuery(target_urn=SF, verdict="Supported", reviewer="alice@example.com"),
        ClaimQuery(verdict="Supported", claim_type="ownership"),
        ClaimQuery(claim_type="ownership", reviewer="alice@example.com"),
        ClaimQuery(target_urn=SF),
    ]

    for query in queries:
        fresh = FakeDataHub({SF: dataset(SF), OTHER: dataset(OTHER)})
        publish(fresh, ownership_claim(), "Supported")
        # Only the retrieval read may be inspected; the write above also talks to the fake.
        fresh.dataset_reads.clear()
        fresh.searches.clear()

        page = ClaimReader(fresh).list(query)
        wire = sent_to_the_server(fresh)

        for predicate in page.retrieval.pushed_down:
            assert values[predicate] in wire, (
                f"{query} reports `{predicate}` as PUSHED DOWN to "
                f"{page.retrieval.entry_point}, but no request carried it: {wire}"
            )
        for predicate in page.retrieval.filtered_locally:
            if predicate not in values:
                continue  # `since` has no single wire form to look for
            assert values[predicate] not in wire, (
                f"{query} reports `{predicate}` as filtered LOCALLY, but it WAS sent to "
                f"{page.retrieval.entry_point}: {wire}. The report understates the server."
            )


def test_a_report_that_claims_the_server_filtered_the_verdict_is_caught(catalog, monkeypatch):
    """THE VACUITY CHECK for the anti-lie test: make it lie, and prove it is caught.

    A guarantee that only ever passes proves nothing. So declare that `dataset.assertions`
    pushes the verdict down — it does not, it filters nothing — and confirm the test above
    fails rather than shrugging.
    """
    monkeypatch.setitem(
        retrieval.PUSHES_DOWN, retrieval.DATASET_ASSERTIONS, frozenset({"target_urn", "verdict"})
    )
    publish(catalog, ownership_claim(), "Supported")
    catalog.dataset_reads.clear()
    catalog.searches.clear()

    page = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, verdict="Supported"))
    wire = sent_to_the_server(catalog)

    assert "verdict" in page.retrieval.pushed_down, "the sabotage did not take"
    assert writeback.verdict_tag_urn("Supported") not in wire, (
        "the report now CLAIMS DataHub filtered by verdict, and the wire proves it did not "
        "— which is precisely what test_the_push_down_report_cannot_lie exists to catch"
    )


def test_the_report_drives_the_filtering_so_a_wrong_report_cannot_hide(catalog, monkeypatch):
    """A lying report must produce WRONG RESULTS, not a quiet lie beside a right answer.

    `_matches_locally` is driven by `filtered_locally` rather than re-testing every field
    independently. That is deliberate: it welds the report to the behaviour, so the report
    cannot drift into being decoration that happens to be wrong while the answers stay
    right. Here, a report that wrongly claims the server filtered the verdict skips the
    local filter — and the unfiltered claim comes back, loudly.
    """
    publish(catalog, ownership_claim(), "Supported")
    publish(catalog, freshness_claim(), "Contradicted")

    honest = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, verdict="Contradicted"))
    assert len(honest.claims) == 1

    monkeypatch.setitem(
        retrieval.PUSHES_DOWN,
        retrieval.DATASET_ASSERTIONS,
        frozenset({"target_urn", "verdict"}),
    )
    lying = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, verdict="Contradicted"))
    assert len(lying.claims) == 2, (
        "the report and the filtering are not welded together: the report can be wrong "
        "while the answer stays right, which is a lie nobody would notice"
    )


# --- the local filters ---------------------------------------------------------


def test_a_dataset_read_finds_a_claim_whose_verdict_tag_never_landed(catalog):
    """The stale-tag asymmetry, and why the response warns about it.

    The tag is written LAST and is a derived search index, never the verdict. A claim whose
    tag step failed has a perfectly correct verdict and is missing from every tag-filtered
    search — so the same question answers differently depending on an entry point the caller
    did not pick. A stale tag costs findability, never correctness, and the note says so
    rather than letting the search result read as complete.
    """
    urn = publish(catalog, ownership_claim(), "Contradicted")
    catalog.tagged[urn].clear()  # the tag step failed: verdict correct, index stale
    catalog.assertions[urn]["tags"] = {"tags": []}

    searched = ClaimReader(catalog).list(ClaimQuery(verdict="Contradicted"))
    scoped = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, verdict="Contradicted"))

    assert len(searched.claims) == 0, "the fake's tag filter is not modelling the index"
    assert len(scoped.claims) == 1, "the dataset read lost a claim whose verdict is correct"
    assert scoped.claims[0].artifact.verdict == "Contradicted"
    assert "stale" in searched.retrieval.note, (
        "a verdict-filtered search can silently miss a claim, and the response does not say so"
    )


def test_reviewer_matches_any_verdict_in_the_history_not_only_the_latest(catalog):
    """"What did alice sign off" is a question about the history, not about the present.

    A reviewer whose verdict was later superseded still signed it, and an audit trail that
    forgot that would lose the accountability it exists for.
    """
    claim = ownership_claim()
    writeback.write_claim_artifact(
        catalog, claim=claim, verdict="Supported", run_id="run-1",
        checked_at=NOW - timedelta(days=30), reviewer="alice@example.com",
    )
    writeback.write_claim_artifact(
        catalog, claim=claim, verdict="Contradicted", run_id="run-2",
        checked_at=NOW, reviewer="bob@example.com",
    )

    alice = ClaimReader(catalog).list(ClaimQuery(target_urn=SF, reviewer="alice@example.com"))

    assert len(alice.claims) == 1
    assert alice.claims[0].artifact.verdict == "Contradicted", (
        "the LATEST verdict is bob's; alice matched because she signed an EARLIER one"
    )
    assert len(alice.claims[0].artifact.history) == 2


def test_since_matches_a_claim_with_any_verdict_in_the_window(catalog):
    publish(catalog, ownership_claim(), "Supported", at=NOW - timedelta(days=30))
    publish(catalog, freshness_claim(), "Supported", at=NOW)

    recent = ClaimReader(catalog).list(
        ClaimQuery(target_urn=SF, since=NOW - timedelta(days=1))
    )

    assert len(recent.claims) == 1
    assert recent.claims[0].artifact.claim_type == "freshness"


# --- Session 21, gap 1: pagination round-trips the total, truncation is NAMED --------
#
# The fake models the CONTRACT (up-to-limit nodes plus the catalog's total), not the client's
# start/count loop — that is server machinery a fake does not have, and `just live` is the
# evidence for it. What these prove is the shape: past the cap the reader returns everything,
# and a page cut off at the limit says so rather than letting a claim be silently absent.


def _publish_many(catalog: FakeDataHub, n: int) -> None:
    """Publish `n` DISTINCT claims on one dataset. Distinct owners hash to distinct
    content-addressed artifacts — the >50 that the 50-cap used to hide."""
    for i in range(n):
        publish(catalog, ownership_claim(owner=f"urn:li:corpuser:user{i:03d}"), "Supported")


def test_a_listing_past_the_cap_returns_EVERYTHING_and_round_trips_the_total(catalog):
    """GAP 1. Fifty was a silent cap, and silent absence reads as 'no such claim'.

    Exercised PAST the cap on purpose — a pagination test that never exceeds one page proves
    nothing. Fifty-five claims on one dataset: with the limit raised the reader returns all of
    them, and the catalog's own total rides back so a full read is known to be full.
    """
    _publish_many(catalog, 55)

    full = ClaimReader(catalog, store=None).list(ClaimQuery(target_urn=SF), limit=200)

    assert len(full.claims) == 55, "the listing dropped claims past the old 50-cap"
    assert full.retrieval.total == 55, "the catalog's total did not round-trip"
    assert "TRUNCATED" not in full.retrieval.note, "a complete page was reported as truncated"


def test_a_truncated_listing_SAYS_it_is_truncated_never_silently(catalog):
    """GAP 1's load-bearing half: absence past the limit must be NAMED on the response.

    Silent absence is indistinguishable from 'no such claim', the collapse this whole project
    refuses. So a page cut off at the limit carries the catalog's total and says, on the
    response itself, that there is more — the caller never has to infer it by counting.
    """
    _publish_many(catalog, 55)

    page = ClaimReader(catalog, store=None).list(ClaimQuery(target_urn=SF), limit=50)

    assert len(page.claims) == 50
    assert page.retrieval.total == 55, "the catalog's total was discarded"
    assert page.retrieval.considered == 50
    assert "TRUNCATED" in page.retrieval.note, (
        "a claim past the limit is absent from the listing and the response does not say so — "
        "which is the silent absence gap 1 exists to remove"
    )


def test_discarding_the_total_hides_the_truncation__the_gap_1_vacuity_check(catalog, monkeypatch):
    """THE VACUITY CHECK for gap 1: restore the old behaviour, prove the signal vanishes.

    The bug was fetching at most a page and reporting no honest total — so a 55-claim dataset
    read as 50 and nothing said otherwise. Sabotage the catalog read back to that (cap the
    fetch, report total == returned) and confirm both the extra claims and the truncation note
    disappear: the real total is exactly what surfaces the absence.
    """
    _publish_many(catalog, 55)
    real = catalog.list_dataset_assertions

    def capped(dataset_urn: str, limit: int | None = 50) -> tuple[list, int]:
        nodes, _total = real(dataset_urn, limit=50)
        return nodes, len(nodes)  # total == returned: the old silent cap

    monkeypatch.setattr(catalog, "list_dataset_assertions", capped)

    page = ClaimReader(catalog, store=None).list(ClaimQuery(target_urn=SF), limit=200)

    assert len(page.claims) == 50, "the sabotage did not cap the fetch"
    assert page.retrieval.total == 50, "with the cap restored, total collapses to what came back"
    assert "TRUNCATED" not in page.retrieval.note, (
        "the truncation note fired without a real total to compare against — the note is only "
        "honest because the total is, and this proves the total is what drives it"
    )


# --- Session 21, gap 2: Insufficient-Coverage evidence round-trips, absence and all ---


def test_IC_evidence_round_trips_through_the_catalog_with_absence_preserved(catalog):
    """GAP 2. An Insufficient-Coverage verdict's evidence IS the absence, and it must survive.

    The write-back used to flatten evidence to a string and DROP every `value is None` row —
    so an IC verdict, whose evidence is often just the catalog's silence, inherited back as an
    empty string. Here the structured evidence goes to the catalog and a store=None reader
    reconstructs it field-by-field, and the absence decodes back as `None` SPECIFICALLY: not
    '', not [], not a missing row. That is the one assertion that cannot be hollow.
    """
    claim = freshness_claim()
    evidence = [
        {"field": "properties.lastModified.time", "value": None, "note": "aspect absent"},
        {"field": "max_age_hours", "value": 24, "note": "claimed window"},
    ]
    writeback.write_claim_artifact(
        catalog,
        claim=claim,
        verdict="Insufficient-Coverage",
        run_id="run-ic",
        checked_at=NOW,
        evidence=evidence,
        snapshot_id="sha256:deadbeefcafe",
        reviewer="alice@example.com",
    )

    event = ClaimReader(catalog, store=None).get(writeback.claim_urn(claim)).artifact.history[0]
    by_field = {e.field: e for e in event.evidence}

    assert set(by_field) == {"properties.lastModified.time", "max_age_hours"}
    absent = by_field["properties.lastModified.time"]
    # THE assertion the whole gap turns on: absence decodes back as None, distinct from empty.
    assert absent.value is None, "the catalog is SILENT here, and that has to read as None"
    assert absent.value != "" and absent.value != [], (
        "None collapsed to an empty value — absence read as 'the catalog said empty', which is "
        "the exact confusion Insufficient-Coverage exists to keep apart"
    )
    assert absent.note == "aspect absent", "the note explaining the absence was lost"
    assert by_field["max_age_hours"].value == 24
    # gap 2b: the snapshot the verdict was decided against rides back too.
    assert event.snapshot_id == "sha256:deadbeefcafe"


def test_collapsing_absence_to_empty_breaks_the_round_trip__the_gap_2_vacuity_check(
    catalog, monkeypatch
):
    """THE VACUITY CHECK for gap 2: drop None at serialization, prove the absence stops
    round-tripping.

    Sabotage `_dump_evidence` back to the old drop-the-null behaviour and confirm no `None`
    survives to the reader. An IC verdict whose only evidence was the absence then inherits
    back with NO evidence at all — lamer than the one Attest computed, which is the whole gap.
    Asserting the DECODED value is None (above) is what catches this; asserting merely that
    'evidence came back non-empty' would pass while the absence was silently gone.
    """
    def drop_none(items):
        return json.dumps(
            [
                {"field": e.get("field", ""), "value": e.get("value"), "note": e.get("note")}
                for e in items
                if e.get("value") is not None
            ],
            sort_keys=True,
        )

    monkeypatch.setattr(writeback, "_dump_evidence", drop_none)

    claim = freshness_claim()
    writeback.write_claim_artifact(
        catalog,
        claim=claim,
        verdict="Insufficient-Coverage",
        run_id="run-ic",
        checked_at=NOW,
        evidence=[{"field": "properties.lastModified.time", "value": None, "note": "absent"}],
    )

    event = ClaimReader(catalog, store=None).get(writeback.claim_urn(claim)).artifact.history[0]
    assert all(e.value is not None for e in event.evidence), (
        "with absence dropped at serialization, no None reaches the reader — this is precisely "
        "what the healthy round-trip must prevent"
    )
    assert not any(e.field == "properties.lastModified.time" for e in event.evidence), (
        "the only evidence row was the absence, and dropping it left the IC verdict with no "
        "evidence at all — the lameness gap 2 removes"
    )


# --- Session 21, gap 3: a stale verdict tag is detected on READ, from the artifact alone ---


def test_a_stale_tag_is_detected_from_the_artifact_alone(catalog):
    """GAP 3. The verdict flipped; the tag did not. A store=None reader still sees it.

    The tag is a derived search index written LAST, so a crash between `report` and `tag` — or
    a verdict that flipped without the swap landing — leaves a correct verdict whose tag lags.
    That claim is missing from every tag-filtered search and findable by a dataset read. The
    mismatch is derivable from the artifact ALONE, which is the point: the second agent
    inheriting this from DataHub has no Attest store to consult.
    """
    claim = ownership_claim()
    urn = publish(catalog, claim, "Supported", at=NOW - timedelta(days=1))  # tag: Supported
    # A later audit reaches Contradicted; the tag swap never lands.
    catalog.report_assertion_result(
        urn,
        "FAILURE",
        int(NOW.timestamp() * 1000),
        {writeback.VERDICT_KEY: "Contradicted", writeback.AUDIT_RUN_KEY: "run-2"},
    )

    got = ClaimReader(catalog, store=None).get(urn)

    assert got.artifact.verdict == "Contradicted", "the latest run event is the authority"
    assert writeback.verdict_tag_urn("Supported") in got.artifact.tags, "tag is still the old one"
    assert got.stale_tag, "tag says Supported, verdict is Contradicted — that is stale"


def test_a_healthy_tag_is_not_stale_and_a_verdictless_claim_cannot_be(catalog):
    """The two directions gap 3's detection must get right, or it cries wolf.

    A claim whose tag matches its latest verdict is NOT stale. And a claim with no verdict
    cannot be stale — there is no verdict for a tag to lag — so pending-lag, incomplete and
    unknown never trip it. A false positive here would flag every fresh claim.
    """
    fresh = publish(catalog, ownership_claim(), "Supported")
    verdictless = upsert(catalog, freshness_claim())

    assert not ClaimReader(catalog, store=None).get(fresh).stale_tag, "a matching tag is not stale"
    got = ClaimReader(catalog, store=None).get(verdictless)
    assert got.artifact.verdict is None
    assert not got.stale_tag, "a claim with no verdict cannot have a stale tag"


def test_the_naive_detector_misses_a_stale_tag__the_gap_3_vacuity_check(catalog, monkeypatch):
    """THE VACUITY CHECK for gap 3: a detector that only asks 'is any tag present' misses it.

    The mismatch has to be against the LATEST verdict's expected tag, not merely whether the
    artifact carries some tag. Swap in the naive version and confirm the stale claim reads
    fresh — which is why the real detector compares the verdict to its tag.
    """
    claim = ownership_claim()
    urn = publish(catalog, claim, "Supported", at=NOW - timedelta(days=1))
    catalog.report_assertion_result(
        urn, "FAILURE", int(NOW.timestamp() * 1000), {writeback.VERDICT_KEY: "Contradicted"}
    )

    assert ClaimReader(catalog, store=None).get(urn).stale_tag, "the real detector catches it"

    # "It has some verdict and some tag, so the tag is fine." Blind to WHICH tag.
    naive = property(lambda self: self.artifact.verdict is not None and not self.artifact.tags)
    monkeypatch.setattr(retrieval.RetrievedClaim, "stale_tag", naive)

    assert not ClaimReader(catalog, store=None).get(urn).stale_tag, (
        "the naive detector called a stale tag fresh — which is why detection compares the "
        "latest verdict to its expected tag, not merely whether any tag exists"
    )
