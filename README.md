# Attest

A groundedness auditor for AI agents' claims about data. Attest takes a claim an
agent made ("the `customers` table is owned by Alice and contains no PII"),
checks it against DataHub's catalog as ground truth, and returns one of three
verdicts:

| Verdict | Meaning |
| --- | --- |
| **Supported** | The catalog affirms the claim. |
| **Contradicted** | The catalog positively disagrees with it. |
| **Insufficient-Coverage** | The catalog is silent. Not evidence against — just absent. |

Keeping *Contradicted* and *Insufficient-Coverage* distinct is the whole point.
An auditor that collapses "the catalog says this is false" into "the catalog
doesn't say" is worse than no auditor.

## Status

**Session 1: the deterministic core.** Claim schema, DataHub client, and four checkers —
freshness, ownership, classification, schema. There is no LLM anywhere in this layer, by
design: the deterministic half is what has to be bulletproof, so it was built and tested
in isolation before any model touched it.

**Session 2: the semantic layer.** Claim decomposition and explanation generation, sitting
on top of the core and never replacing it. The model gets to *phrase* a verdict; it does
not get to reach one. 113 tests against the live seeded catalog.

Not built yet: LangGraph orchestration and the FastAPI surface (Session 3).

## The coverage matrix

Four claim types × three verdicts = **twelve cells**, and every one of them is
reachable against the live catalog. This table *is* the design of the ground truth.

| | Supported | Contradicted | Insufficient-Coverage |
| --- | --- | --- | --- |
| **Freshness** | fresh timestamp within the window | `revenue_daily`, 417 days stale | `pipeline_scratch` — **no `lastModified`** |
| **Ownership** | `customer_profile` is alice.chen's | `support_tickets` is carol's, not dana's | `raw_events` — **no owner** |
| **Classification** | `customer_profile` is tagged PII | `recipient_email_hash` is tagged `NonPII` | `raw_events` — **no tags, no terms** |
| **Schema** | `email` is `VARCHAR(255)` | `orders_fact` has no `ssn` column | `external_report` — **no `schemaMetadata`** |

The right-hand column is the one that matters, and it is the one that is easy to get
wrong. Insufficient-Coverage is reachable **only where the relevant aspect is genuinely
absent from the catalog** — which is why two datasets exist purely to be silent in one
specific way. Without them, freshness and schema could only ever return Supported or
Contradicted, their third branch would be dead code against real data, and every claim
that belonged in that cell would land somewhere else, confidently. Every test would
still have passed.

[`tests/test_coverage.py`](tests/test_coverage.py) asserts all twelve cells against the
live catalog, so this cannot silently regress. `just matrix` runs it alone.

## Layout

```
src/attest/            Attest's own code. Talks to DataHub over raw GraphQL (httpx).
  claims.py            Claim / Verdict / Evidence schema (pydantic).
  config.py            Per-step model config. Never hardcode a model.
  checkers/            The deterministic core. One checker per claim type. NO LLM.
    policy.py          Declared governance semantics — PII_SIGNALS, exclusions, precedence.
  datahub/
    client.py          GraphQL client: datasets, structured properties, search.
    snapshot.py        Normalized read model. Preserves "absent" vs "empty".
  --- the semantic layer: phrases verdicts, never decides them ---
  llm.py               The only place a model is called. Strict JSON, temperature=0.
  decompose.py         Agent prose -> typed claims. A URN must be quoted, never minted.
  explain.py           Verdict + evidence -> prose. Falls back to a deterministic template.
  faithfulness.py      The guard. Every factual token must appear in the evidence.
  crosscheck.py        Model disagrees with the checker -> surfaced, never obeyed.
  sanitize.py          Untrusted agent text in, instructions stripped out.
seed/                  Seed catalog generator + ingestion recipe.
spikes/                Throwaway proofs. datahub_probe.py proves the read/write path.
tests/                 Live-catalog pytest suite. The semantic layer runs offline.
```

## Where the model boundary is drawn

The checkers are pure code — date math, set membership, string comparison. Four
questions came up that code cannot answer without inventing semantics, and each was
pushed out rather than guessed at, because a checker that quietly guesses is worse
than one that abstains: its wrong verdict has the same confident shape as a right one.

