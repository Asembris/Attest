"""`frontend/src/data/catalog.ts` is HAND-MAINTAINED, and nothing tested it until now.

The file is a static list of the seeded datasets, compiled into the shipped bundle and read
by exactly one component -- `ClaimsExplorer`'s dataset filter. It carries a header comment
saying "regenerate when the seed changes", but there is no generator: no script and no
justfile recipe writes it. Keeping it in step with the seed was a standing obligation on
whoever changed the seed, enforced by nothing.

It drifted, exactly as an unenforced obligation does. `2d7eaf9` added a 17th seeded dataset
(`analytics.platform.ingest_metrics`, the CorpGroup-owned one that exists so the seed can
exercise group ownership at all) and this list stayed at 16. Every tier was green: no Python
test reads it, and the frontend has no test runner -- `package.json` has `typecheck`, `lint`
and `build`, and nothing that executes a component. §14's rule, one more time: **a path no
test can execute is a path nobody has checked.**

**The authoritative source is `tests/fixtures/snapshots/`, NEVER `seed/ground_truth.json`.**
The manifest is gitignored (`.gitignore`: `seed/*.json`), so a bare CI runner does not have
it and a pin that read it would skip -- in the tier whose entire claim is that it never
skips. The captured snapshots are committed, are one-per-seeded-dataset, and are held equal
to live GMS by `test_fixture_drift.py`. They are what the rest of the offline tier already
trusts, so this pin adds no new source of truth.

Offline tier: committed files only. No DataHub, no key, no network, no build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from _snapshots import FIXTURE_DIR

REPO = Path(__file__).resolve().parents[1]
CATALOG_TS = REPO / "frontend" / "src" / "data" / "catalog.ts"

# urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<env>)
_DATASET_URN = re.compile(
    r"urn:li:dataset:\(urn:li:dataPlatform:(?P<platform>[^,]+),(?P<name>.+),(?P<env>[^,)]+)\)"
)

# The 16 entries as they stood before the 17th was appended. Pinned as literal tuples rather
# than re-derived from the fixtures: this is the "nothing else moved" half of the sync, and a
# check that recomputed them from the same source the new entry comes from could not tell a
# corrected entry from a silently rewritten one.
ENTRIES_BEFORE_THE_SYNC = (
    ("snowflake", "analytics.customers.customer_profile", "alice.chen"),
    ("snowflake", "analytics.customers.customer_contact", "alice.chen"),
    ("snowflake", "analytics.orders.orders_fact", "bob.martinez"),
    ("snowflake", "analytics.marketing.email_campaign_stats", "carol.davis"),
    ("snowflake", "analytics.finance.revenue_daily", "bob.martinez"),
    ("snowflake", "analytics.staging.raw_events", None),
    ("postgres", "attest_db.public.users", "dana.wu"),
    ("postgres", "attest_db.public.payment_methods", "dana.wu"),
    ("postgres", "attest_db.public.support_tickets", "carol.davis"),
    ("postgres", "attest_db.public.legacy_accounts", None),
    ("postgres", "attest_db.public.hr_headcount", "dana.wu"),
    ("postgres", "attest_db.public.marketing_leads", "carol.davis"),
    ("snowflake", "analytics.product.device_telemetry", "dana.wu"),
    ("postgres", "attest_db.public.audit_log", "dana.wu"),
    ("snowflake", "analytics.staging.pipeline_scratch", "dana.wu"),
    ("postgres", "attest_db.public.external_report", "bob.martinez"),
)

NEW_ENTRY_NAME = "analytics.platform.ingest_metrics"


def _extract(source: str) -> list[dict[str, Any]]:
    """Slice the `seededDatasets` array out of the TypeScript and parse it as JSON.

    The array is strict JSON today (double-quoted keys, no trailing comma, no expressions),
    which is what makes a Python pin possible without a JS runtime. That is a property of the
    file, not a guarantee about TypeScript -- so if it stops holding, this **raises with the
    fix attached** rather than falling back to a lenient parse. A regex that quietly matched
    less would turn a broken catalog into a passing test, which is the failure this module
    exists to end.
    """
    marker = "export const seededDatasets"
    start = source.find(marker)
    if start < 0:
        raise AssertionError(
            f"{CATALOG_TS} no longer declares `{marker}`. This pin slices that array out of "
            "the TypeScript and parses it as JSON; if the declaration was renamed or the "
            "shape changed, update the extractor deliberately -- do not delete the pin."
        )
    open_bracket = source.find("= [", start)
    if open_bracket < 0:
        raise AssertionError(
            f"{CATALOG_TS} declares `{marker}` but not as `= [ ... ]`. See above."
        )
    open_bracket += len("= ")
    close_bracket = source.rfind("]")
    try:
        parsed = json.loads(source[open_bracket : close_bracket + 1])
    except json.JSONDecodeError as exc:  # pragma: no cover - the loud path
        raise AssertionError(
            f"the `seededDatasets` array in {CATALOG_TS} is no longer strict JSON ({exc}). "
            "It must stay hand-writable as plain data so this pin can read it without a JS "
            "runtime: double-quoted keys, no trailing comma, no expressions."
        ) from exc
    if not isinstance(parsed, list):
        raise AssertionError(f"`seededDatasets` in {CATALOG_TS} is not an array")
    return parsed


def _owner_for(snapshot: dict[str, Any]) -> str | None:
    """The frontend's `owner` convention, applied to a captured snapshot's owners.

    A CorpUser owner is carried as a BARE id (`alice.chen`) -- the 16 original entries all do,
    and rewriting them would be churn in a field nothing reads. A CorpGroup owner is carried
    as the CANONICAL URN (`urn:li:corpGroup:data-platform`), because the alternatives are both
    worse: a bare `data-platform` renders a team indistinguishably from a person, which is the
    exact conflation the CorpGroup work exists to end, and `null` would fabricate an absence
    on a dataset that IS owned. The field is inert UI metadata -- `ClaimsExplorer` reads only
    `urn` and `name` -- so the heterogeneity costs nothing and says something true.
    """
    owners = snapshot.get("owners") or []
    users = [o for o in owners if o.startswith("urn:li:corpuser:")]
    if users:
        return users[0].split(":")[-1]
    groups = [o for o in owners if o.startswith("urn:li:corpGroup:")]
    if groups:
        return groups[0]
    return None


@pytest.fixture(scope="module")
def entries() -> list[dict[str, Any]]:
    return _extract(CATALOG_TS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def snapshots() -> dict[str, dict[str, Any]]:
    """Every committed snapshot, keyed by URN. This is the seed, as CI can see it."""
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(FIXTURE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        out[data["urn"]] = data
    assert out, f"no captured snapshots in {FIXTURE_DIR}"
    return out


def test_the_catalog_offers_every_seeded_dataset_and_nothing_else(
    entries: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> None:
    """Set equality, asserted in BOTH directions, because the two gaps are different bugs.

    A MISSING entry hides a real dataset from the filter -- the drift this pin was written
    for. An EXTRA entry offers a judge a dataset the backend cannot resolve, and the listing
    that comes back is honestly empty, which reads like a bug in the product rather than a
    stale file in the bundle.
    """
    in_catalog = {e["urn"] for e in entries}
    seeded = set(snapshots)
    assert in_catalog == seeded, (
        f"frontend/src/data/catalog.ts is out of step with the seed. "
        f"missing: {sorted(seeded - in_catalog)}; extra: {sorted(in_catalog - seeded)}. "
        "The list is hand-maintained against tests/fixtures/snapshots/ -- update it when the "
        "seed changes."
    )


def test_no_urn_appears_twice(entries: list[dict[str, Any]]) -> None:
    """`ClaimsExplorer` keys its <option> elements on the URN, so a duplicate is a React key
    collision as well as a doubled row in the dropdown."""
    urns = [e["urn"] for e in entries]
    dupes = sorted({u for u in urns if urns.count(u) > 1})
    assert not dupes, f"duplicate URNs in catalog.ts: {dupes}"
    assert len(urns) == len(entries)


def test_the_corpgroup_dataset_is_present_exactly_once(
    entries: list[dict[str, Any]],
) -> None:
    """The 17th dataset by name, independently of the set comparison above.

    It is called out on its own because it is the one the seed added last and the one this
    file was missing; a set-equality failure names it among others, and this names it alone.
    """
    matches = [e for e in entries if e["name"] == NEW_ENTRY_NAME]
    assert len(matches) == 1, (
        f"expected exactly one {NEW_ENTRY_NAME} entry in catalog.ts, found {len(matches)}"
    )


def test_every_entry_matches_the_committed_snapshot_it_names(
    entries: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> None:
    """Field-level provenance for all 17, including the owner convention.

    Note `name` is the DOTTED PATH out of the URN, not the snapshot's own `name` field --
    that one holds the short leaf (`ingest_metrics`, `customer_profile`). Reading the wrong
    one is the likeliest way to hand-author a wrong entry, so it is pinned rather than
    trusted.
    """
    for entry in entries:
        snap = snapshots[entry["urn"]]
        m = _DATASET_URN.fullmatch(entry["urn"])
        assert m is not None, f"not a dataset URN: {entry['urn']!r}"
        assert entry["platform"] == m["platform"] == snap["platform"], (
            f"{entry['urn']}: platform {entry['platform']!r} does not match the snapshot"
        )
        assert entry["name"] == m["name"], (
            f"{entry['urn']}: name {entry['name']!r} is not the URN's dotted dataset name "
            f"({m['name']!r}). The snapshot's own `name` field is the short leaf and is NOT "
            "what this list carries."
        )
        assert entry["owner"] == _owner_for(snap), (
            f"{entry['urn']}: owner {entry['owner']!r} does not match the snapshot's owners "
            f"{snap.get('owners')!r} under the frontend convention"
        )


def test_the_new_entry_carries_the_canonical_corpgroup_urn(
    entries: list[dict[str, Any]], snapshots: dict[str, dict[str, Any]]
) -> None:
    """The owner representation, pinned as a literal rather than only via the mapping rule.

    The rule above would keep passing if someone changed both the rule and the data. This is
    the decision itself: the group travels as its canonical URN, never as a bare id that
    would read as a username.
    """
    entry = next(e for e in entries if e["name"] == NEW_ENTRY_NAME)
    assert entry["owner"] == "urn:li:corpGroup:data-platform"
    assert entry["urn"] == (
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
        "analytics.platform.ingest_metrics,PROD)"
    )
    assert entry["platform"] == "snowflake"
    assert snapshots[entry["urn"]]["owners"] == ["urn:li:corpGroup:data-platform"]


def test_the_sixteen_entries_that_predate_the_sync_are_unchanged(
    entries: list[dict[str, Any]],
) -> None:
    """The preservation half: the sync ADDS, it does not rewrite.

    In particular the CorpUser owners stay bare ids. Normalizing them to full URNs for
    consistency with the group entry would be a 16-line diff in a field no component reads,
    inside a change whose whole claim is a one-entry blast radius.
    """
    prefix = [(e["platform"], e["name"], e["owner"]) for e in entries[:16]]
    assert prefix == list(ENTRIES_BEFORE_THE_SYNC), (
        "an entry that predates the 17-dataset sync changed. This list is append-only in "
        "practice: the sync adds the CorpGroup dataset and touches nothing else."
    )


def test_the_order_follows_the_seed_declaration(
    entries: list[dict[str, Any]],
) -> None:
    """Ordering is the seed's declaration order -- NOT sorted, and the difference matters.

    `generate_seed.py` declares the datasets in a deliberate sequence (the PII-signal
    witnesses, then the silent ones, then the CorpGroup dataset last), and this file has
    always mirrored it, so the dropdown reads in the same order as the seed a reviewer is
    looking at. Sorting it would be a 16-row diff for no gain; asserting it is sorted would
    LOCK IN that churn. So what is pinned is determinism plus the one rule the sync relies
    on: the newest dataset goes at the END.
    """
    urns = [e["urn"] for e in entries]
    assert urns != sorted(urns), (
        "catalog.ts is now URN-sorted. It has always followed seed-declaration order; if "
        "that changed deliberately, update this pin and say so."
    )
    assert entries[-1]["name"] == NEW_ENTRY_NAME, (
        "the CorpGroup dataset is declared last in seed/generate_seed.py and must be last "
        "here; appending is what keeps the previous 16 byte-for-byte."
    )


def test_the_extractor_fails_loudly_rather_than_reading_less(tmp_path: Path) -> None:
    """THE VACUITY CHECK. A parser that silently matches nothing is a green light wired to
    nothing -- and this pin is a parser sitting between two files it cannot type-check.

    Three ways the file could stop being readable, each of which must raise rather than
    return an empty list that every assertion above would then pass over. It also proves the
    extractor finds the REAL entries, so the checks are not all running on `[]`.
    """
    real = _extract(CATALOG_TS.read_text(encoding="utf-8"))
    assert len(real) == 17, f"expected 17 entries, extracted {len(real)}"

    with pytest.raises(AssertionError, match="no longer declares"):
        _extract("export const somethingElse = [];\n")

    with pytest.raises(AssertionError, match="not as"):
        _extract("export const seededDatasets = buildFromManifest();\n")

    with pytest.raises(AssertionError, match="strict JSON"):
        _extract("export const seededDatasets = [{ urn: 'unquoted-key' }];\n")
