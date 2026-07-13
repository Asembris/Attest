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

Session 0 (spike) complete. The DataHub read/write path is proven end to end.
Nothing downstream — claim schema, LangGraph pipeline, LLM calls — is built yet.

## Layout

```
src/attest/          Attest's own code. Talks to DataHub over raw GraphQL (httpx).
  config.py          Per-step model config. Never hardcode a model.
  datahub/client.py  GraphQL client: read datasets, read/write structured properties, search.
seed/                Seed catalog generator + ingestion recipe.
spikes/              Throwaway proofs. datahub_probe.py proves the read/write path.
```

The acryl-datahub SDK is used **only** for generating and ingesting seed data. Attest's
runtime never imports it: it warns on Python 3.12, and the parts we control should sit on
the least fragile path available.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e .
copy .env.example .env      # then fill in OPENAI_API_KEY
```

DataHub Core must be running locally (quickstart, GMS on :8080, UI on :9002).
Metadata auth is disabled locally, so no token is needed.

## Seed the catalog

```powershell
python seed\generate_seed.py           # -> seed/seed_metadata.json, recipe.yml, ground_truth.json
datahub ingest -c ./seed/recipe.yml    # run from the repo root
python spikes\datahub_probe.py         # proves READ / READ / WRITE / READ-BACK
```

Expect `failures: []` and `total_records_written: 64` from ingest, and
`ALL FOUR OPERATIONS PASSED` from the probe.

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
substrate**. `seed/generate_seed.py` emits 10 datasets across 2 platforms, each with a real
column schema, and each carrying an `exercises` field naming the verdict bucket it's
designed to land in plus a `note` explaining how:

- **5 Supported** — complete, correct metadata: owner assigned, PII terms attached, fresh timestamp.
- **3 Contradicted** — a plausible agent claim is *provably false*: `recipient_email_hash` is
  explicitly tagged `NonPII` (an agent will confidently call it PII); `revenue_daily` is fully
  documented but last modified 417 days ago (an agent will call it "updated daily");
  `support_tickets` has an owner that isn't the one a claim would guess.
- **2 Insufficient-Coverage** — genuine gaps, where the honest verdict is "the catalog doesn't
  know," not "the agent is wrong": `raw_events` has no owner, tags, terms, or description, and
  `legacy_accounts.email` is *untagged* — so "it's PII" is unverifiable, not false.

That last distinction is the one Attest must never blur, so the seed forces it.
`seed/ground_truth.json` records what was asserted — bucket, note, and all — so the golden
benchmark can be built from it and verdicts scored mechanically.

## DataHub

Pinned to **Core v1.5.0.6** — for a reproducible benchmark base, not as a fallback from
something better. It supports every aspect Attest needs. Ground truth cannot sit on a
moving branch, or a verdict regression becomes indistinguishable from a server change.

[docs/datahub-setup.md](docs/datahub-setup.md) covers the pin, the `dataQualityCheck`
incompatibility, how to rebuild the stack, and the environment landmines (absolute Windows
paths, the BOM, quickstart's lying exit code) — each of which costs an afternoon to
rediscover.
