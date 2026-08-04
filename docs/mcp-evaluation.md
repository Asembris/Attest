# Engaging with the DataHub MCP Server

**Challenge 1 names the DataHub MCP Server as how agents obtain catalog context. Rather than
skip the named path, we scoped an adapter against it, built to the one-method seam it would
implement, and measured whether its responses can carry a deterministic verdict. This document
is that measurement, and the reason Attest's catalog reads stay on GraphQL.**

Two things to know before the numbers. **The server runs** — compatibility is the failure
everyone expects here, and it is not what happened; every call answered, for every seeded
dataset, without error. And the finding is about **structured consumers**, not about the
server's intended use, where its compaction is a feature. Three of the four mechanisms behind
it are fixable upstream, and we wrote them up.

| | |
| --- | --- |
| **Status** | Decided, with evidence. A measured result, not a preference. |
| **Spike** | [`spikes/mcp_reader_probe.py`](../spikes/mcp_reader_probe.py) — `just spike-mcp` |
| **Measured against** | `mcp-server-datahub` **0.6.0**, DataHub Core **v1.5.0.6** (pinned), 16 seeded datasets |
| **Contributed back** | Three write-ups with reproductions in [`docs/upstream/`](upstream/) — two filed upstream ([#169](https://github.com/acryldata/mcp-server-datahub/issues/169), [#168](https://github.com/acryldata/mcp-server-datahub/issues/168)), the third deliberately kept as a draft. A fix for #168 is proposed at [PR #182](https://github.com/acryldata/mcp-server-datahub/pull/182), open |
| **Tripwire** | `just spike-mcp` exits non-zero **by design**. If it ever goes green, the finding has expired and this decision is worth reopening. |
| **What DOES use this server** | its `search` tool, for **catalog discovery** in the URN picker — a human picking a name, never a checker reading a fact. Added after this evaluation and bounded by it: [§9](#9-what-changed-after-this-evaluation-search-for-discovery). |

**Jump to:** [the result](#2-the-result) ·
[the mechanism, at source level](#3-the-mechanism-at-source-level) ·
[why it is a correctness finding](#4-why-this-is-a-correctness-finding-not-a-coverage-one) ·
[what this does not prove](#6-what-this-evaluation-does-not-prove) ·
[what we are contributing back](#7-what-we-are-contributing-back) ·
[reproduce it](#8-reproduce-it) ·
[what changed after: search, for discovery](#9-what-changed-after-this-evaluation-search-for-discovery)

---

## What we set out to do

Challenge 1 names the **DataHub MCP Server / Agent Context Kit** as the way agents obtain
catalog context, and Attest's catalog read already had the one-method seam an adapter would
implement. That made the question concrete and answerable rather than a matter of taste:

> **Does an MCP response contain the facts a deterministic verdict is made of?**

So we built to that seam, ran the real server against the real catalog, and diffed the result
against the reference read — dataset by dataset, and then verdict by verdict.

The short version:

> **The DataHub MCP server is well-built for the job it has — feeding a language model — and
> each optimisation for that job removes something a deterministic checker needs. Fed the MCP
> snapshot, four of five TRUE claims change verdict, and a correctly-tagged PII column reads
> back as `Contradicted`.**

That is not a footnote to Attest's thesis. It *is* Attest's thesis, encountered in the wild: a
transport tuned for a reader that can shrug, feeding one that cannot.

---

## The server runs. Compatibility is not the finding.

Worth stating plainly and early, because it is the failure everyone expects and it is not what
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

Four defects, each verified in the server's own source rather than inferred from a response —
which is what makes three of them a concrete upstream fix rather than a mystery. None is
configurable: there is no raw mode, and the only environment variables the server reads are
token budgets and document-tool switches, so an adapter cannot opt out of any of them.

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

### 3e. Two further caveats for a structured consumer

Neither is a defect — both are reasonable behaviour for the server's intended consumer, and
[§7](#7-what-we-are-contributing-back) says why neither was written up. They are recorded
because an adapter author needs to know about them.

- **Schemas are truncated to a token budget.** `ENTITY_SCHEMA_TOKEN_BUDGET` (default 16000),
  plus a `TOOL_RESPONSE_TOKEN_LIMIT` of 80000, with a `schemaFieldsTruncated` marker. It
  does not fire on the seed (5-column tables) and it is a latent **correctness** risk on a
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

## 5. The conclusion

**Attest reads DataHub over GraphQL because a groundedness auditor requires a lossless read,
and the MCP server — correctly, for its own purpose — does not provide one today.**

The server's transformations are not bugs from its own point of view. Stripping nulls and
empty collections, replacing URNs with human-readable names, truncating schemas to a token
budget: every one of those is *right* when the consumer is a language model reading prose
context and paying by the token. It is a well-built tool for that job, and it does that job.

They are the wrong trade when the consumer is deterministic code whose entire product is the
difference between *disagreement* and *silence*. That is a mismatch between a transport and a
consumer, and naming it is more useful than assigning fault to either.

So the finding is not "MCP is bad", and it is not "we couldn't". It is:

> **The named path encodes meaning for a reader that can shrug; Attest is a reader that
> cannot. We measured the gap rather than assuming it, it is real on these versions, and
> three of the four mechanisms behind it are fixable upstream — so we wrote them up.**

The engagement is the point. Skipping the named path would have cost nothing and proved
nothing. Building to its seam produced a measurement, three drafts with reproductions, and a
tripwire that will tell us the day the finding expires.

### What we did *not* do, and why

- **We did not ship a schema-only MCP reader.** A reader that serves the one claim type that
  survives (schema) while refusing freshness and classification is oddly-shaped and buys
  nothing this document does not already say.
- **We did not add MCP as a "demonstrated but non-verdict" context path.** Running the
  inverting transport alongside the real read, purely to be able to say we touched MCP,
  would be the hollow checkbox integration this finding argues against — and a second read
  of the same catalog re-opens the consistency boundary that
  [cache.py](../src/attest/datahub/cache.py) exists to hold (one run, one frozen read per entity).
  Exercising a path we just proved cannot carry a verdict, with a note explaining that it
  doesn't decide anything, is decoration.

  **What we DID later ship is a different act, and §9 draws the line.** The MCP server now
  backs the URN picker's catalog search — *discovery*, which hands a human a list of
  candidate URNs. It is not a context path: nothing it returns is read by a checker, nothing
  it returns is evidence, and it reads no entity an audit will read. Every mechanism measured
  above is a loss of **field content**; the one value discovery passes on is the **entity
  URN**, which arrives intact.
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
- **It tests the one read tool an adapter would use** — `get_entities`, on explicit URNs.
  Schema fields arrive embedded in that response, so `list_schema_fields` is never called —
  the schema-truncation caveat recorded above is read off the server's source, not measured
  here. It does not evaluate lineage or query tools, which are not what a `Reader` needs.
  **It did not evaluate `search` either — and that gap was later closed rather than left
  standing. See [§9](#9-what-changed-after-this-evaluation-search-for-discovery).**
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

## 7. What we are contributing back

Most of these affect **any** consumer that needs structured metadata, not just Attest, and
none had an existing issue (open issues reviewed 2026-07-16). Drafts, with reproductions, are
in [`docs/upstream/`](upstream/):

| Finding | Upstream status |
| --- | --- |
| `type` commented out; field tags/terms as display names | **Filed: [#169](https://github.com/acryldata/mcp-server-datahub/issues/169)** (draft 1). The data is already in the response and `type`'s reader still exists — the fix is cheap and costs an LLM consumer nothing. |
| Dataset `lastModified` never requested | **Filed: [#168](https://github.com/acryldata/mcp-server-datahub/issues/168)** (draft 2), with a fix proposed at **[PR #182](https://github.com/acryldata/mcp-server-datahub/pull/182)** — open; not reviewed, not merged. Add it to the fragment. Exactly the shape of upstream #118 (missing fragment ⇒ data the backend has never ships), which has a PR. |
| `clean_gql_response` collapses absent and empty | **Draft 3, framed as a question.** Real ambiguity, no measured wrong answer, documented rationale — it may be working as intended. |
| Schema truncated to a token budget | **Not filed.** Working as intended, flagged via `schemaFieldsTruncated`, and `list_schema_fields` exists to page past it. A caveat for structured consumers, not a defect. |
| `get_entities` returns `isError: False` for a missing entity | **Not filed.** Defensible: a batch call shouldn't fail wholesale over one bad URN, and the per-entity `{"error": ...}` dict is a reasonable shape. |

Drafts 1 and 2 are the substantive ones. Draft 3 is included honestly rather than
persuasively: its most compelling version — that the empty-array strip erases a glossary
term's deliberately-not-PII state — is **false**, and we found that out by checking rather
than by shipping it (§3d).

---

## 8. Reproduce it

```bash
pip install -e '.[mcp]'  # the client; an optional extra, never a runtime dependency
just spike-mcp           # needs live DataHub + uvx; exits non-zero BY DESIGN
just discover            # the search path that IS shipped (§9); exits ZERO
```

The probe prints the per-dataset diff, the mismatch summary, and the verdict table. It
downloads the MCP server on first run and reads the same 16 seeded datasets the benchmark
uses.

> **Note for this network:** `uvx` ships its own Rust root store and fails TLS interception
> until told to use the OS trust store — hence `--native-tls` in the probe's server
> parameters. That is the same corporate-CA trap as Python's `truststore` injection and
> Node's `NODE_USE_SYSTEM_CA`, a third runtime over. See CLAUDE.md's landmines.

---

## 9. What changed after this evaluation: `search`, for DISCOVERY

**Everything above still stands.** The catalog READ is GraphQL, `just spike-mcp` still exits
non-zero by design, and no MCP response has ever reached a checker. What was added later is a
different act on a different tool, and the distinction is worth stating precisely because
"we use MCP now" and "we read the catalog over MCP" are not the same sentence.

> **MCP discovers. A human resolves. GraphQL verifies. Deterministic code decides.**

The URN picker in Attest's UI searches your catalog through this server's `search` tool
(`GET /catalog/search` → [`src/attest/discovery/`](../src/attest/discovery/)). It used to
filter a static list generated from the seed manifest — a list that describes one seeded
catalog and is useless against any real DataHub. Search is the fix, and an LLM-facing,
human-in-the-loop lookup is precisely the job this server is built for.

### Why the finding above does not apply to it

| | The adapter this document refused | The discovery path that shipped |
| --- | --- | --- |
| What is consumed | every field of a `DatasetSnapshot` | one field: `entity.urn` |
| What §12's mechanisms damage | tags→display names, `type` dropped, `lastModified` absent, absent≡empty | **none of it** — the URN arrives intact |
| Who decides | a checker, deterministically | a **human**, by clicking |
| What a wrong answer costs | a confident false verdict, silently | a URN the person can see is wrong, in their own text |
| Reads an entity an audit will read | yes — the §2c consistency boundary | no — it runs before any audit exists |

Every mechanism measured in [§3](#3-the-mechanism-at-source-level) is a loss of **field
content**. The entity URN is not field content — it is the identity the response is keyed by,
and it comes back byte-identical. `tests/test_discovery_live.py` proves that where it matters:
every URN the MCP server returns is fetched over GraphQL into a real `DatasetSnapshot`. If the
identifier were lossy, that fetch would fail.

### And the boundary is asserted, not described

- **Nothing in the verdict path can import the discovery module.** Walked over the real import
  graph from the checkers, the snapshot, the run-scoped cache and the pipeline —
  `tests/test_discovery_boundary.py`, in the house style of `NO_LLM_IN_THE_VERDICT_PATH`.
  Exactly one module in `attest` imports it, and it is the HTTP layer.
- **Changing every field of a search result except the URN cannot change a verdict.** Proven
  by running real audits over mutated payloads — including one carrying a `NonPII` tag, a
  wrong owner and a description reading "THIS TABLE CONTAINS NO PII". None of it reaches
  anything, and the non-vacuity proof is that changing the URN *does* change the audit.
- **A picked URN is not a resolved entity.** It must appear **verbatim in the agent's text**
  (`api/schemas.py`) and the decomposer may quote a URN but never mint one. So a wrong pick
  produces claims about an explicitly wrong URN — visible in the report and in the published
  artifact — rather than a resolution error laundered into catalog disagreement, which is what
  CLAUDE.md §4 exists to prevent.
- **An outage is never an empty result.** The server strips empty arrays, so a zero-match
  response and a response that lost its results differ by one key; `total` is the
  discriminator. Empty is a 200, malformed and unreachable are a 503, and a missing client
  library is a 501. Rendering any of the last three as "your catalog has nothing like that"
  would be this project's cardinal sin in its own read path — the same collapse Attest already
  shipped once at the model provider (CLAUDE.md §18) and once at the catalog read (§20), which
  §4 above predicted would happen here.

### What this does not claim

It does not claim the parity finding expired — it has not, and the tripwire still says so. It
does not claim discovery is verification: every response carries `advisory: true` and the UI
says so where the results are. And it does not make MCP load-bearing: without the optional
client the search answers **501**, the picker offers manual URN entry, and audits are
completely unaffected.
