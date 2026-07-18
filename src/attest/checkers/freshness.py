"""FRESHNESS: is the data as recent as the claim says?

Pure date arithmetic. Two judgment calls this checker refuses to make, and both come
back to the same rule: a bad clock is not a freshness signal.

  * A dataset with no lastModified is not stale. The catalog does not know when it last
    changed, and "I don't know" is not "it's old". Reporting Contradicted there would
    flag every pipeline whose ingestion never wrote a timestamp — a statement about the
    catalog's completeness, not about the data's freshness. -> Insufficient-Coverage.

  * A lastModified in the FUTURE is not a very fresh dataset. `now - last_modified` is
    negative, so a naive `age <= window` finds it trivially within ANY window and
    returns a confident Supported ("modified -48.0h ago") — a malformed DATA point
    (a bad upstream clock, a catalog time ahead of ours) laundered into an affirmation.
    That is the confident wrong answer this product exists to prevent. Beyond a small
    clock-skew grace it is Insufficient-Coverage, with the implausible value shown as
    evidence rather than computed into a verdict. -> Insufficient-Coverage.

The grace is what separates "future by a clock-skew second" from "future by days".
Well-synced hosts (NTP) stay sub-second; loosely-synced ones rarely exceed a few
minutes; a timestamp minutes-to-days ahead is a fault, not skew. Within the grace the
age clamps to 0 — just-modified data IS fresh — so skew never cries wolf; beyond it,
recency genuinely cannot be established. The threshold is one named constant, tunable
in one place, and the tests pin its ROLE (clamp-then-Supported vs reject) rather than
its exact value.
"""

from __future__ import annotations

from datetime import UTC, datetime

from attest.checkers.base import result
from attest.claims import CheckResult, Evidence, FreshnessClaim, Verdict
from attest.datahub import DatasetSnapshot

FIELD = "properties.lastModified.time"

# How far into the future a lastModified may sit before it is a bad clock rather than
# clock skew. 300s (5 min) absorbs a loosely-synced host without accepting a plainly
# implausible time; NTP-synced hosts never come near it. Tunable here and nowhere else.
FUTURE_TOLERANCE_SECONDS = 300


def check_freshness(
    claim: FreshnessClaim,
    snapshot: DatasetSnapshot,
    now: datetime | None = None,
) -> CheckResult:
    # `now` is injectable so a test asserting "this is 6 hours old" does not quietly
    # become a test of the system clock.
    now = now or datetime.now(UTC)

    if snapshot.last_modified is None:
        return result(
            claim,
            Verdict.INSUFFICIENT_COVERAGE,
            "The catalog holds no last-modified timestamp for this dataset, so its "
            "recency cannot be confirmed or denied.",
            Evidence(
                field=FIELD,
                value=None,
                note="Aspect absent — the catalog does not record when this last changed.",
            ),
        )

    delta_seconds = (now - snapshot.last_modified).total_seconds()

    # A timestamp beyond the grace into the future is malformed data, not a fresh
    # dataset. Reject it as Insufficient-Coverage — the value is present but implausible,
    # so its evidence is the value itself (never None: that would be absence, a different
    # thing). The reason states silence only, so the deterministic template stays
    # polarity-safe.
    if delta_seconds < -FUTURE_TOLERANCE_SECONDS:
        future_hours = -delta_seconds / 3600
        return result(
            claim,
            Verdict.INSUFFICIENT_COVERAGE,
            "The catalog's recorded last-modified time is in the future, so this "
            "dataset's recency cannot be confirmed or denied.",
            Evidence(
                field=FIELD,
                value=snapshot.last_modified.isoformat(),
                note=(
                    f"Recorded last-modified time is {future_hours:.1f}h in the future; a "
                    f"time ahead of now is a bad clock, not a freshness signal."
                ),
            ),
        )

    # Within the grace, a future time is clock skew: clamp the age to 0 so just-modified
    # data reads as fresh rather than negative-aged.
    age_hours = max(0.0, delta_seconds / 3600)
    observed = Evidence(
        field=FIELD,
        value=snapshot.last_modified.isoformat(),
        note=f"Last modified {age_hours:.1f}h ago; claim allows {claim.max_age_hours:.0f}h.",
    )

    if age_hours <= claim.max_age_hours:
        return result(
            claim,
            Verdict.SUPPORTED,
            f"Last modified {age_hours:.1f} hours ago, within the asserted "
            f"{claim.max_age_hours:.0f}-hour window.",
            observed,
        )

    return result(
        claim,
        Verdict.CONTRADICTED,
        f"Last modified {age_hours:.1f} hours ago, which is outside the asserted "
        f"{claim.max_age_hours:.0f}-hour window.",
        observed,
    )
