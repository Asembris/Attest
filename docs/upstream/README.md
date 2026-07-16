# Upstream issue drafts — `acryldata/mcp-server-datahub`

**Status: DRAFTED, NOT FILED.** Awaiting review.

These came out of [`docs/mcp-evaluation.md`](../mcp-evaluation.md) — the measured finding
that the DataHub MCP server's read tools cannot serve a lossless structured read of a
dataset. All three are reproducible against `mcp-server-datahub` **0.6.0** + DataHub Core
**v1.5.0.6**, and all three are evidenced by [`spikes/mcp_reader_probe.py`](../../spikes/mcp_reader_probe.py)
(`just spike-mcp`).

| Draft | Title | Confidence it is a defect |
| --- | --- | --- |
| [1](issue-1-schema-field-data-loss.md) | Schema fields drop `type` and return tag/term display names instead of URNs | **High.** The data is already in the response; `type` is commented out and its reader still exists. |
| [2](issue-2-dataset-lastmodified.md) | Dataset `properties.lastModified` is never requested by any tool | **High.** Exactly the shape of upstream #118, which has a PR. |
| [3](issue-3-absent-vs-empty.md) | `clean_gql_response` strips nulls and empties, collapsing "absent" and "empty" | **Low-medium, and weaker than we first thought.** A real ambiguity, but no measured wrong answer, the rationale is documented, and it may be working as intended. Filed as a question. |

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

Open issues reviewed 2026-07-16. Nothing matching. Related, and referenced in the drafts:

- **#118** — `get_entities` returns bare URN for `erModelRelationship`; root cause is a
  missing fragment, i.e. *the query does not ask for data the backend has*. Same shape as
  draft 2. Has PR #119.
- **#88** — hardcoded description truncation too aggressive; produced the
  `DESCRIPTION_LENGTH_LIMIT` env var. Precedent that hardcoded lossy transforms are
  accepted as fixable.
- **#131** — older-GMS breakage. Establishes that OSS/self-hosted GMS is in scope.
