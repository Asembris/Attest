"""Fixtures for Attest's tests.

These run against the LIVE seeded catalog, on purpose. The point of the
deterministic core is that it agrees with a real DataHub server, and a suite built
entirely on hand-written fixtures would pass happily while the client queried a
field that does not exist. The seed is reproducible (`just seed`), so "live" does
not mean "unrepeatable".

Snapshots are fetched once per session and reused: the checkers are pure, so nothing
is gained by re-querying, and the suite stays fast.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from attest.datahub import DataHubClient, DataHubError, DatasetSnapshot

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "seed" / "ground_truth.json"

# --- the seeded catalog, by role ---------------------------------------------
# Named for what each dataset PROVES, not for what it contains. A test that reads
# `UNREVIEWED` instead of a URN says why that dataset was chosen.


def _sf(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:snowflake,{name},PROD)"


def _pg(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,{name},PROD)"


# Fully documented: owner, PII tags, terms, fresh timestamp, Verified.
DOCUMENTED = _sf("analytics.customers.customer_profile")
# Reviewed (Verified) and found clean — no PII anywhere.
REVIEWED_CLEAN = _sf("analytics.orders.orders_fact")
# The trap: `recipient_email_hash` looks like PII by name, is tagged NonPII.
NONPII_TRAP = _sf("analytics.marketing.email_campaign_stats")
# Complete metadata, last modified 417 days ago.
STALE = _sf("analytics.finance.revenue_daily")
# Schema only. No owner, no tags, no terms. The catalog is silent.
UNREVIEWED = _sf("analytics.staging.raw_events")
# Schema + owner + tag, but NO lastModified. Isolates freshness-silence.
NO_TIMESTAMP = _sf("analytics.staging.pipeline_scratch")
# Owner + timestamp + tag + term, but NO schemaMetadata. Isolates schema-silence.
NO_SCHEMA = _pg("attest_db.public.external_report")
# Owned by carol.davis, so a claim naming anyone else is contradicted.
OWNED_BY_CAROL = _pg("attest_db.public.support_tickets")
# Tagged Deprecated but unowned; its `email` column is untagged (not NonPII).
DEPRECATED_UNOWNED = _pg("attest_db.public.legacy_accounts")

ALICE = "urn:li:corpuser:alice.chen"
DANA = "urn:li:corpuser:dana.wu"
CAROL = "urn:li:corpuser:carol.davis"

PII = "urn:li:tag:PII"
NON_PII = "urn:li:tag:NonPII"
TIER1 = "urn:li:tag:Tier1"
EMAIL_TERM = "urn:li:glossaryTerm:EmailAddress"


@pytest.fixture(scope="session")
def client() -> DataHubClient:
    with DataHubClient() as c:
        try:
            c.execute("query { appConfig { appVersion } }")
        except DataHubError as exc:
            pytest.skip(
                f"DataHub is not reachable at {c.gms_url} ({exc}). "
                "Start it and seed it: see docs/datahub-setup.md."
            )
        yield c


@pytest.fixture(scope="session")
def snapshot(client: DataHubClient):
    """Fetch a seeded dataset by URN, memoized for the session."""
    cache: dict[str, DatasetSnapshot] = {}

    def _get(urn: str) -> DatasetSnapshot:
        if urn not in cache:
            cache[urn] = client.fetch_dataset(urn)
        return cache[urn]

    return _get


@pytest.fixture(scope="session")
def ground_truth() -> dict[str, Any]:
    """What the seed asserts it wrote. The benchmark's reference, not the catalog's."""
    if not GROUND_TRUTH.exists():
        pytest.skip(f"{GROUND_TRUTH} is missing — run `just seed`.")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def now(ground_truth: dict[str, Any]) -> datetime:
    """The reference 'now' for freshness tests: the moment the seed was generated.

    NOT the wall clock. The seed writes its fresh datasets at (generation time - 6h),
    so under a real clock they age, and a suite that passes today fails next week
    against completely correct code. That failure would say "the checker is broken"
    when the truth is "the catalog is old" — the exact confusion between data state
    and code state that Attest exists to prevent, reproduced in its own test suite.

    Anchoring to `generated_at` makes every freshness assertion a statement about the
    checker's arithmetic, which is what is under test here. Freshness against the wall
    clock is a property of the seed, and test_coverage.py checks that separately.
    """
    return datetime.fromisoformat(ground_truth["generated_at"])