1. **Entity resolution** — "the customer table" → which URN? Not here. Claims arrive
   with an explicit `target_urn`. Keeping resolution upstream means a resolution error
   can never be laundered into catalog disagreement.
2. **Label opposition** — does `NonPII` contradict `PII`? Nothing in DataHub says so;
   they are two unrelated URNs. Declared as data in
   [`checkers/policy.py`](src/attest/checkers/policy.py), so a tag rename can't
   silently flip verdicts.
3. **The closed-world assumption** — does an untagged table contradict "contains PII"?
   **Normally no.** Tags are open-world; a missing tag is silence, so the verdict is
   Insufficient-Coverage. Reading absence as denial would cry wolf on every
   under-documented table, which is most of them. Contradicted requires the *catalog*
   to declare its classification complete (a `Verified` tag a human applied). Attest
   never assumes closed-world — the catalog grants it, per entity. This is the single
   most consequential rule in the layer, and it is what lets `orders_fact` (reviewed,
   no PII tag) return Contradicted while `legacy_accounts` (unreviewed, no PII tag)
   correctly returns Insufficient-Coverage.
4. **Cross-dialect type equivalence** — is a `text` column a `string`? Partially: both
   of DataHub's type vocabularies are matched exactly, but genuine dialect mapping
   (`int8` ~ `BIGINT`) needs a model of each platform's type system. Still deferred: it
   is a semantic-entailment escalation, not an `if`.

The corollary: **"PII-free" is not the mirror image of "contains PII."** An untagged
table cannot *support* a PII-free claim — nobody has looked. A naive checker returns
Supported there and certifies an unreviewed table as clean, which is a groundedness
auditor manufacturing false assurance. It is Insufficient-Coverage.

## How Attest decides what is PII

By a **named list**, not a string match. The list is
[`PII_SIGNALS`](src/attest/checkers/policy.py), and **any one of the three is sufficient**
to contradict a "PII-free" claim:

| Signal | What it is | Kind |
| --- | --- | --- |
| `urn:li:tag:PII` | The global tag. | explicit |
| A glossary term under the **`PII` node** | `EmailAddress`, `PhoneNumber`, `PersonName`. | implied |
| `hasPII` custom property, truthy | An upstream classifier's finding. | implied |

Real catalogs mark PII in more than one place, and a checker that knows about one of them
**certifies the others clean** — the worst verdict this product can return. So each signal
has a dataset in the seed where it is the *only* signal present, and the test asserts both
that the verdict is right and that the other two signals are genuinely absent:
`hr_headcount` is tagged PII with **zero** glossary terms (a term-only checker calls it
clean), `marketing_leads` carries PII-node terms with **zero** PII tags (a tag-only checker
calls it clean), and `device_telemetry` has only `hasPII=true` (both call it clean). None of
the three can be dropped without a test going red.

`CustomerIdentifier` earns its place the other way round: it is a real, attached,
checkable term that is **not** a PII signal, and a test asserts both halves — a claim
naming it is Supported by exact match, while the same table asked about PII is silent.

Nothing here is inferred. `EmailAddress` is a PII signal because someone **filed it under
the PII node** in the catalog's own hierarchy — not because the string reads as personal.
`CustomerIdentifier` is deliberately *outside* that node: a surrogate key is not personal
data, and a checker that reads "customer" as "PII" flags every table in the warehouse.

`hasPII=false` fires **nothing** — in either direction. A scanner that looked and found
nothing is not a review, and absence of evidence is not evidence of absence. A clean bill
requires a `Verified` completeness marker, which is a human's act.

### When signals disagree: precedence

They *will* disagree, and the disagreement is usually meaningful rather than a mistake.

**Rule A — signals propagate up, never down.** The asymmetry is the whole rule, and it
follows from what a table-level PII claim *means*: "this table contains PII" is
**existential**. It is true if PII is anywhere in the table.

- **Up.** A column tagged PII therefore settles a table-scoped claim, whatever the table's
  own metadata says. Without this, a table nobody classified at table level would answer
  "is this PII-free?" with *silence* while its `email` column sat tagged in the schema —
  and if that table also carried a `Verified` marker, the completeness rule would license
  a denial and answer **Supported**, certifying it clean. That was reachable, and
  [a test](tests/test_pii_signals.py) now makes it unreachable.
