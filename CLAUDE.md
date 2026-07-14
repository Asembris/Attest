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

- **Python 3.12**, LangGraph (FastAPI is a later session; nothing depends on it yet).
- **OpenAI API only. Default `gpt-4o-mini`, and the default does not move.**

  There may be a key for another provider (Groq, etc.) sitting in `.env`. **Ignore it. Do
  not swap the default to it, and do not "optimize costs" by doing so.** Two reasons, and
  the first is the serious one:

  1. **Every measured number in this project was measured against `gpt-4o-mini`** — the
     $0.000264/claim receipt, the 2-in-6 rate at which the model attempts the column-swap
     escape, the 13/13 model-authored explanation rate, the whole cost projection. Swapping
     the model invalidates all of them *at once*, and they are the project's evidence. A
     benchmark whose ground truth moves with its provider is not a benchmark.
  2. It would solve a cost problem that **does not exist**: ~$9/mo per org (see cost.py).
     Trading away every receipt to save nine dollars is not a trade.

  The per-step model config (`settings.model_for`) stays — that is what lets ONE step move
  to a stronger model deliberately. It is not an invitation to move the default.

  **The one legitimate future use of a second provider** is cross-model-family calibration
  of the benchmark labels, so that GPT is not grading GPT's homework. That is a Session 6
  concern, it needs a different model *family* (not merely a different provider serving
  similar weights), and it is a validation exercise — never a cost swap.
- **The model is always a per-step config value, never hardcoded.** Every LLM step resolves
  its model through `settings.model_for(step)` ([src/attest/config.py](src/attest/config.py)),
  which falls back to `model_default`. This exists so one step — most likely semantic
  entailment — can be moved to a stronger model without dragging the cheap steps up with it.
  Steps are `claim_extraction` (decompose), `evidence_selection` (unused so far),
  `entailment` (revise), `verdict` (explain).

## Architecture principles

**1. The deterministic core is sacred.** Freshness, ownership, classification, and schema
verdicts come from code — date math, set membership, string comparison. No LLM decides a
verdict. `checkers/` has one checker per claim type and imports no model client.

**2. The LLM layer sits on top; it never replaces the core.** It does claim decomposition and
explanation generation only, and it is constrained to the evidence the deterministic checker
returned. It gets to *phrase* a verdict, not choose one.

Built, and the invariants worth not rediscovering:
- [llm.py](src/attest/llm.py) is the **only** module that calls a model. Strict `json_schema`,
  `temperature=0`, retry-on-malformed (2 attempts, error handed back). The client is injectable,
  so the whole semantic layer tests offline against a scripted fake — the suite spends no tokens.
- [decompose.py](src/attest/decompose.py): a claim's `target_urn` **must appear verbatim in the
  source text**. The model may quote a URN, never mint one. A hallucinated URN would otherwise
  reach a checker and come back Insufficient-Coverage — an invented entity reported as an
  under-documented one.
- [faithfulness.py](src/attest/faithfulness.py): every factual token in an explanation must
  appear in the evidence. Matching is by **contiguous word sequence** (so `PII` does not match
  inside `NonPII`, and `customer_email` cannot be assembled from `customer_profile` + `email`).
  Capitalized words are a factual class and the guard **fails closed**. Derived numbers are
  rejected even when correct.
- [explain.py](src/attest/explain.py): rejection falls back to a **deterministic template** built
  from the checker's reason and evidence. A failed explanation degrades to something *true*,
  never to something plausible. The template must itself pass the guard — keep its scaffolding
  lowercase, or it trips the capitalized-word rule.
- [crosscheck.py](src/attest/crosscheck.py): the model reports the verdict it reads and the
  fields it cited. Disagreement never changes the verdict; it is surfaced as a `Conflict`.

**2b. The pipeline (Session 3) audits itself, and that is not decoration.**
[graph.py](src/attest/graph.py) is a LangGraph state machine: sanitize → decompose → per
claim (resolve → route → check → explain → guard → maybe correct) → human checkpoint →
report. The invariants worth not rediscovering:

