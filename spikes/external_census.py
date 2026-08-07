"""THE CATALOG CENSUS: how many of the showcase pack's datasets can Attest read at all?

    just external-census --receipt docs/external-trial/<name>.json

A DIFFERENT EXPERIMENT FROM THE TRIAL, and deliberately a separate script. The trial asks
whether 15 hand-written claims get defensible verdicts; this asks a catalog-wide question
about the READ itself, over every dataset in the pack, with no model, no claims and no
writes. Keeping them apart keeps the trial runner as close to its original methodology as
it can be, and both run against the same loaded catalog state.

THE HYPOTHESIS, stated before the run:

    The 15 datasets the 2026-08-04 trial found unauditable were refused SOLELY because
    `client.DATASET_QUERY` had no `... on CorpGroup` union arm.

**It is a hypothesis, not an acceptance requirement.** A refusal that survives the fix is a
new finding to be classified, not a failed run.

**WHY BOTH ARMS RUN HERE, rather than the before-number being quoted.** The baseline
receipt's "52 readable / 15 refused" were HARDCODED LITERALS in `external_trial.build_receipt`
-- never measured by the run that reported them. Quoting them would carry an unmeasured
figure into a measurement. So this issues TWO queries per dataset against ONE catalog state:
the shipped `DATASET_QUERY`, and a LEGACY copy with the CorpGroup arm removed. The before
number is re-derived live, on the same data, in the same seconds.

**No product code is touched.** The legacy query is DERIVED from the shipped constant by
deleting one line, in this file, and the derivation asserts it actually changed something --
a rename or a reformat upstream must fail loudly rather than silently produce two identical
arms and a census reporting "no difference", which would be a green light wired to nothing.
`classify_dataset_payload` mirrors `client.fetch_dataset`'s exists-check and its NARROW
exception set exactly; if that ever drifts, this census stops describing what a checker sees.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import ValidationError  # noqa: E402

from attest.datahub.client import (  # noqa: E402
    CatalogUnavailable,
    DataHubClient,
    DataHubError,
)
from attest.datahub.snapshot import DatasetSnapshot  # noqa: E402
from evidence import ReceiptExists, provenance, write_receipt  # noqa: E402

RECEIPT = REPO / "docs" / "external-trial" / "census-after-corpgroup.json"
BASELINE = REPO / "docs" / "external-trial" / "results.json"
FIX_UNDER_TEST = "2d7eaf9 fix(datahub): read CorpGroup owners, not only CorpUser"

# The pack's URN prefix. Everything the census ranges over carries it; nothing in Attest's
# own seed does, so a seeded dataset can never be counted here by accident.
PREFIX = "b2fd91."

# The exact line the fix added. Deleted to rebuild the pre-fix query.
CORPGROUP_ARM = "... on CorpGroup { urn }"
CORPUSER_ARM = "... on CorpUser {"


class LegacyQueryUndeducible(RuntimeError):
    """The shipped query no longer has the shape this census strips an arm out of."""


def legacy_query(shipped: str) -> str:
    """The pre-fix DATASET_QUERY, derived from the shipped one by deleting the group arm.

    Derived rather than copied: a second copy of a 50-line query would drift from the real
    one, and a census whose "before" arm is a stale hand-copy measures a query nobody ever
    shipped.

    Every step is checked, because the failure mode is SILENT. If the arm cannot be found
    -- renamed, reformatted, moved -- a naive `.replace()` returns the string unchanged, both
    arms become identical, and the census reports a clean "no difference" that means nothing.
    """
    if CORPGROUP_ARM not in shipped:
        raise LegacyQueryUndeducible(
            f"{CORPGROUP_ARM!r} is not in DataHubClient.DATASET_QUERY. The query has been "
            f"reshaped since this census was written, so the legacy arm cannot be derived "
            f"and the comparison would be vacuous. Fix this derivation before trusting it."
        )
    out = "".join(
        line for line in shipped.splitlines(keepends=True) if CORPGROUP_ARM not in line
    )
    if out == shipped:
        raise LegacyQueryUndeducible("stripping the CorpGroup arm changed nothing")
    if CORPGROUP_ARM in out:
        raise LegacyQueryUndeducible("the CorpGroup arm survived the strip")
    if CORPUSER_ARM not in out:
        raise LegacyQueryUndeducible(
            "the CorpUser arm did not survive the strip; the legacy query would be wrong "
            "in a second way and the comparison would not isolate the group arm"
        )
    return out


def classify_dataset_payload(dataset: dict[str, Any] | None) -> tuple[str, str]:
    """What a checker would get for this response: `ok`, `not-found`, or the parse failure.

    A MIRROR of `client.get_dataset` + `client.fetch_dataset`, on purpose:

      * `exists` decides not-found, because DataHub fabricates a non-null dataset for ANY
        well-formed URN and only that field tells a typo from an empty entity;
      * the exception set is the same NARROW five the client catches, never bare Exception,
        so a real bug in the parse still crashes the census instead of being counted as a
        refusal.

    Pure, so the offline tier can exercise it with synthetic payloads and no network.
    """
    if not dataset or not dataset.get("exists"):
        return ("not-found", "")
    try:
        DatasetSnapshot.from_graphql(dataset)
    except (KeyError, AttributeError, TypeError, ValueError, ValidationError) as exc:
        return ("MalformedResponseError", f"{type(exc).__name__}: {exc}")
    return ("ok", "")


def read_one(client: DataHubClient, query: str, urn: str) -> tuple[str, str]:
    """Run one query for one dataset and classify the outcome.

    A transport failure is kept DISTINCT from a refusal about the entity (§20): "Attest
    never got an answer" is not "the answer was broken", and folding them together would
    put an outage into the census as a property of the catalog's metadata.
    """
    try:
        payload = client.execute(query, {"urn": urn}).get("dataset")
    except CatalogUnavailable as exc:
        return ("CatalogUnavailable", str(exc)[:300])
    except DataHubError as exc:
        return ("DataHubError", str(exc)[:300])
    return classify_dataset_payload(payload)


def dataset_urns_from_pack() -> list[str]:
    """Every dataset URN in the pack, off the pack's own checksum-pinned files.

    The source of the list is the ARTIFACT that was ingested, not a search index: an index
    read is eventually consistent and would silently undercount on a freshly bulk-loaded
    catalog -- which is exactly the condition this census runs in. Completeness here is
    definitional, the same argument `deploy/datahub/reset.ps1` makes for a full wipe.

    `external_ingest` is imported LAZILY: it injects truststore and imports httpx at module
    scope, and the offline tier must be able to import this module without either.
    """
    import tempfile

    import external_ingest

    cache = Path(tempfile.gettempdir()) / "attest-external-pack"
    cache.mkdir(parents=True, exist_ok=True)
    paths = external_ingest.fetch(cache)  # re-verifies the pinned sha256s

    urns: list[str] = []
    seen: set[str] = set()
    for path in paths.values():
        for mcp in json.loads(path.read_text(encoding="utf-8")):
            if mcp.get("entityType") != "dataset":
                continue
            urn = mcp.get("entityUrn") or ""
            if urn and urn not in seen:
                seen.add(urn)
                urns.append(urn)
    return urns


def census(client: DataHubClient, urns: list[str]) -> dict[str, Any]:
    shipped = DataHubClient.DATASET_QUERY
    legacy = legacy_query(shipped)

    rows: list[dict[str, str]] = []
    for i, urn in enumerate(urns, 1):
        with_outcome, with_detail = read_one(client, shipped, urn)
        without_outcome, without_detail = read_one(client, legacy, urn)
        rows.append(
            {
                "urn": urn,
                "with_corpgroup_arm": with_outcome,
                "with_corpgroup_detail": with_detail,
                "without_corpgroup_arm": without_outcome,
                "without_corpgroup_detail": without_detail,
            }
        )
        if i % 10 == 0 or i == len(urns):
            print(f"  {i}/{len(urns)} datasets")
    return summarize_rows(rows)


def summarize_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Totals and reason breakdowns for both arms. Pure, so the offline tier pins it."""

    def arm(key: str) -> dict[str, Any]:
        outcomes = [r[key] for r in rows]
        refused = [o for o in outcomes if o != "ok"]
        return {
            "readable": sum(1 for o in outcomes if o == "ok"),
            "refused": len(refused),
            "refusals_by_reason": dict(sorted(Counter(refused).items())),
        }

    with_arm = arm("with_corpgroup_arm")
    without_arm = arm("without_corpgroup_arm")
    fixed = [
        r["urn"]
        for r in rows
        if r["without_corpgroup_arm"] != "ok" and r["with_corpgroup_arm"] == "ok"
    ]
    still = [r["urn"] for r in rows if r["with_corpgroup_arm"] != "ok"]
    regressed = [
        r["urn"]
        for r in rows
        if r["without_corpgroup_arm"] == "ok" and r["with_corpgroup_arm"] != "ok"
    ]
    return {
        "datasets_enumerated": len(rows),
        "with_corpgroup_arm": with_arm,
        "without_corpgroup_arm": without_arm,
        # The three questions the hypothesis actually asks, computed rather than narrated.
        "refusals_the_corpgroup_arm_fixed": len(fixed),
        "refusals_the_corpgroup_arm_fixed_urns": fixed,
        "still_refused_with_the_arm": len(still),
        "still_refused_with_the_arm_urns": still,
        # Must be empty. A dataset readable BEFORE the fix and refused after would be a
        # regression the fix caused, and it is computed rather than assumed away.
        "regressed_by_the_arm": len(regressed),
        "regressed_by_the_arm_urns": regressed,
        "per_dataset": rows,
    }