- **Down — deliberately not.** The same existential reading forbids the converse:
  "contains PII" does *not* mean "every column is PII". A table's PII tag says nothing
  about its untagged `signup_ts` column, and inheriting it downward would mark every
  column of every PII table as personal data — crying wolf on the entire warehouse. A
  column-scoped claim is answered by that column's own classification, or by silence.

Precedence is about **grain, not about `NonPII`**. The rule is symmetric in direction:
`audit_log` is `email_campaign_stats` inside out — a table with *no* PII signal whose
`actor_email` column is explicitly tagged PII — and the column's tag decides there too,
pointing the other way.

**Rule B — within one grain, an explicit tag beats an implied signal.** A tag is a
classification act performed on that entity; a term is a coarser statement of subject
matter and a property is a machine's guess. When a human's review and a machine's
classification disagree, the review wins.

One ordering is worth stating outright: an explicit PII tag **on a column** outranks a
`NonPII` tag on its table. The more specific classification act is the better evidence,
and a positive finding of personal data should not be talked out of existence by a
coarser label.

The worked example is `email_campaign_stats`, and it is in the seed on purpose:

- The **table** is filed under the `EmailAddress` term. The table genuinely *is* about
  email.
- The **column** `recipient_email_hash` is explicitly tagged `NonPII` — the one column that
  held an address was hashed and de-identified.

This is what a de-identified column in a subject-matter-tagged table looks like in
production, and it happens constantly. By Rule A, a claim about the column is answered by
the column's own tag: **`recipient_email_hash` is not PII, and "this column is PII-free"
is Supported.** By Rule B, the table's `NonPII` tag outranks its own `EmailAddress` term,
so "this table contains PII" is Contradicted.

Precedence resolves the conflict; it does not hide it. The **losing signal is still
returned as evidence**, so an explanation can say why a table filed under `EmailAddress`
came back PII-free. The converse of Rule A matters just as much: a table tagged PII does
*not* make its `signup_ts` column PII. Table-level PII means "somewhere in here", not
"everywhere in here".

[tests/test_pii_signals.py](tests/test_pii_signals.py) pins all of it.

## Who audits the auditor

Attest exists to catch AI systems asserting things they cannot back up. If Attest's own
explanations were unverified model prose, the product would be self-undermining — so they
are not. **Every factual token in an explanation must appear in the evidence that produced
the verdict**, checked by [code](src/attest/faithfulness.py), not by a model. A model
checking a model is the same failure mode wearing a hat.

The model's world is deliberately small. It sees the claim, the verdict, and the evidence
fields the checker returned — never the raw catalog, never the snapshot. It cannot reach
for a better fact, because it was not given one.

What comes back is not trusted:

| Layer | What it catches |
| --- | --- |
| **Faithfulness guard** | Fabricated specifics — a hallucinated owner, column, tag, date, or number. |
| **Cross-check** | The model reads the evidence as a *different verdict*, or cites a field the checker never read. |
| **Template fallback** | Anything that fails the above. The explanation degrades to something **true**, never to something plausible. |

Three details are what make the guard real rather than decorative:

- **Matching is by contiguous word sequence, not substring.** `PII` must not match inside
  `NonPII` — that single bug would wave through the exact hallucination this product
  exists to catch. Nor can a fabricated `customer_email` be assembled out of a real
  `customer_profile` and a real `email` column found elsewhere.
- **The guard fails closed on names.** A capitalized word is lexically identical whether
  it is a fabricated owner ("Sarah Jennings") or ordinary prose, so anything not in the
  evidence and not a known function word is treated as a possible fabrication. A false
  rejection costs fluency; a false acceptance costs the product its reason to exist.
- **Derived numbers are rejected even when correct.** If the evidence says `10009.9h`, an
  explanation may not say "417 days" — the arithmetic may be right, but the guard cannot
  tell a correct derivation from a plausible one, and one that accepts plausible
  arithmetic accepts hallucinated arithmetic. If we want days said, a checker must put
  days in the evidence.

