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
not get to reach one.

**Session 3: the pipeline.** A LangGraph state machine wiring the two together, plus the
**self-correction loop** — a contradicted claim is handed back to the source agent with the
catalog's facts, and the revision is re-verified by the same deterministic checker. Plus
**trajectory verification**, which holds each run to its own architecture, and full
observability.

**Session 4: the service.** A FastAPI surface, a SQLite audit history, structured
**write-back to DataHub** on approval, and a **run-scoped snapshot cache** that turned out
to be a consistency fix rather than a performance one.

**Session 5: durable resume and per-run token billing.** A run parked at the human
checkpoint now survives the death of its process — and is resumed *through* the checkpoint
node, never around it. The service's lock is gone, because the shared token ledger it was
guarding is gone: each run forks its own model handle, so two concurrent audits cannot
bill each other.

**Session 6: the golden benchmark.** 40 hand-labeled claims, precision/recall/F1 per
verdict, a confusion matrix, pass@k, a vacuity check that fails CI if the metrics stop
measuring anything, and cross-family label calibration against a non-GPT model. 262
offline tests, 5 live.

Not built yet: the web UI, continuous monitoring, multi-tenancy, auth.

## Receipts

Every number here is **measured against `gpt-4o-mini`**, not estimated — from the clock, the
API's own token counts, and repeated live runs. `just live` reproduces all of them.

```
audited 3 claims in 14.3s (4392 tokens, $0.001006)
```

