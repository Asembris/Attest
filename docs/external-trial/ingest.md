# The external trial catalog: what it is and how to rebuild it

The catalog behind [`../external-trial.md`](../external-trial.md) and
[`results.json`](results.json). One command:

```bash
just external-ingest          # download (checksum-pinned), filter for Core, ingest
just external-ingest --plan   # report what Core would refuse; ingest nothing. Free.
```

## What it is

**DataHub's own `showcase-ecommerce` datapack** — the pack
`datahub docker ingest-sample-data --pack showcase-ecommerce` loads. 67 datasets across
snowflake, dbt, postgres, s3, tableau, powerbi and looker, with schemas, glossary, tags,
ownership, domains, data products and lineage. **No metadata value in it was written here.**

Pinned by **content**, not by trust in a URL — `main` moves, and a checksum mismatch means the
catalog is not the one the receipt was measured against, so
[`spikes/external_ingest.py`](../../spikes/external_ingest.py) fails loudly rather than
running a silently different trial.

Registry: `https://raw.githubusercontent.com/datahub-project/datahub/master/metadata-ingestion/src/datahub/cli/datapack/resources/registry.json`
Base: `https://raw.githubusercontent.com/datahub-project/static-assets/main/datapacks/showcase-ecommerce/`

| File | Bytes | sha256 | Loaded |
| --- | --- | --- | --- |
| `01-definitions.json` | 6,001 | `3ecaf19324736331d6edb80c4fd7363d5b15fd41e2f9daf3ac68506aedf86608` | yes — 10 structured-property definitions |
| `02-data.json` | 3,183,160 | `0b3aeaa8941acc79f7d08db370ec092912ca77c15c2c2cdba2bf243225630c90` | yes — 3809 MCPs |
| `03-context.json` | 68,944 | *not loaded* | **no** — 54 `document` entities |

`03-context.json` is deliberately skipped: Attest reads no `document`, and the compatibility
filter passes unknown entity types straight through to the server untested. Out of scope,
said out loud rather than silently omitted.

## Three deliberate departures from `datahub docker ingest-sample-data`

**1. The system trust store is injected.** The CLI resolves the pack registry over `requests`,
which trusts certifi and nothing else, so on a TLS-inspecting network it dies with
`CERTIFICATE_VERIFY_FAILED` — and the bundled fallback `registry.json` **is not shipped inside
the wheel** (`FileNotFoundError`), so there is no offline path either. This is the **fourth**
runtime in this repo to need the OS trust store, after Python's OpenAI/httpx client, Node, and
uvx. CLAUDE.md's rule is to reach for the system-CA opt-in *before* debugging an apparent
outage; that is what `truststore.inject_into_ssl()` is doing at the top of the ingest spike.

**2. The pack's original timestamps are preserved.** The CLI's `--pack` path sets
`no_time_shift: false`, which rewrites every timestamp to the moment of ingest. Every
freshness verdict in the trial would then be an artifact of the ingest clock — a table stale
by a year reading fresh because it was loaded today — and the receipt would be unreproducible
by construction. So the pack goes in through the ordinary `file` → `datahub-rest` recipe with
its recorded times intact.

**3. Cloud-only aspects are filtered using the SERVER's own registry.** The pack carries
DataHub Cloud aspects that Core v1.5.0.6 has no place for. The filter is
`DataHubGraph.get_entity_aspect_specs()` — the same call `datahub datapack load` makes — so
this is a mechanical compatibility pass driven by the live server, not a hand-picked subset.

**Measured: 3571 of 3819 MCPs accepted, 248 refused.**

| Refused | Count |
| --- | --- |
| `dataset/entityInferenceMetadata` | 67 |
| `dataset/lineageFeatures` | 67 |
| `dataset/usageFeatures` | 27 |
| `dataset/storageFeatures` | 25 |
| `dataJob/lineageFeatures` | 23 |
| `chart/lineageFeatures` | 12 |
| `dataset/assertionsSummary` | 9 |
| `domain/status` | 6 |
| `corpuser/corpUserUsageFeatures` | 4 |
| `dashboard/lineageFeatures` | 3 |
| `dataset/documentation`, `dataset/schemaProposals`, `dataset/proposals`, `dashboard/usageFeatures`, `dashboard/proposals` | 1 each |

**None of them is an aspect Attest reads.** `datasetProperties` (with `lastModified`),
`ownership`, `globalTags`, `glossaryTerms` and `schemaMetadata` all load intact.

## The landmine that did not fire

CLAUDE.md warns that DataHub's showcase datapacks reference `dataQualityCheck`, a
Cloud-only entity type absent from Core's EntityRegistry, and that emitting one **crashes the
emitter mid-file and silently drops everything after it** — which is why
`seed/generate_seed.py` is hand-rolled in the first place.

Checked before ingesting, in the pack's raw bytes: `dataQualityCheck` **0**, `anomalies`
**0**, `dataContractProperties` **0**. This pack is clean; the warning is about older
showcase datapacks. Ingest result: **3563 events, 0 failures.**

## What it costs, and how to undo it

The showcase datasets land at their own URNs (`b2fd91.…` under snowflake/dbt/s3/…), so they
**coexist** with Attest's seeded catalog rather than colliding with it. What they do change is
catalog-**wide** reads: search totals, and therefore anything that ranks results.

Known consequence: **`just discover` fails while this catalog is loaded.**
`test_discovery_live.py` asserts the seeded `customer_profile` is among the top 10 hits for
`custo`, and then resolves *every* returned URN over GraphQL — which now includes group-owned
dbt datasets that raise `MalformedResponseError` (see the trial's headline finding).
`just check`, `just bench` and `test_fixture_drift` are unaffected: different URNs, captured
fixtures.

```bash
just reset     # destroy the catalog and rebuild it from the seed
```

That removes everything this trial ingested **and the three claim artifacts it published**.
A published verdict is an append-only timeseries aspect and `DELETE` on an assertion returns
200 while removing nothing, so `just reset` is the only complete undo.