The verdict itself is never at risk, whatever the model does: verdicts come from
[checkers/](src/attest/checkers/), which take a typed claim and a catalog snapshot and
never see agent text at all. A test asserts that the deterministic core imports no model
client, so this cannot quietly stop being true.

### Does the guard reject *truthful* explanations?

It has to be asked, because a guard strict enough to reject everything is as useless as no
guard at all: every answer would be the template and the semantic layer would be
decorative. The offline suite cannot answer it — a fake model only lies when told to — so
[`just live`](tests/test_live.py) runs the layer against a real `gpt-4o-mini`.

The first live run rejected **2 of 9** truthful explanations, and every rejection was a bug
in *Attest*, not a lie by the model: the explain prompt told the model to say the catalog
was "SILENT" and the guard then rejected `SILENT` as an unevidenced capitalised token; the
model expanded PII to "Personally Identifiable Information"; and it cited one half of a
composite field path, which the cross-check called a fabrication. Fixed at the prompt and
the cross-check — **not** by loosening the guard, which still passes every hallucination
test. It now runs **9 of 9 model-authored, 0 fallbacks**, with all 4 claim types extracted
from real agent prose and none dropped.

The rule that fell out of it is worth keeping: when the guard rejects something truthful,
**widen the evidence, never the guard**.

