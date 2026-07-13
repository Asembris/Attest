"""FRESHNESS: is the data as recent as the claim says?

Pure date arithmetic. The only judgment call is the one this checker refuses to
make: a dataset with no lastModified is not stale. The catalog does not know when it
last changed, and "I don't know" is not "it's old". Reporting Contradicted there
would flag every pipeline whose ingestion never wrote a timestamp — which is a
statement about the catalog's completeness, not about the data's freshness.
"""

from __future__ import annotations

from datetime import UTC, datetime

from attest.checkers.base import result
from attest.claims import CheckResult, Evidence, FreshnessClaim, Verdict
from attest.datahub import DatasetSnapshot

FIELD = "properties.lastModified.time"


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

    age_hours = (now - snapshot.last_modified).total_seconds() / 3600
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
