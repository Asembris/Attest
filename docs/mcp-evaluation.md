# Why Attest reads DataHub over GraphQL and not over MCP

**Status:** decided, with evidence. This is a measured result, not a preference.
**Spike:** [`spikes/mcp_reader_probe.py`](../spikes/mcp_reader_probe.py) (`just spike-mcp`)
**Measured against:** `mcp-server-datahub` **0.6.0**, DataHub Core **v1.5.0.6** (pinned), 16 seeded datasets.

---

## The question

Challenge 1 names the **DataHub MCP Server / Agent Context Kit** as the way agents obtain
catalog context. Attest reads the catalog with direct GraphQL over `httpx`. So:

> "Why aren't you using the named integration path?"

The honest answer is not a preference, and it is not "we ran out of time". We built the
adapter's seam, evaluated the named path against the real server, and **measured that it
cannot carry a deterministic verdict.** This document is that measurement.

The short version:

> **The DataHub MCP server is correct for the job it has — feeding a language model — and
> its every optimisation for that job is destructive for a deterministic checker. Fed the
> MCP snapshot, four of five TRUE claims change verdict, and a correctly-tagged PII column
> reads back as `Contradicted`.**

That is not a footnote to Attest's thesis. It *is* Attest's thesis, encountered in the wild.

---

## 1. What was tested, and how

Attest's catalog read already had the seam an MCP adapter would use.
[`datahub/cache.py`](../src/attest/datahub/cache.py) defines `Reader` — a protocol with
exactly one method:

```python
class Reader(Protocol):
    def fetch_dataset(self, urn: str) -> DatasetSnapshot: ...
```

The checkers consume `DatasetSnapshot` and never see the transport. So an MCP adapter is
well-defined: implement that one method against MCP tool calls, map the responses into the
same normalized snapshot, change nothing else. The question is purely **whether the MCP
response contains the facts a `DatasetSnapshot` is made of.**

The probe answers it by building each snapshot **twice** — once through Attest's GraphQL
client, once through the MCP server's `get_entities` tool — and diffing them field by field
across every seeded dataset. GraphQL is the reference because it is what every measured
number in this project was measured against.

**The MCP normalizer in the spike is deliberately written to give MCP its best shot.** It
reads every field the tool actually returns and reconstructs as much of the snapshot as the
response physically permits. Where it gives up, the field is not there to be read. That
distinction is the whole point: a gap the probe reports is a gap in the **data**, not in the
mapping. A normalizer written any other way would be measuring its own author.

**No LLM is involved anywhere in this evaluation.** MCP is a protocol, not a model. The
probe calls a fixed tool with fixed JSON arguments from code, on an explicit URN, exactly as
the adapter would have. Nothing decides which tool to call and nothing summarises anything.

### The server runs. Compatibility is not the finding.

Worth stating plainly, because it is the failure everyone expects and it is not what
happened:

- The server **installs and runs** against Core v1.5.0.6.
- It **detects the deployment correctly**: `is_oss=True`, `is_cloud=False`, and it
  version-gates its own `#[NEWER_GMS]` and `#[CLOUD]` query fragments accordingly.
- It **answers every call** we made, for every seeded dataset, without error.
- It exposes six read tools: `search`, `get_entities`, `list_schema_fields`, `get_lineage`,
  `get_lineage_paths_between`, `get_dataset_queries`. Mutation, user, and data-quality
  tools are disabled by default on OSS.

Everything below is about what those successful responses **contain**.

---

## 2. The result

### Parity: 16/16 datasets fail

```
=== 130 mismatches over 16 datasets ===
   102  field.*
    15  last_modified
    12  custom_properties
     1  term_parents
```

### The same gap, expressed as verdicts

A field diff is abstract. This is the identical gap in the only units the project cares
about — the verdict a human would be shown. **All five claims below are TRUE**, and both
columns come from the same checkers reading the same catalog at the same moment. Only the
transport differs:

| Claim (all five are true) | via GraphQL | via MCP | |
| --- | --- | --- | --- |
| freshness: `customer_profile` updated within 48h | Supported | Insufficient-Coverage | flipped |
| freshness: `revenue_daily` within 24h (stale by a year) | Contradicted | Insufficient-Coverage | flipped |
| **classification: `customer_profile.email` is PII** | **Supported** | **Contradicted** | **flipped** |
| classification: `marketing_leads.work_email` is an EmailAddress | Supported | Insufficient-Coverage | flipped |
| schema: `customer_profile.email` is `VARCHAR(255)` | Supported | Supported | — |

**Four of five verdicts move. One of them moves Supported → Contradicted.**

That third row is the whole finding. `benchmark/README.md` names Supported↔Contradicted as
a **correctness failure** — the worst thing this product can do — as distinct from a
coverage failure. Over the MCP transport, Attest would tell a compliance auditor that a
column the catalog *explicitly tags as PII* is **not** PII. Not "we're not sure". Not.

