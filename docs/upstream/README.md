# Upstream issue drafts — `acryldata/mcp-server-datahub`

**Status: drafts 1 and 2 are FILED upstream** — [#169](https://github.com/acryldata/mcp-server-datahub/issues/169)
and [#168](https://github.com/acryldata/mcp-server-datahub/issues/168), opened 2026-08-02, no
maintainer response yet. **Draft 3 is deliberately NOT filed** and stays a draft — see below.

These came out of [`docs/mcp-evaluation.md`](../mcp-evaluation.md) — the measured finding
that the DataHub MCP server's read tools cannot serve a lossless structured read of a
dataset. All three are reproducible against `mcp-server-datahub` **0.6.0** + DataHub Core
**v1.5.0.6**, and all three are evidenced by [`spikes/mcp_reader_probe.py`](../../spikes/mcp_reader_probe.py)
(`just spike-mcp`).

| Draft | Title | Upstream | Confidence it is a defect |
| --- | --- | --- | --- |
| [1](issue-1-schema-field-data-loss.md) | Schema fields drop `type` and return tag/term display names instead of URNs | Filed: [#169](https://github.com/acryldata/mcp-server-datahub/issues/169) | **High.** The data is already in the response; `type` is commented out and its reader still exists. |
| [2](issue-2-dataset-lastmodified.md) | Dataset `properties.lastModified` is never requested by any tool | Filed: [#168](https://github.com/acryldata/mcp-server-datahub/issues/168) | **High.** Exactly the shape of upstream #118, which has a PR. |
| [3](issue-3-absent-vs-empty.md) | `clean_gql_response` strips nulls and empties, collapsing "absent" and "empty" | Not filed | **Low-medium, and weaker than we first thought.** A real ambiguity, but no measured wrong answer, the rationale is documented, and it may be working as intended. Framed as a question. |

**On draft 3, and why it is worth reading before filing.** The first version of this finding
claimed the empty-array strip erased a glossary term's "no parent nodes" state — the evidence
that a term is deliberately *not* PII — which would have been the most thematic result in the
whole evaluation. **It is false.** `count` is a scalar, so `{count: 0, nodes: []}` survives as
`{count: 0}` and the state stays legible. Checking it is what demoted this from a headline
defect to a question, and the draft says so in its own caveats. Filing the elegant version
would have been publicly wrong on a repo we are trying to help, and would have discredited
drafts 1 and 2 — which are solid.

Not filed as issues, deliberately:

- **Schema truncation to `ENTITY_SCHEMA_TOKEN_BUDGET`.** Working as intended, documented via
  `schemaFieldsTruncated`, and `list_schema_fields` exists precisely to page past it. It is a
  caveat for structured consumers, not a defect. Noted in the evaluation instead.
- **`get_entities` returning `isError: False` for a missing entity.** Defensible — a batch
  call should not fail wholesale because one URN in the array is bad, and the per-entity
  `{"error": ...}` dict is a reasonable shape. A caller contract note at most.

## Checked for duplicates

**Re-scanned 2026-08-02** (26 open issues; the repo moved a long way from the 2026-07-16 scan,
which is why the recheck happened *before* filing rather than after). **Nothing matching any of
the three drafts.** Targeted searches for `lastModified`, `freshness`/`stale`/`timestamp`,
`glossary`, and `schema field type` return no issue claiming either defect.

Related, and referenced in the drafts:

- **#149** — *Expose `Dataset.externalUrl` through `get_entities`.* Open, with a PR and a test.
  **The closest precedent to draft 2, and a closer one than #118**: same file
  (`entity_details.gql`), same fragment (`entityPreview`), same Dataset arm, same argument —
  the field is already requested for other entity types and was missed for Dataset — and the
  same one-line fix. Cite this one.
- **#159** — *Expose optional read-only aspect audit context from `get_entities`.* Open.
  **Adjacent to draft 2 and NOT a duplicate**, and the distinction is worth keeping straight: it
  asks for `systemMetadata` envelope provenance (`lastObserved`, ingestion run ids, audit
  actors) and states explicitly that ingestion/observation time is **not** validation time.
  Draft 2 asks for `DatasetProperties.lastModified` — the business/source timestamp. #159 draws
  exactly the line that leaves draft 2's field unaddressed.
- **#157** — *`get_entities` returns no aspects for `schemaField` urns.* Open. Adjacent to
  draft 1, not a duplicate: a missing `SchemaFieldEntity` branch and `structuredProperties`,
  nothing about `type` or display-name flattening.
- **#139** — *`grep_documents` silently omits unresolvable, empty, and zero-match documents
  alike, blocking fail-closed callers.* Open. Draft 3's philosophy on a different tool — a
  caller cannot tell **why** something is absent. Not a duplicate; the right cross-reference if
  draft 3 is ever filed.
- **#118** — `get_entities` returns bare URN for `erModelRelationship`; root cause is a
  missing fragment, i.e. *the query does not ask for data the backend has*. Same shape as
  draft 2. Still **open**; PR #119 has not landed.
- **#88** — hardcoded description truncation too aggressive; produced the
  `DESCRIPTION_LENGTH_LIMIT` env var. Still open, now with PR #129
  (`DESCRIPTION_LENGTH_OVERRIDES`). Precedent that hardcoded lossy transforms are accepted as
  fixable.
- **#131** — older-GMS breakage. Establishes that OSS/self-hosted GMS is in scope.
