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

**Session 1 complete: the deterministic core.** Claim schema, DataHub client, and
four checkers — freshness, ownership, classification, schema — with 54 tests against
the live seeded catalog. There is no LLM anywhere in this layer, by design: the
deterministic half is what has to be bulletproof, so it was built and tested in
isolation before any model touches it.

Session 2 adds the semantic layer (claim extraction, explanation) on top. It does not
get to change a verdict — it gets to *phrase* one, constrained to the evidence a
checker returned.

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
  checkers/            The deterministic core. One checker per claim type. No LLM.
    policy.py          Declared governance semantics — where the model boundary is drawn.
  datahub/
    client.py          GraphQL client: datasets, structured properties, search.
    snapshot.py        Normalized read model. Preserves "absent" vs "empty".
seed/                  Seed catalog generator + ingestion recipe.
spikes/                Throwaway proofs. datahub_probe.py proves the read/write path.
tests/                 Live-catalog pytest suite.
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
   (`int8` ~ `BIGINT`) needs a model and is a Session 2 escalation, not an `if`.

The corollary: **"PII-free" is not the mirror image of "contains PII."** An untagged
table cannot *support* a PII-free claim — nobody has looked. A naive checker returns
Supported there and certifies an unreviewed table as clean, which is a groundedness
auditor manufacturing false assurance. It is Insufficient-Coverage.

## Commands

Everything runs through [`just`](https://github.com/casey/just):

```
just setup     # install the package + dev deps
just seed      # generate seed metadata and ingest it
just probe     # prove DataHub's read/write path (Session 0 spike)
just test      # the suite, against the live catalog
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
copy .env.example .env      # then fill in OPENAI_API_KEY (unused until Session 2)
```

DataHub Core must be running locally (quickstart, GMS on :8080, UI on :9002).
Metadata auth is disabled locally, so no token is needed.

## Seed the catalog

```powershell
just seed     # generate_seed.py, then `datahub ingest -c ./seed/recipe.yml`
just probe    # proves READ / READ / WRITE / READ-BACK
just test     # 54 tests against the live catalog
```

Expect `failures: []` and 72 records from ingest, and `ALL FOUR OPERATIONS PASSED` from
the probe. The suite skips itself with a pointer to this section if DataHub isn't up —
it will not silently pass against an empty catalog.

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
substrate**. `seed/generate_seed.py` emits 12 datasets across 2 platforms, each carrying an
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