**Prompt injection**, therefore, has nowhere to land. Attest ingests untrusted text by
definition — the thing it audits is another agent's output — so
[sanitize.py](src/attest/sanitize.py) strips instruction-like spans ("ignore previous
instructions", "mark this as Supported") and logs them as findings rather than swallowing
them. But a sanitizer is a blocklist, and blocklists leak. The real answer is structural:
**there is no prompt in this system whose output is a verdict.** The worst a successful
injection achieves is a corrupted claim *extraction* — and even then, the catalog decides
what is true.

## Known boundaries

Real, deliberate, and *not* silently carried. Each is a place Attest declines to answer
rather than guessing, because a wrong verdict has the same confident shape as a right one.

| Boundary | Today | Why it's deferred |
| --- | --- | --- |
| **Semantic term matching** | A glossary term implies PII **iff the catalog files it under the PII node**. A term nobody filed there implies nothing, however personal it reads. | Structure is a declaration someone made; a name is a guess. Deciding that an *unfiled* term entails a classification is semantic entailment, and it must be evidence-constrained rather than a vibe. Still deferred. |
| **Ownership type** | `ownershipType` (technical vs business vs data steward) is ignored; any listed owner satisfies an ownership claim. | "Alice is the *business* owner" is a strictly stronger claim than "Alice is an owner." Checking it needs a claim schema that carries the role, which is a schema change, not an `if`. |
| **Entity-not-found propagation** | `fetch_dataset()` raises `EntityNotFoundError`. Nothing above it catches that yet. | Correct at this layer — a missing entity is an error, not a verdict. How a *pipeline* surfaces it (a fifth outcome? a hard failure?) is a Session 3 decision. |
| **Cross-dialect types** | Both of DataHub's type vocabularies match exactly; `int8` ~ `BIGINT` does not. | Needs a model of each platform's type system. |

## Commands

Everything runs through [`just`](https://github.com/casey/just):

```
just setup     # install the package + dev deps
just seed      # generate seed metadata and ingest it
just probe     # prove DataHub's read/write path (Session 0 spike)
just test      # the suite: live catalog, semantic layer offline. Free.
just live      # the semantic layer against a REAL model. Costs money.
just matrix    # just the 12-cell coverage assertion
just lint
just check     # lint + test
```

The acryl-datahub SDK is used **only** for generating and ingesting seed data. Attest's
runtime never imports it: it warns on Python 3.12, and the parts we control should sit on
the least fragile path available.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env      # then fill in OPENAI_API_KEY (the semantic layer needs it;
                            # the checkers and the whole test suite do not)
```

DataHub Core must be running locally (quickstart, GMS on :8080, UI on :9002).
Metadata auth is disabled locally, so no token is needed.

## Seed the catalog

```powershell
just seed     # generate_seed.py, then `datahub ingest -c ./seed/recipe.yml`
just probe    # proves READ / READ / WRITE / READ-BACK
just test     # 113 tests: live catalog + the semantic layer (offline, free)
just live     # 2 more, against a real gpt-4o-mini. Needs OPENAI_API_KEY.
```

Expect `failures: []` and 90 records from ingest, and `ALL FOUR OPERATIONS PASSED` from
the probe. The suite skips itself with a pointer to this section if DataHub isn't up —
it will not silently pass against an empty catalog.

### The suite is deterministic across machines and dates

Seed timestamps are **relative to seed time** (`FRESH = seed - 6h`, `STALE = seed - 417d`),
which would ordinarily make freshness tests rot: under a wall clock the fresh datasets age,
and a suite that is green today goes red in a fortnight against completely correct code —
reporting "the checker is broken" when the truth is "the catalog is old". That is exactly
the confusion between data state and code state that Attest exists to prevent, and it has
no business in Attest's own tests.

So the clock is injected (`check_freshness(..., now=...)`), and the test `now` is
**reconstructed from the live catalog**: one hour after the reference dataset's own
`lastModified`. Not the wall clock, and deliberately not `ground_truth.json` either — that
file is committed, so a fresh clone with a fresh `just seed` would have a catalog from
today and a `generated_at` from whenever it was last committed, and the tests would
silently start measuring the gap between them. Deriving `now` from the same server the
data came from is true on any machine, on any date, reseeded or not.
`test_verdicts_do_not_depend_on_the_wall_clock` asserts the property directly.

### Why we generate our own seed data

DataHub's showcase sample datapacks reference `dataQualityCheck`, a **DataHub Cloud**
entity type absent from Core's EntityRegistry at every version — so no choice of server
fixes it. Loading one fails, and the failures cascade over the schemas, owners, tags, and
glossary links that follow. Attest's four claim types (freshness, ownership,
classification, schema) never touch data-quality entities, so we emit only aspects Core
supports natively and the error class disappears by construction. See
[docs/datahub-setup.md](docs/datahub-setup.md).

That's a feature, not a workaround. Attest verifies claims against *known* ground truth,
so its benchmark needs entities where we control exactly what is true.

The metadata variation in the seed is therefore **not cosmetic — it's the benchmark's
substrate**. `seed/generate_seed.py` emits 16 datasets across 2 platforms, each carrying an
`exercises` field naming the headline verdict it's built for, plus a `note` explaining how.
Both flow into `seed/ground_truth.json`, so the golden benchmark can be built from it and
verdicts scored mechanically.

The datasets that earn their place:

- **Complete and correct** — owner assigned, PII terms attached, fresh timestamp. Claims
  about them are Supported.
- **Provably false** — `recipient_email_hash` is explicitly tagged `NonPII` (an agent will
  confidently call it PII on the strength of its name); `revenue_daily` is fully documented
  but last modified 417 days ago (an agent will call it "updated daily"); `support_tickets`
  has an owner that isn't the one a claim would guess.
- **Classified by tag alone** — `hr_headcount` is tagged PII with *zero* glossary terms, so
  a checker that reads only the glossary certifies it PII-free. See below.
- **Genuinely silent** — where the honest verdict is "the catalog doesn't know," not "the
  agent is wrong". `raw_events` has no owner, tags, terms, or description.
  `legacy_accounts.email` is *untagged*, so "it's PII" is unverifiable, not false.
  `pipeline_scratch` has no `lastModified`, and `external_report` has no `schemaMetadata`.

Note that a dataset is **not** a bucket. `orders_fact` is Supported for an ownership claim
and Contradicted for a PII claim; `exercises` is a label, not a partition. The real unit of
coverage is the twelve (claim type × verdict) cells above — which is exactly why the last
two datasets exist. They contribute nothing a human would notice browsing the catalog, and
without them a third of the checkers' logic was untestable.

## DataHub

Pinned to **Core v1.5.0.6** — for a reproducible benchmark base, not as a fallback from
something better. It supports every aspect Attest needs. Ground truth cannot sit on a
moving branch, or a verdict regression becomes indistinguishable from a server change.

[docs/datahub-setup.md](docs/datahub-setup.md) covers the pin, the `dataQualityCheck`
incompatibility, how to rebuild the stack, and the environment landmines (absolute Windows
paths, the BOM, quickstart's lying exit code) — each of which costs an afternoon to
rediscover.