- **Every step records a `kind`** — `deterministic` / `llm` / `io` — plus latency and token
  spend ([observe.py](src/attest/observe.py)). The kind is what a node CLAIMS about itself;
  the token count is the EVIDENCE that checks it. This is why checker nodes are handed the
  `llm` handle even though they must never call a model: passing it *arms the trap*. A step
  that cannot bill tokens cannot **detect** them.
- [trajectory.py](src/attest/trajectory.py): seven named invariants asserted against the
  run's own trace. The load-bearing one is `NO_LLM_IN_THE_VERDICT_PATH` — a verdict step
  that spent any tokens, or a model call between `resolve` and the checker, fails the run.
  This turns "the deterministic core is sacred" from a README claim into a property of the
  run. **The expected path is declared in trajectory.py, NOT read off the graph's edges** —
  derive it from the graph and it agrees by construction and asserts nothing.
- **Every rule has a test that BREAKS it**, and four of them sabotage the *real* pipeline
  (guard torn out, checker spending tokens, correction proposed unverified, router
  miswired). Every other test stays green through all four — that is the point. A
  trajectory check that only ever passes is a green light wired to nothing.
- [revise.py](src/attest/revise.py) — **a revision may change what a claim ASSERTS; it may
  never change what the claim is ABOUT.** Enforced by comparison after the fact, never by
  prompting, at three grains: the `target_urn`, the `claim_type`, and `revise.subject()`
  (labels + column grain for classification; column NAMES for schema). Correct a column's
  type, never swap the column; flip `present`, never swap the label. Every such swap
  re-verifies **green** while leaving the false claim uncorrected and replaced by an
  unrelated true one — the retarget attack, one grain down. The URN rule alone does NOT
  close it, which is why `subject()` exists. The revision is re-checked by the same checker
  against the **same snapshot** (not a re-fetch: the agent is held to the facts it was
  *shown*, and a re-fetch would let the catalog move underneath the loop).
- **`STOOD_FIRM` is live-reachable, and the subject rule is what makes it so.** A schema
  claim naming a column that does not exist is Contradicted and genuinely *unrevisable* —
  `SchemaClaim` has no `present=False`, so "it does not have an ssn column" cannot be said,
  and naming a column that does exist is forbidden. The only honest move is to stand by the
  claim and be marked wrong. This is the 12-cell coverage argument applied to the correction
  loop: a state no data can produce exists in the enum and never in the world, and every
  test still passes. If the subject rule is loosened, that outcome goes theoretical again.
- **MEASURED, and the reason the subject rule exists.** Six live runs of that claim against
  gpt-4o-mini at temperature=0: **4 stood firm, 2 tried to swap `ssn` for the table's entire
  real column list.** Without `subject()` those two re-verify Supported, become a CORRECTED
  proposal, and hand a human a green correction for a claim that was simply false — a third
  of the time. The guard is not hypothetical and neither is the model's attempt to route
  around it.
- **Do NOT assert a specific correction outcome in a live test.** The model chooses between
  `stood-firm` and `refused` here and both are honest; asserting `stood-firm` alone flaked
  1-in-3. The live test asserts the property that holds every time — *an unrevisable claim
  produces no proposal* — and the specific outcome is pinned offline against the fake. A
  flaky assertion on a load-bearing invariant is worse than none: it teaches people to
  re-run the suite until it goes green, which is how a real regression gets waved through.
- **The retry cap (2) is a graph EDGE, not a counter.** An unbounded correction loop
  against a paid API is a cost bug that ships silently.
- **A correction is PROPOSED, never applied.** `ReviewStatus.PENDING` is the resting state;
  an unattended run accepts nothing. This is an accountability choice, not a limitation —
  an auditor that silently rewrites what it audits has stopped being an auditor, and the
  correction would be the one fact in the system with no independent source.
