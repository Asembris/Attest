"""Fixtures for Attest's tests. Two tiers, and the split is the point (Session 8).

**The offline tier reads captured snapshots, never the network.** The `snapshot` fixture
loads serialized `DatasetSnapshot`s from `tests/fixtures/snapshots/` (see `_snapshots.py`).
So every checker, benchmark, coverage, explain, faithfulness, and pii-signal test runs
anywhere — no DataHub, no key, never a skip — and gates CI. This is the fix for the old
dishonesty: ~half the "offline" suite used to read the live catalog and SKIP when DataHub
was down, so the suite read GREEN with half of it never run. A green tick about a smaller
program, sitting in the test suite of the tool built to catch exactly that.

**The integration tier still needs the real server, and is marked so it can be told apart.**
`@pytest.mark.integration` (this file, on `test_client.py`) needs live GMS to exercise the
wire format and the structured-property fabrication landmine. `@pytest.mark.live`
(`test_live.py`) needs the real model, and `test_fixture_drift.py` needs live GMS to hold
the fixtures honest. These are ALLOWED to skip when the server is down — but the skip is
announced prominently (`pytest_terminal_summary` below), never buried in a count, because a
suspiciously fast run must say why.

**What the offline tier does NOT cover, stated so nobody assumes it does:** it does not
exercise `DatasetSnapshot.from_graphql`. Parsing the wire format is `test_client.py`'s job,
and it is live. The fixtures are `DatasetSnapshot`s already; loading one skips the parse.
And the fixtures are only as honest as `test_fixture_drift.py`, which re-fetches every
seeded URN and asserts equality — but only when the live tier is actually run. This tier
inherits the cadence rule; it does not escape it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from _snapshots import load_snapshot
from attest.datahub import DataHubClient, DataHubError, DatasetSnapshot

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "seed" / "ground_truth.json"

# The reason recorded when the `client` fixture skips an integration test. Matched in
# `pytest_terminal_summary` so the skip is announced, not buried. Kept as a constant so the
# emitter and the reporter cannot drift on the wording.
_DATAHUB_DOWN = "DataHub is not reachable"


def pytest_configure(config: pytest.Config) -> None:
    """Refuse to run the LIVE suite under parallel workers. Structural, not a convention.

    The offline suite parallelizes freely: every real write it makes is scoped to a
    per-test `tmp_path` (its store and its checkpoints), and everything it reads from the
    live catalog is idempotent, so N workers reading the seeded datasets at once is only
    more load, never a different answer.

    The LIVE suite is the exception, and it is a hard one. `test_live` approves a real audit
    and writes `attest.*` structured properties onto real seeded datasets
    (`support_tickets`, `customer_profile`). Those writes are last-write-wins on shared
    entities. Run two live workers at once — one approving while another audits the same
    dataset — and each reads a catalog the other is halfway through mutating: flaky
    cross-talk, and the flake would land on the one path that writes to someone's catalog.

    So this is refused rather than discouraged, and refused HERE — in `pytest_configure`, on
    the xdist controller, BEFORE any worker spawns — so no catalog write can happen before
    the refusal lands. (The obvious place, `pytest_collection_modifyitems`, runs on the
    workers, where `numprocesses` is already reset to 0 and a raise would let a sibling
    worker write first. Learned by watching it not fire.)

    The signal is the mark expression, because that is what actually selects the live tests.
    The default `addopts` is `-m 'not live'`, so `just check -n auto` carries `not live` and
    this never fires; `just live` carries `-m live` but no `-n`, so this never fires. The
    only way to reach the refusal is to ask for the live tests AND parallel workers at once
    — and the answer is no, by name, with what to do instead.
    """
    parallel = bool(getattr(config.option, "numprocesses", None))
    markexpr = getattr(config.option, "markexpr", "") or ""
    selects_live = "live" in markexpr and "not live" not in markexpr
    if parallel and selects_live:
        raise pytest.UsageError(
            "the live suite writes attest.* properties to shared seeded datasets and "
            "cannot run under parallel workers without cross-talk. Run it serially: "
            "`just live` (no -n). See tests/conftest.py::pytest_configure."
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Announce integration/live tests skipped because DataHub was down. LOUDLY.

    The offline tier never skips — it reads captured fixtures. So a skip here means an
    INTEGRATION test did not run, and that is exactly the kind of thing that makes a run
    read green while covering less than it looks like it covers. The cadence rule says a
    suspiciously fast run must say why; this is where it says it. Not a line buried in the
    skip count — a separator the operator cannot miss.
    """
    skipped = terminalreporter.stats.get("skipped", [])
    down = sum(1 for rep in skipped if _DATAHUB_DOWN in _skip_reason(rep))
    if down:
        terminalreporter.write_sep(
            "=",
            f"{down} integration tests skipped: DataHub not running "
            "(offline tier ran in full; wire-format + write-back coverage did NOT)",
            red=True,
            bold=True,
        )


def _skip_reason(report) -> str:
    """The human reason on a skip report, across pytest's representations of it."""
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple):  # (path, lineno, reason)
        return str(longrepr[2])
    return str(longrepr or "")


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
# The postgres mirror of DOCUMENTED: complete owner, tags, terms, and a FRESH timestamp,
# so a freshness claim with any generous window is Supported. The seed declares that
# ("ownership and freshness claims should both be Supported"), which is why a test that
# needs a Supported freshness verdict names this rather than picking a dataset that
# happens to be recent today.
RECENTLY_MODIFIED = _pg("attest_db.public.users")
# Tagged Deprecated but unowned; its `email` column is untagged (not NonPII).
DEPRECATED_UNOWNED = _pg("attest_db.public.legacy_accounts")
# PII marked by globalTag ONLY — zero glossary terms, at table or column grain.
# A checker that reads terms and forgets tags certifies this table as PII-free.
TAG_ONLY_PII = _pg("attest_db.public.hr_headcount")
# PII marked by glossary TERM only (under the PII node) — zero PII tags. The mirror
# image: a checker that reads tags and forgets terms certifies this one as PII-free.
TERM_ONLY_PII = _pg("attest_db.public.marketing_leads")
# PII marked by the hasPII custom PROPERTY only — no tag, no term. A scanner's finding
# that nobody has done the governance work behind.
PROPERTY_ONLY_PII = _sf("analytics.product.device_telemetry")

