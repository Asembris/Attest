"""FRESHNESS against the live catalog. All three verdicts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from attest import faithfulness, polarity
from attest.checkers import check_freshness
from attest.checkers.freshness import FUTURE_TOLERANCE_SECONDS
from attest.claims import FreshnessClaim, Verdict
from attest.datahub.snapshot import DatasetSnapshot
from attest.explain import template
from conftest import DOCUMENTED, NO_TIMESTAMP, STALE


def test_recent_dataset_supports_a_24h_claim(snapshot, now: datetime) -> None:
    claim = FreshnessClaim(
        target_urn=DOCUMENTED,
        max_age_hours=24,
        raw_text="The customer profile table is updated in the last 24 hours.",
    )
    r = check_freshness(claim, snapshot(DOCUMENTED), now=now)

    assert r.verdict is Verdict.SUPPORTED
    assert r.evidence[0].field == "properties.lastModified.time"
    assert r.evidence[0].value is not None


def test_stale_dataset_contradicts_a_daily_claim(snapshot, now: datetime) -> None:
    claim = FreshnessClaim(
        target_urn=STALE,
        max_age_hours=24,
        raw_text="revenue_daily is rebuilt nightly.",
    )
    r = check_freshness(claim, snapshot(STALE), now=now)

    assert r.verdict is Verdict.CONTRADICTED
    # The verdict must point at the timestamp that produced it, not just assert.
    assert r.evidence[0].value is not None
    assert "outside" in r.reason


def test_missing_timestamp_is_insufficient_not_contradicted(snapshot, now: datetime) -> None:
    """The distinction the whole project turns on.

    pipeline_scratch has no lastModified. It is not stale — the catalog simply does
    not know when it last ran. A checker that reports Contradicted here flags every
    pipeline whose ingestion never wrote a timestamp, which is a complaint about the
    catalog dressed up as a finding about the data.
    """
    claim = FreshnessClaim(
        target_urn=NO_TIMESTAMP,
        max_age_hours=24,
        raw_text="pipeline_scratch is refreshed daily.",
    )
    r = check_freshness(claim, snapshot(NO_TIMESTAMP), now=now)

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.verdict is not Verdict.CONTRADICTED
    # The evidence IS the absence: it must be reported, not merely implied.
    assert r.evidence[0].field == "properties.lastModified.time"
    assert r.evidence[0].value is None


def test_the_window_is_the_only_thing_that_moves_the_verdict(snapshot, now: datetime) -> None:
    """The same stale dataset flips to Supported under a window wide enough to cover it.

    Proves the verdict is arithmetic on the claim, not a property baked into the
    dataset — a checker that hardcoded "revenue_daily is the stale one" would pass
    the test above and fail this.
    """
    generous = FreshnessClaim(
        target_urn=STALE,
        max_age_hours=500 * 24,
        raw_text="revenue_daily has been updated in the last 500 days.",
    )
    assert check_freshness(generous, snapshot(STALE), now=now).verdict is Verdict.SUPPORTED

    # `now` is one hour past this dataset's timestamp, so a half-hour window excludes it.
    tight = FreshnessClaim(
        target_urn=DOCUMENTED,
        max_age_hours=0.5,
        raw_text="The customer profile was updated in the last 30 minutes.",
    )
    assert check_freshness(tight, snapshot(DOCUMENTED), now=now).verdict is Verdict.CONTRADICTED


def test_verdicts_do_not_depend_on_the_wall_clock(snapshot) -> None:
    """Reproducibility, asserted rather than hoped for.

    Every freshness verdict must be a function of (claim window, catalog timestamp,
    injected now) and nothing else. Someone cloning this repo in three weeks runs the
    same seed and gets the same verdicts, because `now` is derived from the catalog —
    not from the machine's clock, and not from a committed file that drifts out of sync
    with a freshly-seeded catalog.

    This test fails the moment check_freshness reaches for datetime.now() internally.
    """
    snap = snapshot(DOCUMENTED)
    assert snap.last_modified is not None
    claim = FreshnessClaim(target_urn=DOCUMENTED, max_age_hours=24, raw_text="Fresh daily.")

    # Shift the whole world forward a decade — data and clock together. The dataset is
    # still 1h old at that instant, so the verdict must not move. Only the INTERVAL is
    # allowed to matter; the absolute date must not enter into it.
    for offset in (timedelta(0), timedelta(days=3650)):
        shifted = snap.model_copy(update={"last_modified": snap.last_modified + offset})
        anchored = shifted.last_modified + timedelta(hours=1)
        assert check_freshness(claim, shifted, now=anchored).verdict is Verdict.SUPPORTED

    # And the same instant judged against a dataset a decade older flips it, proving the
    # arithmetic is live rather than the answer being pinned to the dataset.
    ancient = snap.model_copy(update={"last_modified": snap.last_modified - timedelta(days=3650)})
    assert check_freshness(claim, ancient, now=snap.last_modified).verdict is Verdict.CONTRADICTED


def test_boundary_is_inclusive(snapshot) -> None:
    """A dataset exactly at the age limit is within it. Off-by-one here is a wrong verdict."""
    snap = snapshot(DOCUMENTED)
    assert snap.last_modified is not None

    claim = FreshnessClaim(target_urn=DOCUMENTED, max_age_hours=24, raw_text="Updated daily.")
    exactly_24h_later = snap.last_modified + timedelta(hours=24)

    assert check_freshness(claim, snap, now=exactly_24h_later).verdict is Verdict.SUPPORTED
    assert (
        check_freshness(
            claim, snap, now=exactly_24h_later + timedelta(seconds=1)
        ).verdict
        is Verdict.CONTRADICTED
    )


# --- a future timestamp is a bad clock, not a fresh dataset (Session 23, Hole 1) ---

_FUT_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,x,PROD)"
_FUT_NOW = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _future_claim() -> FreshnessClaim:
    return FreshnessClaim(
        target_urn=_FUT_URN, max_age_hours=24, raw_text="updated in the last 24 hours"
    )


def test_a_future_timestamp_beyond_grace_is_insufficient_not_supported() -> None:
    """(now - last_modified) is negative, so the old code computed a negative age, found it
    <= any positive window, and returned a confident Supported ('modified -48.0h ago').

    A future timestamp is a malformed DATA point — a bad upstream clock — not a fresh
    dataset. It cannot establish recency, so it is Insufficient-Coverage, and the
    implausible value is SHOWN as evidence rather than computed into a verdict.
    """
    snap = DatasetSnapshot(urn=_FUT_URN, last_modified=_FUT_NOW + timedelta(days=2))
    r = check_freshness(_future_claim(), snap, now=_FUT_NOW)

    assert r.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert r.verdict is not Verdict.SUPPORTED
    # the implausible value is shown, not swallowed — and it is NOT None (that is absence)
    assert r.evidence[0].value is not None
    assert "future" in r.evidence[0].note.lower()


def test_the_clock_skew_grace_pins_the_constants_role() -> None:
    """BOTH sides of the boundary, so the constant's ROLE is pinned, not its number.

    Within the grace a future time is clock skew: the age clamps to 0 and just-modified
    data is Supported. Beyond it, it is implausible: Insufficient-Coverage. Change
    300 -> 30 and this still passes (the boundary holds either side); remove the clamp and
    the within-grace side goes red.
    """
    within = DatasetSnapshot(
        urn=_FUT_URN,
        last_modified=_FUT_NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS - 1),
    )
    beyond = DatasetSnapshot(
        urn=_FUT_URN,
        last_modified=_FUT_NOW + timedelta(seconds=FUTURE_TOLERANCE_SECONDS + 2),
    )

    assert check_freshness(_future_claim(), within, now=_FUT_NOW).verdict is Verdict.SUPPORTED
    assert (
        check_freshness(_future_claim(), beyond, now=_FUT_NOW).verdict
        is Verdict.INSUFFICIENT_COVERAGE
    )


def test_future_implausible_is_a_third_state_distinct_from_absent() -> None:
    """absent(value=None) != empty != implausible(value=future-ISO). Keep them distinct.

    Both an absent timestamp and a future one are Insufficient-Coverage, but the evidence
    must not collapse them: absence is None, an implausible time is the value itself.
    """
    future = DatasetSnapshot(urn=_FUT_URN, last_modified=_FUT_NOW + timedelta(days=2))
    absent = DatasetSnapshot(urn=_FUT_URN)  # last_modified is None

    fr = check_freshness(_future_claim(), future, now=_FUT_NOW)
    ar = check_freshness(_future_claim(), absent, now=_FUT_NOW)

    assert fr.verdict is ar.verdict is Verdict.INSUFFICIENT_COVERAGE
    assert ar.evidence[0].value is None  # the aspect is absent
    assert fr.evidence[0].value is not None  # the value is present but implausible


def test_the_future_rejection_ships_past_the_guards() -> None:
    """Hole 4 tie-off: the deterministic template for the future-IC verdict must pass
    faithfulness AND polarity, so display prose never falls back or drifts from it."""
    snap = DatasetSnapshot(urn=_FUT_URN, last_modified=_FUT_NOW + timedelta(days=2))
    r = check_freshness(_future_claim(), snap, now=_FUT_NOW)
    text = template(r)

    assert faithfulness.check(text, r).ok, faithfulness.check(text, r).summary
    assert polarity.check(text, r).ok, polarity.check(text, r).summary


def test_the_completeness_marker_does_not_license_freshness_closed_world() -> None:
    """`Verified` marks CLASSIFICATION complete, not freshness (Session 23, Hole 3, the
    correct-by-design half). A Verified table with no last-modified time is
    Insufficient-Coverage: a review of PII says nothing about when the data last changed.
    This pin goes red if the closed-world marker ever leaks into this checker."""
    snap = DatasetSnapshot(urn=_FUT_URN, tags=("urn:li:tag:Verified",))
    assert (
        check_freshness(_future_claim(), snap, now=_FUT_NOW).verdict
        is Verdict.INSUFFICIENT_COVERAGE
    )