---

## 3. The mechanism, at source level

Four defects, each verified in the server's own source rather than inferred from a response.
None is configurable: there is no raw mode, and the only environment variables the server
reads are token budgets and document-tool switches.

### 3a. `lastModified` is never requested for a Dataset — by any tool

Not a version gate. `gql/entity_details.gql` asks for `lastModified` on **Dashboard**,
**Chart**, **Document**, and **BusinessAttribute**. The Dataset fragment asks for:

```graphql
properties {
    name
    description
    customProperties { key value }
}
```

`search.gql` asks a Dataset for `properties { name }` and nothing more. So no MCP tool
returns a dataset's `lastModified`, and **`FreshnessClaim` — one of four claim types, three
of the twelve matrix cells — has no input at all.**

The data is right there. Same GMS, same instant:

```graphql
{ dataset(urn: "...customer_profile,PROD)") { properties { lastModified { time } } } }
# -> { "lastModified": { "time": 1784125241353 } }
```

`DatasetProperties.lastModified` is **`NON_NULL AuditStamp`** in Core v1.5.0.6's schema — it
is not merely available, it is guaranteed present. Measured: 15/16 datasets lose it. The
16th is `pipeline_scratch`, which is genuinely `None` in the catalog — it is the
freshness-silence witness, so it agrees by accident.

### 3b. Field tags and terms are flattened to display names, not URNs

`graphql_helpers._clean_schema_fields` keeps the *name*:

```python
field_dict["tags"] = [
    t["tag"]["properties"]["name"]
    for t in tag_list
    if t.get("tag", {}).get("properties") and t["tag"]["properties"].get("name")
]
```

So `urn:li:tag:PII` arrives as `"PII"`, and `urn:li:glossaryTerm:CustomerIdentifier` arrives
as `"Customer Identifier"` — with a space. (A tag with no `properties.name` is dropped
entirely and silently.)

This is fatal twice over. `ClassificationClaim.labels` are tag/term **URNs** by validator —
[claims.py](../src/attest/claims.py) refuses anything else, deliberately, because a label is
an identifier and not a string that looks like one. And `DatasetSnapshot.term_parents` is
keyed by term **URN**, so a column's glossary term can never be joined to the hierarchy that
makes it a classification signal at all.

That hierarchy is not decoration. It is the entire basis on which Attest infers nothing from
a name (CLAUDE.md §6): *`EmailAddress` is a PII signal because someone filed it under the PII
node; `CustomerIdentifier` is deliberately outside it.* Reduced to display strings, both are
just words — and Attest would be back to guessing from names, which is the one thing
[policy.py](../src/attest/checkers/policy.py) exists to forbid.

### 3c. `type` is commented out of the server's own fragment

```graphql
fragment entitySchemaFieldFields on SchemaField {
    fieldPath
    label
    jsonPath
    nullable
    description
    # type          <-- commented out
    nativeDataType
    ...
}
```

`SchemaField.type` is **`NON_NULL SchemaFieldDataType`** in Core v1.5.0.6 — always present,
never null. Downstream, `_clean_schema_fields` still contains `if field_type := f.get("type")`,
so the reader is written for a field the query no longer requests. Measured:
`FieldSnapshot.data_type` is `None` on every column of every dataset.

### 3d. Absent and empty are collapsed — an evidence-fidelity loss, not a verdict flip

`clean_gql_response` recursively drops `None`, `[]`, and `{}`:

```python
for k, v in response.items():
    if k in banned_keys or v is None or v == []:
        continue
    ...
    if cleaned_v is not None and cleaned_v != {}:
        cleaned_response[k] = cleaned_v
```

[snapshot.py](../src/attest/datahub/snapshot.py) exists largely to preserve that
distinction: `None` means the catalog has **no such aspect**; an empty tuple means the aspect
**exists and holds nothing**. Measured: `custom_properties` is `{}` via GraphQL — the aspect
is present and empty — and `None` via MCP, indistinguishable from absent, on **12/16**
datasets.

**Stated precisely, because the flattering version of this finding is false and we wrote it
down before checking.** This one does **not** flip a verdict, and `snapshot.py` says why in
its own docstring: *"Both yield Insufficient-Coverage — an unowned table is unowned either
way — but a checker reporting evidence should be able to say which it saw, and this is the
only layer that still knows."* So what the collapse destroys is **evidence fidelity**: Attest
can still reach the right verdict, and can no longer say whether the catalog was *silent* or
merely *empty*. That is a latent correctness risk for any consumer whose rules do turn on the
difference, and a measured loss of evidence for this one. It is not the headline; findings
3a and 3b are.