- **`CorrectionOutcome` names six outcomes, not a boolean.** `stood-firm` (the evidence
  does not say what the truth is) is not `refused` is not `exhausted`. A success flag would
  hide the loop's own failure modes, which is the failure mode this project is about.
- **Insufficient-Coverage is NEVER sent round the correction loop.** The catalog being
  silent is not the agent being wrong.

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

**When signals disagree, precedence resolves it — and it is about GRAIN, not about `NonPII`.**

(A) **Signals propagate up, never down**, because a table-level PII claim is *existential*
("contains PII" = PII is somewhere in it). **Up:** a column tagged PII settles a table-scoped
claim regardless of table metadata — without this, a `Verified` table with no table-level PII tag
returned **Supported** for "this table is PII-free" while its own `actor_email` column was tagged
PII, which is the worst verdict the product can produce. **Down:** a table's PII tag says nothing
about its untagged `signup_ts` column; inheriting it downward would flag every column of every
PII table. See `policy.resolve_pii_at_table`.

(B) Within a grain, an explicit tag beats an implied signal — a human's classification act
outranks a term's subject matter or a scanner's guess. An explicit PII tag *on a column* also
outranks a `NonPII` tag on its table: the more specific act is the better evidence.

The losing signal is still returned as evidence, so an explanation can say why the conflict
resolved as it did. Two worked examples, and they are mirror images: `email_campaign_stats` is
filed under `EmailAddress` while its `recipient_email_hash` column is tagged `NonPII` (column
wins → not PII); `audit_log` carries no table-level PII signal at all while its `actor_email`
column is tagged PII (column wins → PII, at both grains).

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
- **NEVER put a typed object in LangGraph's checkpointed state. Do not "clean this up".**
  `AuditState` holds **primitives only** (cursor, retry count, decisions) and the typed
  audit record lives in a `_Ledger` keyed by thread id, *beside* the graph. This looks like
  an odd split and it is deliberate. The checkpointer serializes through msgpack, and it
  *currently* round-trips unregistered classes **with a deprecation warning**:

      Deserializing unregistered type attest.claims.FreshnessClaim from checkpoint.
      This will be blocked in a future version.

  Pydantic claims and frozen dataclasses therefore **survive today**, which is exactly what
  makes this a trap: put them in state, run the tests, and everything is green. When it
  breaks, typed objects come back as **bare dicts after a resume** — so it fails only on the
  human-checkpoint path, only after an interrupt, as an `AttributeError` deep in report
  assembly. That is close to the hardest possible thing to diagnose, and it will happen in a
  demo. The pause is no less real for the split: `interrupt_before` is a genuine interrupt.
  If a future session finds this ugly and moves the ledger into the graph state, it will
  work perfectly until the day it doesn't.

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
  llm.py               The only module that calls a model.
  decompose.py         Agent prose -> typed claims. A URN must be quoted, never minted.
  explain.py           Verdict + evidence -> prose. Falls back to a deterministic template.
  faithfulness.py      The guard. Every factual token must appear in the evidence.
  crosscheck.py        Model/checker disagreement -> a Conflict, never a changed verdict.
  sanitize.py          Untrusted agent text in, instruction-like spans stripped out.
  graph.py             The LangGraph pipeline. Routing, the loop, the human checkpoint.
  revise.py            Self-correction. A revision may not change the subject.
  trajectory.py        Seven invariants asserted against the run's own trace.
  observe.py           Step trace: kind, latency, tokens. What trajectory.py reads.
  cost.py              Prices a run. An unpriced model costs None, never 0.
  report.py            AuditReport: verdicts, proposed corrections, receipts.
seed/                  Seed catalog generator + ingestion recipe (ground_truth.json).
spikes/                Throwaway proofs. datahub_probe.py proves the read/write path.
tests/                 Live-catalog pytest suite. Skips (does not pass) if DataHub is down.
                       test_graph/test_trajectory run fully offline: control flow is not a
                       statement about DataHub's wire format.
