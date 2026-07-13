"""FRESHNESS against the live catalog. All three verdicts."""

from __future__ import annotations

from datetime import datetime, timedelta

from attest.checkers import check_freshness
from attest.claims import FreshnessClaim, Verdict
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

    tight = FreshnessClaim(
        target_urn=DOCUMENTED,
        max_age_hours=1,
        raw_text="The customer profile was updated in the last hour.",
    )
    assert check_freshness(tight, snapshot(DOCUMENTED), now=now).verdict is Verdict.CONTRADICTED


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
