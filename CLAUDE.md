# CLAUDE.md

Project context for future sessions. Read this before touching anything — most of what
follows was expensive to learn and is not re-derivable from the code.

## What Attest is

A **groundedness auditor**. An AI agent makes claims about data ("the `customers` table is
owned by Alice and contains no PII"); Attest verifies each claim against DataHub's catalog
as ground truth and returns one of three verdicts:

| Verdict | Meaning |
| --- | --- |
| **Supported** | The catalog affirms the claim. |
| **Contradicted** | The catalog positively disagrees. |
| **Insufficient-Coverage** | The catalog is silent. Absence, not disagreement. |

Built solo for the DataHub Agent Hackathon.

## Stack

- **Python 3.12**, FastAPI + LangGraph.
- **OpenAI API only.** Default `gpt-4o-mini`.
- **The model is always a per-step config value, never hardcoded.** Every LLM step resolves
  its model through `settings.model_for(step)` ([src/attest/config.py](src/attest/config.py)),
  which falls back to `model_default`. This exists so one step — most likely semantic
  entailment — can be moved to a stronger model without dragging the cheap steps up with it.
  Steps are `claim_extraction`, `evidence_selection`, `entailment`, `verdict`.

## Architecture principles

**1. The deterministic core is sacred.** Freshness, ownership, classification, and schema
verdicts come from code — date math, set membership, string comparison. No LLM decides a
verdict. `checkers/` has one checker per claim type and imports no model client.

**2. The LLM layer sits on top; it never replaces the core.** It does claim decomposition and
explanation generation only, and it is constrained to the evidence the deterministic checker
returned. It gets to *phrase* a verdict, not choose one.

**3. Three verdicts, and the third is load-bearing.** Insufficient-Coverage ≠ Contradicted.
An agent is not wrong because the catalog is incomplete, and most real catalog entities are
incomplete. Collapsing the two would make Attest cry wolf on every under-documented entity —
the exact failure this project exists to prevent.

**4. Claims reference explicit DataHub URNs.** Free-text entity resolution ("the customer
table" → a URN) is deliberately **out of scope**, not an oversight. Keeping resolution
upstream means a resolution error can never be laundered into catalog disagreement.
`BaseClaim.target_urn` validates that it is a dataset URN.

**5. The 12-cell coverage matrix is the ground-truth design.** 4 claim types × 3 verdicts,
and all 12 must stay live-reachable against the seeded catalog.
[tests/test_coverage.py](tests/test_coverage.py) fails loudly if any cell goes dark
(`just matrix`). Two seed datasets exist *purely* to be silent in one specific way; without
them a third of the checkers' logic would be dead code against real data and every test would
still pass.

**6. PII signals are an explicit named set, not a magic string.** `PII_SIGNALS` in
[policy.py](src/attest/checkers/policy.py) names three, and **any one is sufficient** to
contradict a "PII-free" claim: the `PII` global tag (explicit), a glossary term filed under
the `PII` glossary node (implied), and a truthy `hasPII` custom property (implied). Each has a
witness dataset where it is the *only* signal — `hr_headcount` (tag), `marketing_leads` (term),
`device_telemetry` (property) — so none can be dropped without a test going red. `hasPII=false`
fires nothing in either direction: a scanner's miss is not a review.

Nothing is inferred from a name. `EmailAddress` is a PII signal because it is filed under the
PII node in the catalog's hierarchy; `CustomerIdentifier` is deliberately outside it.

**When signals disagree, precedence resolves it.** (A) Column over table — a table-level signal
never propagates into a column with its own classification, and a table tagged PII does not make
its `signup_ts` column PII. (B) Within a grain, an explicit tag beats an implied signal — a
human's classification act outranks a term's subject matter or a scanner's guess. The losing
signal is still returned as evidence, so an explanation can say why the conflict resolved as it
did. Worked example: `email_campaign_stats` is filed under `EmailAddress` while its
`recipient_email_hash` column is tagged `NonPII`; the column's tag wins.

Related, and easy to get wrong: **"PII-free" is not the mirror of "contains PII."** An untagged
table cannot *support* a PII-free claim — nobody has looked. That is Insufficient-Coverage.
Closed-world reasoning is never assumed by Attest; it is *granted by the catalog* per entity,
via a `Verified` completeness marker someone deliberately applied. All such governance semantics
live in [policy.py](src/attest/checkers/policy.py) as reviewable data rather than as an `if`
buried in a checker.

## Environment constraints — hard-won, do not rediscover

- **DataHub Core, Docker quickstart, pinned to v1.5.0.6.** Pinned *deliberately, for
  reproducibility* — not as a fallback from something better. `head` gives a moving RC
  (`v1.6.0rc1`) that drifts between runs, and a benchmark's ground truth cannot sit on a
  moving branch: a verdict regression becomes indistinguishable from a server change.
- **GMS `http://localhost:8080`, UI `http://localhost:9002`.** Metadata auth is disabled
  locally; no token needed.
- **Never emit `dataQualityCheck`, `anomalies`, or `dataContractProperties`.** These are
  DataHub **Cloud-only** entity types, absent from Core's EntityRegistry at every version.
  Emitting one doesn't just fail — it **crashes the emitter mid-file and silently drops
  everything after it**. This cost hours to diagnose. Do not fake them as custom properties
  either.
- **Never write YAML with PowerShell's `Out-File`.** It emits a UTF-8 BOM that breaks the YAML
  parser. Use `[IO.File]::WriteAllText` or Python.
- **Ingestion recipes must use relative `./` paths.** Absolute Windows paths hit a
  drive-letter parsing bug in the DataHub CLI — it reads `D:\...` as a URI scheme
  (`Did not find a registered class for d`).
- **Attest's own code talks to DataHub via direct GraphQL over `httpx`, not the
  `acryl-datahub` SDK.** The CLI/SDK is for *ingestion only*.

More landmines (quickstart's lying exit code, the eventually-consistent search index,
structured-property value shapes) are in [docs/datahub-setup.md](docs/datahub-setup.md).

## Layout

```
src/attest/
  claims.py            Claim / Verdict / Evidence schema (pydantic, frozen, extra=forbid).
  config.py            Per-step model config. Never hardcode a model.
  checkers/            The deterministic core. One checker per claim type. No LLM.
    policy.py          Declared governance semantics — the model boundary, as data.
  datahub/
    client.py          GraphQL client over httpx. Raises EntityNotFoundError.
    snapshot.py        Normalized read model. Preserves "absent" vs "empty".
seed/                  Seed catalog generator + ingestion recipe (ground_truth.json).
spikes/                Throwaway proofs. datahub_probe.py proves the read/write path.
tests/                 Live-catalog pytest suite. Skips (does not pass) if DataHub is down.
```

## Commands

```
just setup     # install package + dev deps
just seed      # generate seed metadata and ingest it
just probe     # prove DataHub's read/write path
just health    # is the pinned version actually running?
just test      # the suite, against the live catalog
just matrix    # just the 12-cell coverage assertion
just check     # lint + test
```

## Known deferred items — document, don't fix

| Item | Today | Why deferred |
| --- | --- | --- |
| **Semantic glossary-term matching** | A term implies PII iff it is *filed under the PII node*. A term nobody filed there implies nothing, however personal it reads. | Deciding that an unfiled term *entails* a classification is semantic entailment — the LLM layer's job, evidence-constrained. Structure is a declaration; a name is a guess. |
| **Ownership-type distinctions** | `ownershipType` (technical / business / steward) is ignored; any listed owner satisfies an ownership claim. | "Alice is the *business* owner" is a strictly stronger claim. Checking it needs the role in the claim schema — a schema change, not an `if`. |
| **Entity-not-found propagation** | `fetch_dataset()` raises `EntityNotFoundError`; nothing above it catches that yet. | Correct at this layer — a missing entity is an error, not a verdict. How the pipeline surfaces it is a later decision. |
| **Cross-dialect type equivalence** | Both DataHub type vocabularies match exactly; `int8` ~ `BIGINT` does not. | Needs a model of each platform's type system. |

## Commit convention — follow strictly

Conventional Commits with a scope, then tight bullets. **Use the accurate type** — don't force
everything into `feat:`.

- `feat(scope):` new capability
- `fix(scope):` bug fix
- `test(scope):` tests only
- `docs(scope):` documentation
- `refactor(scope):` no behavior change
- `chore(scope):` tooling, deps, config

```
feat(decompose): extract structured claims from agent output

- OpenAI function calling with strict JSON schema, temperature=0
- Retry-on-malformed-output, max 2 attempts
- Model is a per-step config value, not hardcoded
```

Bullets state **what changed, not why it's good**. No prose paragraphs, no emoji, no
"Generated with Claude Code" footer.