def print_summary(result: dict[str, Any]) -> None:
    w, wo = result["with_corpgroup_arm"], result["without_corpgroup_arm"]
    n = result["datasets_enumerated"]
    print("\n" + "=" * 70)
    print(f"  datasets enumerated              {n}")
    print(f"  readable WITHOUT the group arm   {wo['readable']}/{n}   (the pre-fix read)")
    print(f"  readable WITH the group arm      {w['readable']}/{n}   (what ships today)")
    print(f"  refusals the arm fixed           {result['refusals_the_corpgroup_arm_fixed']}")
    print(f"  still refused with the arm       {result['still_refused_with_the_arm']}")
    print(f"  regressed by the arm             {result['regressed_by_the_arm']}")
    print(f"  refusal reasons, pre-fix         {wo['refusals_by_reason']}")
    print(f"  refusal reasons, today           {w['refusals_by_reason']}")
    print("=" * 70)
    print("  NOT A SCORE. A surviving refusal is a finding to classify, not a failure.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--receipt",
        type=Path,
        default=RECEIPT,
        help="where to write the census receipt. An EXISTING path is refused; no --force.",
    )
    ap.add_argument(
        "--limit", type=int, default=0, help="census only the first N datasets (debugging)"
    )
    args = ap.parse_args()

    path: Path = args.receipt if args.receipt.is_absolute() else REPO / args.receipt
    if path.exists():
        raise SystemExit(
            f"{path} already exists and this will not overwrite a receipt.\n"
            f"Pass --receipt with a NEW path. Nothing has been read."
        )

    client = DataHubClient()
    started = datetime.now(tz=UTC)
    print(f"\n=== EXTERNAL CATALOG CENSUS — {started.isoformat()}")
    print(f"    receipt: {path.relative_to(REPO)}")

    urns = dataset_urns_from_pack()
    if args.limit:
        urns = urns[: args.limit]
    print(f"    {len(urns)} dataset URNs in the pack (prefix {PREFIX!r})\n")

    # A sanity gate with the same job as the trial's preflight: refuse to describe a catalog
    # that is not loaded. Without it a census of an empty server reports 0 readable / N
    # not-found and reads like a catastrophic finding.
    first, _ = read_one(client, DataHubClient.DATASET_QUERY, urns[0])
    if first == "not-found":
        raise SystemExit(
            f"{urns[0]} is not in the catalog: the showcase pack is not loaded.\n"
            f"Run `just external-ingest` first. See docs/external-trial/ingest.md."
        )

    result = census(client, urns)
    finished = datetime.now(tz=UTC)

    receipt = {
        "what_this_is": (
            "A catalog-wide census of how many showcase-ecommerce datasets Attest's "
            "GraphQL read can resolve, measured WITH and WITHOUT the CorpGroup union arm "
            "against one loaded catalog state. NOT a benchmark and nothing here is scored. "
            "Companion to docs/external-trial.md; the 15-claim trial is a separate run."
        ),
        "reproduce": "just external-census --receipt <path>",
        "hypothesis": (
            "The previously observed 15 refusals were caused SOLELY by the missing "
            "CorpGroup union arm in client.DATASET_QUERY. Stated as a hypothesis and NOT "
            "as an acceptance requirement: a surviving refusal is a new finding to be "
            "classified, not a failed run."
        ),
        "baseline_note": (
            "The 2026-08-04 receipt's `datasets_readable_by_attest: 52` / "
            "`datasets_refused: 15` were HARDCODED LITERALS in external_trial.build_receipt "
            "and were never measured by that run. This census re-derives the pre-fix number "
            "live, on the same catalog state, rather than quoting them."
        ),
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "provenance": provenance(
            fix_under_test=FIX_UNDER_TEST,
            gms_url=client.gms_url,
            baseline_receipt=BASELINE,
            extra={
                "urn_source": "the checksum-pinned pack files, not a search index",
                "queries_compared": "DataHubClient.DATASET_QUERY, and the same with the "
                "`... on CorpGroup { urn }` line deleted",
                "product_code_modified": False,
            },
        ),
        "catalog": {
            "source": "DataHub showcase-ecommerce datapack",
            "ingest": "docs/external-trial/ingest.md",
            "urn_prefix": PREFIX,
        },
        "census": result,
    }
    try:
        write_receipt(path, receipt)
    except ReceiptExists as exc:
        print(f"\n!! {exc}\n\n{json.dumps(receipt, indent=2)}")
        return 1
    print_summary(result)
    print(f"\nreceipt -> {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
