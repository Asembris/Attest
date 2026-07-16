# Draft issue 3 — absent vs empty

**Repo:** `acryldata/mcp-server-datahub`
**Title:** `clean_gql_response` strips empty collections, so consumers cannot distinguish "aspect absent" from "aspect present but empty"
**Type:** question / discussion as much as defect. See "honest caveats" below.

---

### Summary

`clean_gql_response` recursively removes `None`, `[]`, and `{}` from responses. That makes
three different catalog states arrive identically as *"the key is not there"*:

1. the aspect does not exist on the entity,
2. the aspect exists and holds nothing,
3. the query did not ask for it.

For a catalog, (1) and (2) are genuinely different facts. "Nobody has classified this table"
and "someone reviewed this table and attached nothing" are different answers to a governance
question, and the difference is often the whole answer.

### Environment

- `mcp-server-datahub` 0.6.0 (via `uvx`)
- DataHub Core **v1.5.0.6**, self-hosted quickstart

### Reproduction

A dataset whose `customProperties` aspect exists but is empty. GMS:

```graphql
{ dataset(urn: "...analytics.staging.raw_events,PROD)") {
    ownership { owners { owner { ... on CorpUser { urn } } } }
    tags { tags { tag { urn } } }
    properties { customProperties { key value } } } }
```

```json
{ "ownership": null,
  "tags": null,
  "properties": { "customProperties": [] } }
```

GMS is being precise here: `ownership` and `tags` are **null** (no aspect), while
`customProperties` is **`[]`** (the aspect is present and empty).

The same entity via `get_entities`:

```jsonc
{
  "urn": "...",
  "name": "raw_events",
  "properties": { "name": "raw_events" },   // customProperties key is gone
  // no "ownership" key
  // no "tags" key
  ...
}
```

All three distinctions are gone. Measured across 16 datasets: `customProperties` is `[]` in
GMS and absent from the MCP response on 12 of them.

### Root cause

`graphql_helpers.clean_gql_response`:

```python
for k, v in response.items():
    if k in banned_keys or v is None or v == []:
        continue
    cleaned_v = clean_gql_response(v)
    ...
    if cleaned_v is not None and cleaned_v != {}:
        cleaned_response[k] = cleaned_v
```

`v == []` drops present-but-empty collections identically to `v is None`.

### Impact

The consumer cannot ask "has anyone looked at this?" — only "is there data here?". Those are
the same question only if you assume the catalog is complete, which is exactly the assumption
a catalog exists to avoid.

Concretely, for a caller trying to reason about coverage:

- `tags` absent could mean the table is untagged, or that tagging was never attempted. An
  agent asked "is this table PII-free?" cannot distinguish "nobody reviewed it" from
  "reviewed, no PII found" — and those warrant opposite answers. Note this affects LLM
  consumers too, not only structured ones: the model is reasoning from the same ambiguous
  response.
- `customProperties` absent could mean no upstream classifier has written findings, or that
  one ran and found nothing.

We hit this building a tool that reports *why* it reached a verdict — it distinguishes "the
catalog is silent" from "the catalog is empty here" in its evidence, and over MCP it can no
longer tell a reader which it saw.

### Honest caveats

Stating these because they bound the claim, and because we got one of them wrong first:

- **We did not measure this changing an answer**, only degrading the evidence behind one. In
  our checkers both states legitimately yield the same verdict; what is lost is the ability
  to *report* which was observed. It is a fidelity/ambiguity problem, not a demonstrated
  wrong result.
- **Scalars rescue some cases.** `parentNodes: {count: 0, nodes: []}` survives as
  `{count: 0}`, because `count` is a scalar — so a glossary term with no parent nodes is
  still legible as such. We initially believed the strip erased that and it does not.
- **The rationale is sound and documented.** The docstring is explicit that this is noise
  reduction for LLM consumers, and for most fields it plainly is. We are not arguing the
  default is wrong for the primary audience.

So this may well be **working as intended**, and the useful question is narrower than "stop
stripping":

### Suggested direction

Rather than a behaviour change, either would resolve the ambiguity for structured callers:

1. **Do not strip empty collections when the aspect exists** — i.e. drop `v == []` from the
   condition and keep `v is None`. This preserves exactly the null-vs-empty distinction GMS
   itself draws, at a cost of a few tokens for the rare present-but-empty aspect.
2. **Gate it** behind an env var (as `DESCRIPTION_LENGTH_LIMIT` was for #88), defaulting to
   today's behaviour.

Is the current collapse intentional, or incidental to stripping nulls? If intentional, a note
in the tool descriptions saying an absent key does not imply an absent aspect would let
callers reason correctly, and would cost nothing.
