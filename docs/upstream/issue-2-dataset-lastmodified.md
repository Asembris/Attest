# Draft issue 2 — Dataset `lastModified`

**Repo:** `acryldata/mcp-server-datahub`
**Title:** Dataset `properties.lastModified` is never requested, so no tool can answer "when did this table last change?"

---

### Summary

No MCP tool returns a dataset's `lastModified` timestamp. The Dataset fragments in
`gql/entity_details.gql` and `gql/search.gql` do not request it, so freshness/staleness — a
common thing to ask a catalog about — is unanswerable via MCP, even though GMS returns it and
the field is `NON_NULL` in the schema.

`lastModified` **is** requested for `Dashboard`, `Chart`, `Document`, and
`BusinessAttribute`. Dataset appears to have been missed rather than deliberately excluded.

This looks like the same shape as #149 (`Dataset.externalUrl` present in GMS, requested for
other entity types, missed for Dataset — a one-line fix to the same fragment) and #118
(`erModelRelationship` returning a bare URN because the fragment does not ask for its
properties).

Note this is **not** what #159 asks for. That proposes exposing `systemMetadata` envelope
provenance — `lastObserved`, ingestion run ids, audit actors — and rightly points out that
ingestion/observation time is not validation time. `properties.lastModified` is the
business/source timestamp, and it is the one a freshness question is actually about.

### Environment

- `mcp-server-datahub` 0.6.0 (via `uvx`)
- DataHub Core **v1.5.0.6**, self-hosted quickstart
- Python 3.12

### Reproduction

`get_entities` on any dataset returns `properties` with no timestamp of any kind:

```jsonc
"properties": {
  "name": "customer_profile",
  "description": "One row per customer. ...",
  "customProperties": [ { "key": "hasPII", "value": "true" } ]
}
```

Same GMS, same instant, over GraphQL:

```graphql
{ dataset(urn: "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)") {
    properties { lastModified { time } } } }
```

```json
{ "data": { "dataset": { "properties": { "lastModified": { "time": 1784125241353 } } } } }
```

Measured across 16 seeded datasets: 15 have a `lastModified` in GMS, and **0** are reachable
through any MCP tool. (The 16th genuinely has none.)

### Root cause

`gql/entity_details.gql`, `fragment entityPreview on Entity`, the Dataset arm:

```graphql
... on Dataset {
    name
    platform { ...platformFields }
    editableProperties { name description }
    properties {
        name
        description
        customProperties { key value }
    }
    ...
}
```

No `lastModified`. Compare the Dashboard arm in the same file:

```graphql
... on Dashboard {
    properties {
        name
        description
        externalUrl
        access
        lastModified { time }
    }
```

`gql/search.gql` requests only `properties { name }` for a Dataset, so the search path does
not carry it either. `DatasetProperties.lastModified` is `NON_NULL AuditStamp` in Core
v1.5.0.6.

### Impact

"When was this table last updated?" and "is this data stale?" are among the most common
questions asked of a catalog, and they are currently unanswerable through the MCP server for
the entity type most likely to be asked about. An agent asked whether a dashboard's upstream
table is fresh can retrieve the timestamp for the *dashboard* but not for the *table*.

For our part we were building a checker that verifies freshness claims ("this table is
updated daily") against the catalog; with no timestamp the check has no input at all, and the
honest output is "the catalog is silent" — which is wrong, because the catalog is not silent,
the query just didn't ask.

### Suggested fix

Add to the Dataset arm of `entityPreview`:

```graphql
    properties {
        name
        description
        customProperties { key value }
        lastModified { time }
    }
```

`created { time }` may be worth including for the same reason; `AuditStamp.actor` is
available too, if "who last changed this" is useful. Token cost is a handful of tokens per
entity.

Happy to open a PR.