| Receipt | Measured | Where |
| --- | --- | --- |
| **Attest's verdict on the same claim, asked 5 times** | **5 / 5 identical** (pass@5 = 100%) | [benchmark](benchmark/README.md) |
| **An LLM judge's verdict on the same claim, asked 6 times** | **unstable — a different answer set every run** | [benchmark](benchmark/README.md#cross-family-calibration-not-letting-gpt-grade-gpts-homework) |
| Groundedness accuracy, 40 hand-labeled claims | **100%**, macro-F1 **1.000** | [`just bench`](benchmark/run_eval.py) |
| Cost per audited claim | **$0.000264** | [cost.py](src/attest/cost.py) |
| Explanations model-authored, not template fallback | **40 / 40** | [`just bench-full`](benchmark/run_eval.py) |
| **Self-correction guard fires against a real model** | **2 / 6 runs** | [below](#self-correction-and-why-it-cannot-be-gamed) |
| Catalog fetches, 4 claims over 2 datasets | **2** (not 4) | [cache.py](src/attest/datahub/cache.py) |
| Verdicts decided by a model | **0** — structurally, asserted per run, and now *measured* | [trajectory.py](src/attest/trajectory.py) |

### The first two rows are the whole argument

`NO_LLM_IN_THE_VERDICT_PATH` has always been an *architectural* claim, asserted by
[trajectory.py](src/attest/trajectory.py) on every run. Session 6 turned it into a **measured
one**, and by accident.

Attest's own benchmark harness employs a second model (Nemotron, a Llama-family model) as an
independent labeler, to keep GPT from grading GPT's homework. It was run **six times** —
identical code, identical prompt, `temperature=0` — and it disagreed with a **different set of
cases every single time**: `{class-15}`, then `{class-03, class-15}`, then `{class-03,
class-13}`, then `{class-03}`, then none, then none. Every case it ever disputed, it also
*agreed* with on another run. It twice returned unparseable JSON on a narrow,
schema-constrained question and needed a retry to recover.

Asked the same question, the same LLM gives different answers. **Attest's core, asked the same
question five times, gives the same answer five times.**

That is not a slogan and it is not a citation — it is two tables produced by the same harness,
in this repository, on the same catalog, on the same day. It is the difference between a
verdict you can *audit* and a verdict you can only *hope about*, and it is the entire reason
the deterministic core exists. A judge that cannot reproduce itself cannot be ground truth. It
can only calibrate ground truth — which is exactly, and only, what it is used for here.

And the row below it is worth pausing on too. Handed a **false claim it could not honestly
correct**, `gpt-4o-mini` tried to escape the finding — by swapping the fabricated column for
real ones — in **2 of 6 runs**. The subject rule caught it every time. It is not a guard
against a hypothetical adversary: it is a guard against **the model we actually ship**,
firing at a measured rate of roughly one run in three. Details
[below](#self-correction-and-why-it-cannot-be-gamed).

### What it costs to operate

A receipt for three claims answers *does this work*. It does not answer *what does this cost
to run continuously* — and that question is worth answering **before** monitoring is built,
not discovered afterwards. So the per-step token counts are measured and projected forward
([`cost.py`](src/attest/cost.py), pinned by [tests](tests/test_cost.py)):

| Per unit | Measured | |
| --- | --- | --- |
| One claim | 895 in / 216 out | **$0.000264** |
| One correction attempt | 1140 in / 125 out | **$0.000246** |

At **1000 claims/day, 50 datasets, one org** — `gpt-4o-mini`:

| Contradiction rate | Attempts each | $/day | $/month | $/year |
| --- | --- | --- | --- | --- |
| 5% | 1 | $0.28 | $8 | $101 |
| **10%** (nominal) | 1 | **$0.29** | **$9** | **$105** |
| 25% | 1.5 | $0.36 | $11 | $130 |
| 100% (every claim wrong, cap spent) | 2 | $0.76 | $23 | $276 |

**It is not alarming, and that is itself the finding.** Token cost is not the constraint on
continuous monitoring at one-org scale, so it is *not* the argument for sampling — and it
would have been easy to assume it was. Three things the numbers actually say:

1. **The cost bound is structural, not aspirational.** Corrections, not claims, are what move
   a bill: a revision hands the evidence back to the model, making it the priciest call in
   the pipeline, and it fires *only* on Contradicted claims — exactly the ones anyone cares
   about. The worst case is **2.86× the quiet case**, and the reason that is a *ceiling*
   rather than a hope is that **the retry cap is a graph edge, not a runtime check**. It is
   not a counter some code path can forget to increment, or an `if` an exception can skip
   past: `recheck` has exactly two outgoing edges, and the one back to `revise` is guarded
   by the cap. There is no path through the graph that revises a claim three times — so
   $0.76/day is not "what we expect to spend", it is **what the topology permits us to
   spend**. [`trajectory.py`](src/attest/trajectory.py) then asserts per run that the bound
   held, so even a rewiring that broke it would fail loudly rather than bill quietly. Raise
   the cap and the ceiling rises with it, deliberately and in one place. **The cap is the
   budget control.**
2. **The real scaling pressure is catalog reads, not tokens — and it is now fixed.**
   `resolve_entity` used to fetch once *per claim*: 1000 claims across 50 datasets was
   **1000 GraphQL fetches for 50 distinct datasets**, 20× redundant. A run-scoped snapshot
   cache makes that **50**. It turned out to be a correctness fix wearing a performance
   fix's clothes — see [below](#the-snapshot-cache-is-a-consistency-boundary).
3. **Per-tenant budget caps are a multi-tenancy concern, not a single-org one.** Cost is
   linear in claims/day: ~$9/mo per org means ~$900/mo at 100 orgs and ~$9k/mo at 1000. The
   cap matters *there*, and it should be enforced per tenant rather than globally.

## The service

```
POST /audit                    submit an agent's output; get verdicts, evidence, receipts
GET  /audit/{run_id}           retrieve a stored audit, whole
POST /audit/{run_id}/approve   the human checkpoint: settle the proposed corrections
GET  /health                   liveness — Attest's, and the catalog's, reported separately
```

`just serve`, then [localhost:8003/docs](http://localhost:8003/docs). The port is pinned
deliberately: DataHub already owns 8080 (GMS) and 9002 (UI) on the same machine, and a
collision surfaces as an audit that cannot reach the catalog — which reads like a DataHub
outage rather than a port clash.

**`POST /audit` changes nothing in the catalog. Ever.** It finds contradictions, asks the
agent to correct them, re-verifies the corrections against the catalog — and then *proposes*
them. There is no `?auto_approve=true` and no "approve all", and their absence is the
design: an HTTP surface is exactly where the accountability decision from Session 3 would
have been quietly traded away for a caller's convenience. An unattended script can audit all
day and write nothing. [A test asserts it.](tests/test_api.py)

### The snapshot cache is a consistency boundary

The cost model said the real load was catalog reads, so the obvious fix was a cache. The
obvious fix turned out to be the *less* important half.

Without a cache, two claims about the same dataset in the same audit are checked against two
**separate reads** of the catalog. If someone re-tags the table in between, Attest returns
"contains PII: **Supported**" and "PII-free: **Supported**" in the same report — each correct
against the catalog as it saw it, and together, nonsense. **A verification tool that cannot
say which state of the world it verified against has not verified anything.**

So a run resolves each entity exactly once, and every claim in that run is decided against
that one snapshot. The report now means something precise: *these verdicts hold against the
catalog as it stood when this run read it.* The speed is a side effect.

**Cross-run caching would be a liability, not an optimization.** A snapshot carried into the
next audit means verifying today's claim against a catalog that no longer exists — the exact
failure Attest was built to catch, committed by the tool that catches it. So: **cache within
an audit for consistency; always re-fetch for a new audit for correctness.** That is a design
decision, not a missing feature, and the scoping is *structural* — the cache is created inside
`run()`, lives on that run's ledger, and dies with it. There is no object for a second run to
reach.

(No Redis. It buys cross-process sharing, and nothing here needs it: the only reader of a
run's snapshots is the run itself. It would buy a deployment dependency and a stale-data
failure mode in exchange for nothing.)

| | Fetches | |
| --- | --- | --- |
| 4 claims, 2 datasets — **measured** | 4 → **2** | `just live` |
| 1000 claims/day, 50 datasets — projected | 1000 → **50** | [cost.py](src/attest/cost.py) |

### DataHub is the catalog, not the event store

On approval, the verdict is written back to DataHub as **typed structured properties** —
`attest.verdict`, `attest.claim_type`, `attest.checked_at`, `attest.source_agent`,
`attest.audit_run` — not as a text blob. The point is that *"show me every contradicted
ownership claim caught this week"* becomes a real DataHub query.

But DataHub's structured properties are **last-write-wins and unversioned**. Write a verdict
today and yesterday's is *gone* — not superseded, gone. That is exactly right for "what is
true of this dataset now", and useless for every question an auditor actually exists to
answer:

- has this agent's ownership accuracy improved since March?
- was this claim ever contradicted, before someone fixed the tag?
- who approved this correction, and what evidence were they shown?

Every one of those is a question about **events**, and a last-write-wins field has no events
in it. So the history — every run, every claim, every piece of evidence, every human decision
— lives in [Attest's own store](src/attest/store.py), the catalog carries the *latest*
verdict, and `attest.audit_run` is the key that joins them. Approvals are **append-only**:
re-deciding a claim writes a second row, because overwriting would reproduce, inside Attest's
own database, the very property that disqualified DataHub from holding the history.

**There is deliberately no `attest.confidence`.** The verdicts come from deterministic code —
date arithmetic, set membership, string comparison. There *is* no confidence, and the third
verdict already carries the only uncertainty in the system (about the catalog's coverage, not
about the answer). A `confidence: 0.95` would be a number invented to look like a machine
learning system, and a fabricated figure reported as a measurement is the precise thing this
project exists to catch.

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

## The golden benchmark

A groundedness auditor that cannot report its own accuracy is asking for the trust it
refuses to give. So: **40 hand-labeled claims** against the seeded catalog, 26 of them
marked *hard*, all 12 coverage cells populated, every label carrying a one-line rationale.
It ships as a standalone, citable artifact — [`benchmark/`](benchmark/README.md) — usable by
someone who has never seen this code.

| | Deterministic core | Full pipeline (prose in) |
| --- | --- | --- |
| Accuracy | **100%** (40/40) | **100%** (40/40) |
| Macro F1 | **1.000** | **1.000** |
| Correctness failures (Supported ↔ Contradicted) | 0 | 0 |
| Coverage failures (anything ↔ Insufficient) | 0 | 0 |
| pass@k | **100%** (k=5) | **100%** (k=3) |
| Cost | $0 | **$0.0138** / 40 claims |

**pass@k is a bug detector, not a score.** Verdicts come from date math and set membership,
so the same claim must produce the same verdict every time. A pass@k below 100% on the core
would mean a model had leaked into the verdict path — the one thing
[`trajectory.py`](src/attest/trajectory.py) exists to make impossible. The structural
assertion and the empirical measurement agree.

**The benchmark caught a real bug on its first run.** *"Updated every 30 minutes"* came back
from extraction as `max_age_hours: 0` — the model floored 0.5 — which the claim schema
correctly rejects (`gt=0`), so the claim was silently **dropped**. The fix was to widen the
schema's *description* (fractions are allowed), never to relax `gt=0`, which would have
admitted a meaningless "updated within zero hours" claim. Widen the evidence, never the
guard. 97.5% → 100%.

**And the benchmark can fail, which is the only reason to believe it.** `just bench-sabotage`
replaces the classification checker with one that affirms everything: accuracy drops 100% →
**67.5%**, Supported precision collapses to **0.536**, and 13 cases are named. It exits
non-zero if the numbers *don't* move.

**GPT does not grade GPT's homework.** The labels are cross-checked by **Nemotron, a
Llama-family model** — because LLM judges favor their own outputs (GPT-4 by ~10% win rate,
Claude-v1 by ~25%; [Zheng et al. 2023](https://arxiv.org/abs/2306.05685)), and validating
GPT-labeled ground truth with a GPT judge would inflate the number by construction. It never
touches the verdict path.

**Agreement: 95–97.5% across six runs — and no disagreement survives repetition.** Every case
Nemotron ever disputed, it also *agreed* with on another run, with identical code, an identical
prompt and `temperature=0`. So the residual 5% is **judge noise, not a finding about the
labels**, and any single run's dispute list is a fact about which sample got written to disk.

**That is the sharpest argument in this repository for why Attest's verdicts come from code.**
A judge that answers its own question differently at temperature=0 cannot be a source of ground
truth — it can only calibrate one. Attest's core, asked the same question five times, answers
identically five times: **pass@5, 100%**, in the table above. Measured here, not cited from a
paper.

**And the labeler earned its keep anyway: it found a bug in the governance policy.** An untagged
column on a `Verified` table is where two declared rules collide ("column over table" vs
"`Verified` licenses closed-world reasoning") — and the tie-break existed only as a *comment
inside a checker*, the exact thing [`policy.py`](src/attest/checkers/policy.py) exists to
prevent. Nemotron wasn't wrong; it was under-informed. Now declared as
`COMPLETENESS_REACHES_COLUMNS`. Finding a rule that lived in an `if` is worth more than the
percentage that surfaced it.

### Why not RAGAS or DeepEval?

Not used, and this is a decision rather than an omission.

**Both frameworks score an LLM-generated answer against retrieved context.** That is the
right tool for a RAG pipeline, where a model produces the answer and the question is how
faithful that answer is to what was retrieved. **Attest has no such answer to score.** Its
verdicts come from date math, set membership and string comparison — the model is permitted
to *phrase* a verdict and never to reach one. There is no generation in the verdict path for
a groundedness metric to grade.

What Attest is, statistically, is a **deterministic three-class classifier**, and the correct
way to evaluate one of those is precision, recall and F1 against hand-labeled ground truth,
with a confusion matrix. That is `sklearn.metrics`, and it is what the harness uses.

Adopting DeepEval would mean one of two things, and both are bad. Either it would be used for
metrics it was not built for — or, worse, its presence would *imply that Attest's verdicts are
LLM-judged when they explicitly are not*. That would undercut
`NO_LLM_IN_THE_VERDICT_PATH`, which is the strongest engineering claim this system makes.
**A dependency that contradicts a core architectural principle is a liability, not
credibility.**

Worth noting on its own terms: DeepEval's author states that G-Eval is non-deterministic and
that a benchmark resting on it cannot be fully trusted. Their most deterministic offering, the
DAG metric, decomposes an evaluation into narrow binary LLM judgements — which is a genuinely
good instinct, and it is **strictly weaker than what is here**, because Attest's verdicts are
not narrow LLM judgements at all. They are code. The DAG philosophy was arrived at
independently and then gone past.

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
    cache.py           ONE RUN's view of the catalog. A consistency boundary, not a cache.
  --- the semantic layer: phrases verdicts, never decides them ---
  llm.py               The only place a model is called. Strict JSON, temperature=0.
  decompose.py         Agent prose -> typed claims. A URN must be quoted, never minted.
  explain.py           Verdict + evidence -> prose. Falls back to a deterministic template.
  faithfulness.py      The guard. Every factual token must appear in the evidence.
  crosscheck.py        Model disagrees with the checker -> surfaced, never obeyed.
  sanitize.py          Untrusted agent text in, instructions stripped out.
  --- the pipeline: wires the above into a graph, and audits itself doing it ---
  graph.py             The LangGraph state machine. Routing, the loop, the checkpoint.
  revise.py            Self-correction. A revision may not change the subject.
  trajectory.py        Seven invariants, asserted against the run's own trace.
  observe.py           Every step: kind, latency, tokens. The trace trajectory.py reads.
  cost.py              Prices a run. An unpriced model costs None, never 0.
  report.py            Verdicts, proposed corrections, receipts.
  --- the service: an HTTP surface, a history, and a way back into the catalog ---
  api/                 FastAPI. Four endpoints. The checkpoint does not soften here.
  record.py            The persisted projection of a report. Keeps the evidence.
  store.py             The audit history. SQLite, plain SQL, Postgres-shaped.
  writeback.py         Approved verdict -> DataHub structured properties. Queryable.
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

## The pipeline

```
sanitize → decompose → ┌ per claim ────────────────────────────────────────┐
                       │ resolve → route → check → explain → guard          │
                       │                    ↑ deterministic   ↓ Contradicted │
                       │                    └── recheck ← revise ────────────┤ ×2 max
                       └───────────────────────────────────────────────────┘
                                                     ↓
                                       human checkpoint → report
```

### Why a graph and not a for-loop

A fair question, and it deserves a real answer rather than a buzzword — a `for` loop would
run these steps in this order. Four things the graph buys, each a property a loop would
have to be *trusted* to maintain:

1. **The retry cap is an edge, not a counter.** Self-correction is a genuine cycle
   (`revise → recheck → revise`), and the only way out is a conditional edge that reads the
   retry count. A `while` with a `break` is one careless edit from unbounded, and an
   unbounded correction loop against a paid API is a cost bug that ships silently.
2. **The human checkpoint is a real pause.** `interrupt_before` parks the run mid-graph
   with its state intact. It does not *return a flag saying someone should look at this* —
   it **stops**, and cannot proceed until a person resumes it.
3. **Routing by claim type is topological, not an `if`.** Each claim type has its own
   checker node. Because the trace records which node ran, a misrouted claim is a
   *catchable fact*. In a for-loop the dispatch agrees with itself by construction and
   cannot be audited from outside.
4. **The trajectory is a record, not a story.** Every node records its kind
   (`deterministic` / `llm` / `io`), latency, and token spend — which is what lets Attest
   *prove* its central claim instead of asserting it.

### Self-correction, and why it cannot be gamed

When a claim is Contradicted, Attest hands it back to the source agent along with what the
catalog actually says, and lets it restate itself. The revision is then **re-verified by the
same deterministic checker against the same snapshot** — so the outcome of a correction is
decided by code, exactly as the original verdict was.

One rule makes this an audit rather than a negotiation, and it is enforced by comparison
after the fact, never by asking the model nicely:

> **A revision may change what a claim ASSERTS. It may never change what the claim is ABOUT.**

The subject is frozen; the value is free:

| Claim type | Subject — frozen | Value — revisable |
| --- | --- | --- |
| freshness | *(the dataset)* | `max_age_hours` — widen the window |
| ownership | *(the dataset)* | `owner_urn` — name the real owner |
| classification | the `labels`, the column | `present` — flip the polarity |
| schema | the column **names** | the column **types** |

Correct a column's type; never swap the column. Flip `present`; never swap the label. Every
one of those swaps would re-verify **green** while leaving the false claim uncorrected —
replaced by an unrelated true one. That is the agent wriggling out from under the finding,
and it is closed at three grains: the target URN, the claim type, and the subject *within*
the claim. (The same snapshot is reused deliberately too: the agent is held to the facts it
was *shown*, so the catalog cannot move underneath the loop.)

**The rule is what makes some claims honestly unrevisable — and that is a feature.**
`customer_profile has an ssn column` is Contradicted and cannot be corrected: a `SchemaClaim`
has no `present=False`, so "it does *not* have an ssn column" is inexpressible, and naming a
column that *does* exist is forbidden by the rule above. The only honest move left is to
stand by the claim and be marked wrong.

**And this is not a hypothetical guard against a hypothetical model.** Six live runs of that
exact claim, `gpt-4o-mini` at temperature=0:

| Outcome | Runs | What the model did |
| --- | --- | --- |
| `stood-firm` | **4/6** | Set `unchanged=true`. The honest answer. |
| `refused` | **2/6** | Tried to swap `ssn` for the table's *entire real column list* — `customer_id, email, full_name, is_active, signup_ts`. |

Without `subject()`, those two runs each re-verify **Supported**, become a `CORRECTED`
proposal, and put a human in front of a green correction for a claim that was simply false.
A false claim laundered into an unrelated true one, **a third of the time**. The rule fires
in production.

Both outcomes are honest and neither proposes anything, so [the live
test](tests/test_live.py) asserts the property that held in all six runs — *Attest invents
no correction for an unrevisable claim* — rather than the specific outcome, which is the
model's to choose. Asserting `stood-firm` alone made it flake 1-in-3, and a flaky assertion
on a load-bearing invariant is worse than none: it trains people to re-run it. `stood-firm`
itself is pinned **offline and deterministically** in
[tests/test_graph.py](tests/test_graph.py).

(This is the 12-cell coverage argument applied to the correction loop: a state no data can
reach exists in the type and never in the world, and every test still passes. `stood-firm`
was theoretical until the subject rule made it reachable.)

The outcome is **named, not a boolean** — collapsing these would hide the interesting ones:

| Outcome | Meaning |
| --- | --- |
| `corrected` | Revised, re-verified Supported. Becomes a **proposal**. |
| `not-corrected` / `exhausted` | Revised, re-verified, still wrong. The cap (2) stopped it. |
| `stood-firm` | The agent declined: the evidence does not determine the truth. An honest non-answer — and **live-reachable**, see above. |
| `refused` | The revision changed the subject, or failed the claim schema. Rejected *before* verification. |
| `not-attempted` | The verdict was not Contradicted. Insufficient-Coverage is **never** dragged into the loop — the catalog being silent is not the agent being wrong. |

### The human checkpoint is an accountability choice, not a limitation

**This is a deliberate design decision, and it is not up for negotiation with the demo.**

A revision that re-verifies clean is **still not applied**. It becomes a *proposal*, the
graph parks, and it stays `PENDING` until a person accepts it. There is no "approve all"
default, and its absence is the design.

> **An auditor that silently rewrites what it audits has stopped being an auditor.**

That is the whole reason, and it has nothing to do with the loop being unreliable — the loop
is re-verified by deterministic code and works. It is that Attest's entire value is that a
human can point at any verdict and see the catalog field it came from. A correction Attest
applied to itself, on the strength of a model's revision, would be **the one fact in the
system with no independent source** — the single unauditable thing inside the auditor.

So the resting state of an unattended correction is *unreviewed*: never accepted, never
quietly written back. A run nobody looks at proposes changes to nobody. A tighter demo loop
is not worth the accountability story, and
[`test_an_unattended_proposal_stays_pending_rather_than_being_accepted`](tests/test_graph.py)
exists specifically to stop a hurried refactor from flipping the default to auto-accept —
which would look identical in every other test.

### Trajectory verification: the answer to "that's one prompt in a costume"

The sharpest cheap-shot at any agentic system is that the graph is decoration around a
single model call doing all the work. The answer has to be an **assertion**, not a log line.

So every step carries a *kind*, and [`trajectory.py`](src/attest/trajectory.py) holds each
run to seven named invariants. Checking that the nodes you called got called is trivially
true and proves nothing — a graph that ran every node in the right order and let a model
pick the verdict would sail through it. These are the properties that break if the
deterministic core is hollowed out:

| Rule | What it catches |
| --- | --- |
| **`no-llm-in-the-verdict-path`** | The big one. A verdict step that spent **any tokens**, or a model call smuggled in between resolving an entity and deciding on it. |
| `no-verdict-without-a-deterministic-check` | A verdict that no checker produced. |
| `no-explanation-without-the-guard` | Unverified prose reaching a reader. |
| `no-correction-without-re-verification` | A loop that *believed* the model's revision. |
| `no-claim-without-decomposition` | A claim minted mid-pipeline, never URN-checked. |
| `routing-matched-the-claim` | A freshness claim answered by the ownership checker. |
| `retry-cap-held` | The loop ran away. |

The first is the one that matters. A step's `kind` is the **claim it makes about itself**;
its token count is the **evidence that checks it** — so a checker that quietly started
calling a model fails this even if it returns the right answers, and even if every other
test stays green. That is Attest's own philosophy turned on Attest.

And the rules are proven to *fire*: [`tests/test_trajectory.py`](tests/test_trajectory.py)
**sabotages the real pipeline** four ways — the guard torn out, a checker that spends
tokens, a correction proposed without re-verification, a miswired router — and asserts each
run reports itself broken. Every other test in the suite stays green through all four,
which is the entire point. A trajectory check that only ever passes is a green light wired
to nothing.

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

The rule that fell out of it is the governing principle of this layer:

> **When the guard rejects something truthful, widen the evidence — never the guard.**

If an explanation needs a word, a checker must put that word in the evidence. Loosening
the guard to make a test go green is the one change that would quietly destroy the
product's reason to exist, and it is exactly the change a build under time pressure
reaches for.

The two halves of the suite are blind to each other's failure mode, which is why
[CLAUDE.md](CLAUDE.md) makes running both a rule rather than a habit: a guard that rejects
*everything* passes the offline suite perfectly — every hallucination caught — while every
explanation silently degrades to a template. `just check` proves the guard still catches
lies; `just live` proves it still lets the truth through. `just preflight` runs both, and
is required before any push that touches a prompt.

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
| **Cross-dialect types** | Both of DataHub's type vocabularies match exactly; `int8` ~ `BIGINT` does not. | Needs a model of each platform's type system. |
| **Step inputs/outputs across a restart** | A run's per-step log summaries (`cached: true`) are not persisted, so a *replayed* step carries them empty. **The boundary is asserted**, not just documented: a test strips them out of a real run and demands the record, the receipts, the summary and the trajectory verdict are all unmoved. | Nothing a reader sees may be computed from them. If something ever is, a resumed run starts reporting what an unrestarted one does not — silently, only after a restart, with every other test green. So it is nailed down rather than trusted, and two sabotages prove the assertion bites. |
| **Store migrations** | None. A pre-Session-5 database is refused at open, by name. **If you cloned this repo mid-development and it now fails at startup: `rm attest.db attest-checkpoints.db` and re-run.** Both are gitignored dev state; nothing in DataHub is touched. | Three columns went from a rendered string to the structure that produced it, and a string cannot be parsed back into the pair it came from. Inferring it would be Attest fabricating its own audit trail — the one thing it may not do. A real deployment needs a real migration; this is a hackathon build and says so. |

**Durable resume and concurrent audits were the Session 4 boundaries, and Session 5 closed
both — by removing a shared thing, not by guarding it.** A run parked at the human
checkpoint now survives the death of its process: the paused graph comes back from SQLite,
the typed ledger is rebuilt from the audit history, and **the resumed run goes through the
`human_checkpoint` node like any other** — which is the whole feature, because applying the
decision straight to the stored record would have been half the code and would have created
a second, unaudited path to the one thing in this system that must not have one. The bar was
not "it resumes" but *"it resumes and the report is identical to an unrestarted run's"*: a
restarted audit that quietly reports something different is invisible, and it is on the path
a human uses to approve a change to the catalog.

The sharpest thing that fell out of holding that bar: the step trace did not persist which
**models** a step called. `Trace.cost` reports a run's dollars as `None` — never `0` — when
a model that spent tokens has no price, and it identifies those models *by name* off the
step. Rebuild the trace without them, and a resumed run computes `usd = sum(...) = 0.0`
where the original honestly said *unknown*. **A restarted audit fabricating a cost figure the
original refused to state** is the None-is-not-zero rule breaking inside Attest's own
receipts — the exact failure this project exists to catch, committed by the thing that
catches it. It has a test.

And the lock is gone. It was never about throughput: one pipeline meant one LLM handle meant
one shared token ledger, and two concurrent runs would have billed each other. So the sharing
went, not the safety — each run forks its own handle. The test holds one audit's first model
call **open across the whole of another** and asserts each receipt bills only its own tokens:
with a shared handle, run A is billed **480 tokens for 240 tokens of work**. A concurrency fix
that silently cross-bills is worse than the queue it replaced.

**Entity-not-found is now decided** (it was Session 3's call to make). A claim about a
dataset that does not exist is **not a verdict**. The catalog neither disagrees with it nor
is silent about it — the *question was malformed*, most likely a bad URN from upstream
entity resolution. Scoring it Insufficient-Coverage would launder a hallucinated URN into a
legitimate-looking audit result and the bad URN would never be seen. So it surfaces as a
`ClaimError`, kept out of `audits` entirely and counted in no verdict tally.

## Commands

Everything runs through [`just`](https://github.com/casey/just):

```
just setup     # install the package + dev deps
just seed      # generate seed metadata and ingest it
just probe     # prove DataHub's read/write path (Session 0 spike)
just serve     # run the API on :8003. Docs at /docs.  (8080/9002 belong to DataHub)
just test      # the suite, across cores (-n auto). Live catalog, semantic layer offline. Free.
just live      # the semantic layer + one full pipeline run against a REAL model.
               # Costs money — about $0.001. Prints the receipts quoted above.
just matrix    # just the 12-cell coverage assertion
just resume    # durable resume + per-run token billing, on their own. Free.
just lint
just check     # lint + test — what CI runs
just preflight # lint + test + live. Required before pushing a prompt change.
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