The near-miss is worth recording, because it is the reason to distrust a tidy story. The
tempting version went: `CustomerIdentifier` has no parent nodes — it is deliberately outside
the PII node, which is exactly what makes it *not* a PII signal — so its `parentNodes.nodes`
is `[]`, the strip deletes it, and *the evidence that a term is deliberately not-PII is
precisely what gets dropped*. Elegant, thematic, and **wrong**: `count` is a scalar, so
`{count: 0, nodes: []}` survives the strip as `{count: 0}`, and a term with no parents stays
legible. The single `term_parents` mismatch in the run is not this rule at all — it is 3b
(`customer_contact`'s `CustomerIdentifier` is a *column*-level term, and column terms arrive
as display names, so no URN and no hierarchy).

### 3e. Two further landmines the probe surfaced

- **Schemas are truncated to a token budget.** `ENTITY_SCHEMA_TOKEN_BUDGET` (default 16000),
  plus a `TOOL_RESPONSE_TOKEN_LIMIT` of 80000, with a `schemaFieldsTruncated` marker. It
  does not fire on the seed (5-column tables) and it is a latent **correctness** bug on a
  wide one: a dropped column makes `snapshot.field(path)` return `None`, and the schema
  checker reads that as the catalog *positively denying the column exists*. Contradicted,
  from a token budget.
- **A missing entity is not an error.** `get_entities` on a nonexistent URN returns
  `isError: False` and `[{"error": "... not found", "urn": ...}]`. An adapter that does not
  special-case that dict builds an all-`None` snapshot and returns Insufficient-Coverage —
  laundering a hallucinated URN into a legitimate-looking audit result, which is the precise
  failure [`EntityNotFoundError`](../src/attest/datahub/client.py) exists to prevent.

---

## 4. Why this is a correctness finding, not a coverage one

The natural assumption is that a lossy read degrades gracefully: less information, more
Insufficient-Coverage, an auditor that shrugs more often. **That assumption is wrong here,
and the reason is the most interesting thing the spike found.**

Trace the PII flip:

1. The claim asserts `urn:li:tag:PII` on `customer_profile.email`. It is **true**: the
   catalog holds exactly that tag on exactly that column.
2. MCP delivers the column's tag as `"PII"` — a display name. The claimed URN does not
   match it.
3. So the column reads as **unlabelled**.
4. `customer_profile` carries the `Verified` completeness marker, which
   [policy.py](../src/attest/checkers/policy.py) treats as the catalog *granting*
   closed-world reasoning — someone looked, and tagged what they found.
5. `COMPLETENESS_REACHES_COLUMNS` (CLAUDE.md §6) propagates that marker **down**: an
   untagged column of a Verified table is a column that was reviewed and found clean.
6. Rule 4 of `check_classification` therefore returns **Contradicted** — a denial, not
   silence.

**Attest's own completeness rule weaponises the transport's loss into a confident false
denial.** Every step is correct in isolation. The rule in step 5 is right and was hard-won:
it exists because "someone reviewed this and tagged nothing" is genuinely different from
"nobody looked". But it assumes the read is **lossless**, because until now the read was
never a place where meaning could be lost.

That generalizes into the rule this evaluation leaves behind:

> **A transport that is lossy for an LLM is not merely lossy for a checker — it is
> inverting.** Give a language model `"PII"` instead of `urn:li:tag:PII` and it does fine;
> it was going to read the string anyway. Give a deterministic checker the same thing and
> the signal doesn't weaken, it **reverses**, because the checker's precision is exactly
> what makes it unable to shrug.

And note where Attest's defences are. `faithfulness.py`, `polarity.py`, `crosscheck.py`, the
trajectory invariants — all of them fail closed on **the model's output**. Nothing in the
design defends the **catalog read**, because the catalog read was the one thing that could
be trusted to mean what it said. An LLM-shaped transport in front of the catalog puts a
lossy compression stage exactly where the system has no guard, and the failure it produces
is silent, confident, and wrong.

---

## 5. The conclusion, and why it is the stronger answer

**Attest reads DataHub over GraphQL because a groundedness auditor requires a lossless read,
and the MCP server — correctly, for its purpose — does not provide one.**

The MCP server's transformations are not bugs from its own point of view. Stripping nulls
and empty collections, replacing URNs with human-readable names, truncating schemas to a
token budget: every one of those is *right* when the consumer is a language model reading
prose context and paying by the token. It is a well-built tool for that job.

They are precisely wrong when the consumer is deterministic code whose entire product is the
difference between *disagreement* and *silence*.

So the finding is not "MCP is bad" and not "we couldn't". It is:

> **The named integration path encodes meaning as display strings and empty arrays, then
> optimises both away — and Attest exists to say that absence is not an answer. We measured
> the path, it cannot carry a verdict, and here is the receipt.**

You cannot script a better argument for why deterministic grounding needs a lossless read.

### What we did *not* do, and why