# Tagged PII at table level but NOT Verified. Isolates "does table-level PII leak down
# into an untagged column" from "does the completeness marker license a denial".
PII_TABLE_UNVERIFIED = _sf("analytics.customers.customer_contact")
# The inverse of NONPII_TRAP: the TABLE carries no PII signal at all, while its
# `actor_email` column is explicitly tagged PII. Proves precedence is about grain, not
# about NonPII, and that column signals propagate UP into a table-scoped claim.
COLUMN_ONLY_PII = _pg("attest_db.public.audit_log")

# Owned by a CorpGroup rather than a CorpUser — the ONLY seeded dataset that is. Every
# other one carries a corpuser owner, which is exactly why `DATASET_QUERY`'s missing
# `... on CorpGroup` arm stayed invisible here while making 15 of 67 datasets unauditable
# on a real external catalog (CLAUDE.md §23).
GROUP_OWNED = _sf("analytics.platform.ingest_metrics")

PII_NODE = "urn:li:glossaryNode:PII"

ALICE = "urn:li:corpuser:alice.chen"
DANA = "urn:li:corpuser:dana.wu"
CAROL = "urn:li:corpuser:carol.davis"
PLATFORM_GROUP = "urn:li:corpGroup:data-platform"

PII = "urn:li:tag:PII"
NON_PII = "urn:li:tag:NonPII"
TIER1 = "urn:li:tag:Tier1"
EMAIL_TERM = "urn:li:glossaryTerm:EmailAddress"


@pytest.fixture(scope="session")
def client() -> DataHubClient:
    """A live DataHub client. INTEGRATION-tier only — skips when the server is down.

    The offline tier does not touch this. Only the integration tier (`test_client.py`) and
    the anti-drift pin (`test_fixture_drift.py`) do, and both are allowed to skip — loudly,
    via `pytest_terminal_summary`. The skip reason MUST contain `_DATAHUB_DOWN` for that
    announcement to fire; the constant is shared so it cannot drift.
    """
    with DataHubClient() as c:
        try:
            c.execute("query { appConfig { appVersion } }")
        except DataHubError as exc:
            pytest.skip(
                f"{_DATAHUB_DOWN} at {c.gms_url} ({exc}). "
                "Start it and seed it: see docs/datahub-setup.md."
            )
        yield c


@pytest.fixture(scope="session")
def snapshot():
    """A seeded dataset by URN, loaded from a captured fixture. OFFLINE — no network.

    This is the boundary that makes the offline tier honest (Session 8). It used to fetch
    from the live catalog and skip when DataHub was down; now it reads a serialized
    `DatasetSnapshot` from `tests/fixtures/snapshots/`, so the checkers, benchmark, coverage,
    and semantic-guard tests run anywhere and never skip. A missing fixture is a hard error
    (run `just capture`), never a skip — the whole point is that absence of the catalog does
    not turn into a green tick. The fixtures are held equal to the live catalog by
    `test_fixture_drift.py` in the live tier.

    Memoized: the checkers are pure and the files do not change mid-session.
    """
    cache: dict[str, DatasetSnapshot] = {}

    def _get(urn: str) -> DatasetSnapshot:
        if urn not in cache:
            cache[urn] = load_snapshot(urn)
        return cache[urn]

    return _get


@pytest.fixture(scope="session")
def ground_truth() -> dict[str, Any]:
    """What the seed asserts it wrote. The benchmark's reference, not the catalog's."""
    if not GROUND_TRUTH.exists():
        pytest.skip(f"{GROUND_TRUTH} is missing — run `just seed`.")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def now(snapshot) -> datetime:
    """The reference 'now' for freshness tests, DERIVED FROM THE CATALOG ITSELF.

    Never the wall clock, and — deliberately — never a file either.

    The seed writes its fresh datasets at (seed time - 6h) and its stale one at
    (seed time - 417d), so the timestamps are RELATIVE to whenever someone last ran
    `just seed`. Under a real clock the fresh datasets therefore age, and a suite that
    is green today goes red in a fortnight against completely correct code — reporting
    "the checker is broken" when the truth is "the catalog is old". That is precisely
    the confusion between data state and code state that Attest exists to prevent, and
    it has no business appearing in Attest's own test suite.

    Anchoring to ground_truth.json's `generated_at` fixes the wall clock but not the
    real problem: that file is committed, so a fresh clone with a fresh `just seed` has
    a catalog from today and a `generated_at` from whenever it was committed. The two
    drift apart silently, and the tests start measuring the gap between them.

    So `now` is reconstructed from the reference dataset's own snapshot: one hour after
    the moment it says it was last modified. Since Session 8 that snapshot is a captured
    fixture rather than a live read, which only strengthens the property — the reference
    time and the datasets it is compared against are frozen together at capture, so a judge
    cloning this repo in three weeks gets a green suite with no server and no clock in it.
    The fixtures are held equal to the live catalog by `test_fixture_drift.py`.
    """
    reference = snapshot(DOCUMENTED).last_modified
    assert reference is not None, "the reference dataset must carry a timestamp"
    return reference + timedelta(hours=1)