```

## Commands

```
just setup     # install package + dev deps
just seed      # generate seed metadata and ingest it
just probe     # prove DataHub's read/write path
just health    # is the pinned version actually running?
just test      # the suite: live catalog, semantic layer offline. Free.
just matrix    # just the 12-cell coverage assertion
just check     # lint + test — what CI runs
just live      # the semantic layer against a REAL model. Costs money.
just preflight # lint + test + live — required before pushing semantic-layer changes
```

## Verification cadence — a rule, not a habit

**Run `just preflight` (lint + test + live) before any push that touches the semantic
layer.** That means any change to `llm.py`, `decompose.py`, `explain.py`,
`faithfulness.py`, `crosscheck.py`, `sanitize.py`, `revise.py`, **or any prompt string or
JSON schema in them**. `just check` is not enough for those files, and this is not a nicety:

- **`just check` (offline, free) proves the guard still catches hallucinations.** It runs
  against a scripted fake that lies on demand — `Sarah Jennings`, an invented `ssn` column,
  a derived `417 days`, `PII` matched inside `NonPII`.
- **`just live` (real `gpt-4o-mini`, costs a fraction of a cent) proves the guard still
  lets the truth through.** It runs all 12 matrix cells and fails if more than one
  explanation falls back to the template.

**Each half is blind to the other's failure mode.** A guard that rejects *everything*
passes `just check` with flying colours — every hallucination is caught — while the
semantic layer silently degrades to templates and nobody notices until a demo. That is
not hypothetical: the first live run rejected 2 of 9 *truthful* explanations, and the
offline suite was green throughout.

**When `just live` fails, widen the EVIDENCE, not the guard.** Every live failure so far
has been a bug in Attest's prompts or cross-check, never a lie by the model. If an
explanation needs a word, a checker must put that word in the evidence. Loosening the
guard to make a test pass is the one change that would quietly destroy the product's
reason to exist.

The generalized form, learned the hard way in Session 3: **if the model omits a field, it
is usually because you did not tell it what the field is for.** The first live run of the
correction loop returned `owner_urn=null` on an ownership revision, and the claim was
rejected as malformed. The model was not being evasive — `revise.SCHEMA` had been written
with the field descriptions stripped, so it had been handed a bare `["string", "null"]` and
told nothing about what belonged in it. **A JSON-schema field with no `description` is a
prompt bug**, and it surfaces far downstream as a failed correction rather than as anything
that looks like a prompt problem. Every field in `decompose.SCHEMA` and `revise.SCHEMA`
carries its description for this reason; do not "tidy" them away.

## Known deferred items — document, don't fix

| Item | Today | Why deferred |
| --- | --- | --- |
| **Semantic glossary-term matching** | A term implies PII iff it is *filed under the PII node*. A term nobody filed there implies nothing, however personal it reads. | Deciding that an unfiled term *entails* a classification is semantic entailment — the LLM layer's job, evidence-constrained. Structure is a declaration; a name is a guess. |
| **Ownership-type distinctions** | `ownershipType` (technical / business / steward) is ignored; any listed owner satisfies an ownership claim. | "Alice is the *business* owner" is a strictly stronger claim. Checking it needs the role in the claim schema — a schema change, not an `if`. |
| **Cross-dialect type equivalence** | Both DataHub type vocabularies match exactly; `int8` ~ `BIGINT` does not. | Needs a model of each platform's type system. |

**Entity-not-found is now DECIDED** (it was the pipeline's call to make, and Session 3 made
it). A claim about a dataset that does not exist surfaces as a `report.ClaimError`, kept
out of `audits` entirely and counted in no verdict tally. It is **not** a verdict: the
catalog neither disagrees with the claim nor is silent about it — the question was
malformed. Scoring it Insufficient-Coverage would launder a hallucinated URN into a
legitimate-looking audit result, and the bad URN would never be seen.

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
