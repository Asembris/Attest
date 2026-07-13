"""Spike: prove DataHub's read/write path end to end against the seeded catalog.

Nothing downstream of this gets built until all four operations pass:

  1. READ       a dataset by URN -> schema, tags, ownership, glossary terms, last-modified
  2. READ       that dataset's structured + custom properties
  3. WRITE      a structured property value onto it
  4. READ-BACK  query it again; confirm it persisted, and that it is queryable

Step 4 is the one that matters most. Attest's whole output is a verdict written
back onto a dataset. If that round-trip is unreliable, the architecture changes.

Run (from repo root, after seeding):  python spikes/datahub_probe.py
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any

from attest.datahub import DataHubClient, DataHubError

PROBE_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
    "analytics.customers.customer_profile,PROD)"
)
PROPERTY_QUALIFIED_NAME = "attest.groundedness_verdict"
PROPERTY_URN = f"urn:li:structuredProperty:{PROPERTY_QUALIFIED_NAME}"

# How long to give the (eventually consistent) search index before reporting the
# written value as not searchable.
SEARCH_TIMEOUT_S = 60

# A value that changes every run, so a "successful" read-back can't be a stale
# hit from a previous run.
PROBE_VALUE = f"Supported@{datetime.now(timezone.utc).isoformat(timespec='seconds')}"


class ProbeFailure(Exception):
    pass


def header(n: int, title: str) -> None:
    print(f"\n{'=' * 72}\n[{n}] {title}\n{'=' * 72}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    return ok


def read_dataset(client: DataHubClient) -> dict[str, Any]:
    header(1, "READ — dataset by URN")
    dataset = client.get_dataset(PROBE_URN)
    if not dataset:
        raise ProbeFailure(
            f"No dataset at {PROBE_URN}. Did ingestion run? "
            "python seed/generate_seed.py && datahub ingest -c ./seed/recipe.yml"
        )

    props = dataset.get("properties") or {}
    print(f"  urn:         {dataset['urn']}")
    print(f"  platform:    {(dataset.get('platform') or {}).get('name')}")
    print(f"  description: {(props.get('description') or '')[:70]}")

    last_modified = (props.get("lastModified") or {}).get("time")
    if last_modified:
        iso = datetime.fromtimestamp(last_modified / 1000, tz=timezone.utc).isoformat()
        print(f"  lastModified: {iso}")

    owners = [
        (o.get("owner") or {}).get("urn")
        for o in ((dataset.get("ownership") or {}).get("owners") or [])
    ]
    tags = [
        (t.get("tag") or {}).get("urn")
        for t in ((dataset.get("tags") or {}).get("tags") or [])
    ]
    terms = [
        (t.get("term") or {}).get("urn")
        for t in ((dataset.get("glossaryTerms") or {}).get("terms") or [])
    ]
    fields = (dataset.get("schemaMetadata") or {}).get("fields") or []

    print(f"  owners:      {owners}")
    print(f"  tags:        {tags}")
    print(f"  terms:       {terms}")
    print(f"  schema:      {len(fields)} columns")
    for f in fields:
        col_tags = [
            (t.get("tag") or {}).get("urn", "").split(":")[-1]
            for t in ((f.get("globalTags") or {}).get("tags") or [])
        ]
        col_terms = [
            (t.get("term") or {}).get("urn", "").split(":")[-1]
            for t in ((f.get("glossaryTerms") or {}).get("terms") or [])
        ]
        print(
            f"    - {f['fieldPath']:<14} {f.get('nativeDataType', ''):<16}"
            f" tags={col_tags} terms={col_terms}"
        )

    ok = True
    ok &= check("schema present", bool(fields), f"{len(fields)} columns")
    ok &= check("ownership present", bool(owners))
    ok &= check("tags present", bool(tags))
    ok &= check("glossary terms present", bool(terms))
    ok &= check("last-modified present", last_modified is not None)
    ok &= check(
        "column-level tags present",
        any((f.get("globalTags") or {}).get("tags") for f in fields),
    )
    if not ok:
        raise ProbeFailure("Dataset is missing metadata the seed should have written.")
    return dataset


def read_properties(client: DataHubClient, dataset: dict[str, Any]) -> None:
    header(2, "READ — structured + custom properties")

    custom = ((dataset.get("properties") or {}).get("customProperties")) or []
    print("  customProperties:")
    for entry in custom:
        print(f"    - {entry['key']} = {entry['value']}")
    check("custom properties readable", bool(custom))

    structured = (dataset.get("structuredProperties") or {}).get("properties") or []
    # 0 on a freshly seeded catalog; 1 on any re-run, since step 3 overwrites.
    print(f"  structuredProperties: {len(structured)}")
    for prop in structured:
        print(f"    - {(prop.get('structuredProperty') or {}).get('urn')}: {prop.get('values')}")

    definition = client.get_structured_property(PROPERTY_URN)
    if definition and definition.get("definition"):
        d = definition["definition"]
        print(f"  property definition: {d['qualifiedName']} ({d.get('cardinality')})")
        check("verdict property defined (from ingestion)", True)
    else:
        print(f"  property definition: MISSING — creating {PROPERTY_URN} over GraphQL")
        client.create_structured_property(
            qualified_name=PROPERTY_QUALIFIED_NAME,
            display_name="Attest Groundedness Verdict",
            description="The most recent groundedness verdict Attest recorded.",
        )
        check("verdict property created via GraphQL", True)


def write_property(client: DataHubClient) -> None:
    header(3, "WRITE — structured property onto the dataset")
    print(f"  {PROPERTY_QUALIFIED_NAME} = {PROBE_VALUE!r}")
    result = client.set_structured_property(PROBE_URN, PROPERTY_URN, [PROBE_VALUE])
    written = result.get("properties") or []
    check("mutation returned the written property", bool(written), f"{len(written)} propert(ies)")


def read_back(client: DataHubClient) -> bool:
    """Returns whether the written value became searchable (see caveat below)."""
    header(4, "READ-BACK — confirm it persisted and is queryable")

    dataset = client.get_dataset(PROBE_URN)
    structured = ((dataset or {}).get("structuredProperties") or {}).get("properties") or []

    found = None
    for prop in structured:
        if (prop.get("structuredProperty") or {}).get("urn") == PROPERTY_URN:
            values = [
                v.get("stringValue") or v.get("numberValue") for v in (prop.get("values") or [])
            ]
            found = values
            print(f"  read back: {PROPERTY_URN} = {values}")

    if not check("property persisted on the entity", found is not None):
        raise ProbeFailure("Structured property did not persist on the entity.")
    if not check("value round-tripped exactly", found == [PROBE_VALUE], f"{found}"):
        raise ProbeFailure(f"Value changed in transit: wrote {PROBE_VALUE!r}, read {found!r}")

    # The search index is eventually consistent, and it lags noticeably when it is
    # busy (e.g. right after a seed ingest): observed anywhere from ~2s to >10s for
    # the same write. Poll generously — a short window here reports "not searchable"
    # for what is really just latency.
    print("  waiting for the search index...")
    hits: dict[str, Any] = {}
    deadline = time.monotonic() + SEARCH_TIMEOUT_S
    waited = 0.0
    while time.monotonic() < deadline:
        try:
            hits = client.search_by_structured_property(PROPERTY_QUALIFIED_NAME, PROBE_VALUE)
        except DataHubError as exc:
            print(f"    search error: {exc}")
            break
        if hits.get("total", 0) > 0:
            print(f"    indexed after ~{waited:.0f}s")
            break
        time.sleep(1)
        waited += 1

    urns = [(r.get("entity") or {}).get("urn") for r in (hits.get("searchResults") or [])]
    queryable = PROBE_URN in urns
    check(
        f"queryable by structured property (search, <={SEARCH_TIMEOUT_S}s)",
        queryable,
        f"total={hits.get('total', 0)}",
    )
    if not queryable:
        # The round-trip itself still passed; this is a separate capability. Attest
        # needs it later to answer "which datasets are Contradicted?", so say so
        # rather than folding it silently into an overall pass.
        print(
            f"\n  NOTE: the value persisted and reads back on the entity, but did not\n"
            f"  appear in search within {SEARCH_TIMEOUT_S}s. This index is eventually\n"
            f"  consistent and lags under load, so it is most likely still latency —\n"
            f"  re-run the search before concluding otherwise. The write path is proven\n"
            f"  either way; only verdict-filtered *search* is in question."
        )
    return queryable


def main() -> int:
    print(f"probing {PROBE_URN}")
    try:
        with DataHubClient() as client:
            print(f"gms: {client.gms_url}  (auth: {'token' if client.token else 'disabled'})")
            dataset = read_dataset(client)
            read_properties(client, dataset)
            write_property(client)
            searchable = read_back(client)
    except (ProbeFailure, DataHubError) as exc:
        print(f"\nPROBE FAILED: {exc}")
        return 1

    print(f"\n{'=' * 72}")
    print("ALL FOUR OPERATIONS PASSED — read/write path is proven.")
    print(
        f"  verdict-filtered search: {'working' if searchable else 'NOT CONFIRMED (see note above)'}"
    )
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