- **We did not ship a schema-only MCP reader.** A reader that serves the one claim type that
  survives (schema) while refusing freshness and classification is oddly-shaped and buys
  nothing this document does not already say.
- **We did not add MCP as a "demonstrated but non-verdict" context path.** Running the
  inverting transport alongside the real read, purely to be able to say we touched MCP,
  would be the hollow checkbox integration this finding argues against — and a second read
  of the same catalog re-opens the consistency boundary that
  [cache.py](../src/attest/datahub/cache.py) exists to hold (one run, one frozen snapshot).
  Exercising a path we just proved cannot carry a verdict, with a note explaining that it
  doesn't decide anything, is decoration.
- **We did not touch the write path.** Challenge 1 names MCP for *reading* context and
  separately rewards *writing back*. Attest's write-back is a GraphQL/OpenAPI concern
  (§10, [claim-artifact.md](design/claim-artifact.md)) and this evaluation says nothing
  about it.

---

## 6. What this evaluation does NOT prove

Naming the boundary is what makes the rest credible.

- **It is not a claim that the MCP server is defective for its intended use.** It is a claim
  that it is lossy for *structured* consumers. For an agent assembling context to reason
  over, the compaction is a feature, and the token budgets exist for a real reason.
- **It is one server version against one DataHub version** — `mcp-server-datahub` 0.6.0
  against Core v1.5.0.6. Both move. `just spike-mcp` is the tripwire: it exits **non-zero by
  design**, so if it ever goes green the finding has expired and this decision is worth
  reopening. That is the same discipline as `test_fixture_drift.py` — an assertion that only
  ever passes is a green light wired to nothing.
- **It tests the read tools an adapter would use** — `get_entities` and
  `list_schema_fields`, on explicit URNs. It does not evaluate `search`, lineage, or query
  tools, which are not what a `Reader` needs and which Attest deliberately does not want
  (free-text entity resolution is out of scope by design — CLAUDE.md §4).
- **It does not prove no adapter is possible in principle.** It proves no adapter is possible
  *on this server's responses* without inventing data the catalog did not send. Three of the
  four defects are fixable upstream, and we have written them up (§7). Were they fixed, this
  decision should be revisited — which is exactly what the non-zero tripwire is for.
- **The verdict table is 5 claims, not the 40-case benchmark.** The benchmark cannot be run
  over MCP at all: `benchmark/run_eval.Catalog` reconstructs its reference `now` from
  `last_modified`, so it cannot even *construct* against an MCP-backed source. That is itself
  a measurement, but it means the flip rate is a demonstration on chosen claims, not a score.
  Each of the five was chosen to name one thing the transport drops.

---

## 7. Upstream

Most of these harm **any** consumer that needs structured metadata, not just Attest, and none
had an existing issue (open issues reviewed 2026-07-16). Drafts, with reproductions, are in
[`docs/upstream/`](upstream/):

| Finding | Upstream status |
| --- | --- |
| `type` commented out; field tags/terms as display names | **Draft 1.** The data is already in the response and `type`'s reader still exists — the fix is cheap and costs an LLM consumer nothing. |
| Dataset `lastModified` never requested | **Draft 2.** Add it to the fragment. Exactly the shape of upstream #118 (missing fragment ⇒ data the backend has never ships), which has a PR. |
| `clean_gql_response` collapses absent and empty | **Draft 3, filed as a question.** Real ambiguity, no measured wrong answer, documented rationale — it may be working as intended. |
| Schema truncated to a token budget | **Not filed.** Working as intended, flagged via `schemaFieldsTruncated`, and `list_schema_fields` exists to page past it. A caveat for structured consumers, not a defect. |
| `get_entities` returns `isError: False` for a missing entity | **Not filed.** Defensible: a batch call shouldn't fail wholesale over one bad URN, and the per-entity `{"error": ...}` dict is a reasonable shape. |

Drafts 1 and 2 are the substantive ones. Draft 3 is included honestly rather than
persuasively: its most compelling version — that the empty-array strip erases a glossary
term's deliberately-not-PII state — is **false**, and we found that out by checking rather
than by shipping it (§3d).

---

## 8. Reproduce it

```bash
pip install mcp          # the client; deliberately not an Attest dependency
just spike-mcp           # needs live DataHub + uvx; exits non-zero BY DESIGN
```

The probe prints the per-dataset diff, the mismatch summary, and the verdict table. It
downloads the MCP server on first run and reads the same 16 seeded datasets the benchmark
uses.

> **Note for this network:** `uvx` ships its own Rust root store and fails TLS interception
> until told to use the OS trust store — hence `--native-tls` in the probe's server
> parameters. That is the same corporate-CA trap as Python's `truststore` injection and
> Node's `NODE_USE_SYSTEM_CA`, a third runtime over. See CLAUDE.md's landmines.
