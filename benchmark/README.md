# The Attest Golden Benchmark

**40 hand-labeled groundedness claims about a DataHub catalog, with the rationale for every
label.** [`cases.json`](cases.json) is the artifact, usable without reading a line of Attest's
source. This document is the methodology behind it: how the cases were built, what they cover,
what they measure, and where the boundary of that measurement is. Every number here is a
committed receipt in [`results/`](results).

**Looking for the numbers?** [Reference results](#reference-results), and the
[vacuity check](#the-vacuity-check) that proves they can move. **Looking for the limits?**
[Why 100% is the expected result](#read-this-before-you-read-the-100) ·
[What this does not prove](#what-this-benchmark-does-not-prove) ·
[Cross-family calibration](#cross-family-calibration-not-letting-gpt-grade-gpts-homework) ·
[Cases that were cut](#cases-that-were-cut)

An AI agent says *"the `customers` table is owned by Alice and contains no PII."* Two
questions follow, and only one of them is usually asked. The first is *is that true?* The
second — the one this dataset is about — is *does the catalog actually say so, or is it
simply silent?* Those are not the same question, and a system that confuses them will cry
wolf on every under-documented table in a warehouse.

So every claim here is labeled with one of **three** verdicts, not two:

| Verdict | Meaning |
| --- | --- |
| **Supported** | The catalog affirms the claim. |
| **Contradicted** | The catalog **positively disagrees**. |
| **Insufficient-Coverage** | The catalog is **silent**. Absence, not disagreement. |

The third verdict is the whole point. Collapsing it into Contradicted is the single most
common way a groundedness checker goes wrong, and 8 of the 40 cases exist to catch exactly
that.

---

## The dataset

`cases.json` — 40 cases, **26 of them marked `hard`**. Each carries:

```json
{
  "id": "class-18",
  "agent_text": "The email column of urn:li:dataset:(...,legacy_accounts,PROD) contains PII.",
  "target_urn": "urn:li:dataset:(...,legacy_accounts,PROD)",
  "claim": { "claim_type": "classification", "field_path": "email",
             "labels": ["urn:li:tag:PII"], "present": true, "...": "..." },
  "expected_verdict": "Insufficient-Coverage",
  "rationale": "A column literally named email, untagged, on a table nobody reviewed: unclassified is not clean and not dirty.",
  "hard": true,
  "tags": ["the-distinction-that-must-not-blur"]
}
```

**Both the prose and the structured claim.** The prose exercises a full system end to end
(extraction included). The structured claim exercises a verdict engine directly, with no
model in the loop. Carrying both lets you tell a *checker* bug from an *extraction* bug —
which are different problems with different fixes, and an aggregate accuracy score blurs
them into one.

**Every label carries a one-line rationale, and the one-line rule is a forcing function.**
If a label needs a paragraph to justify, the claim is ambiguous and does not belong in a
benchmark. Cases that could not be justified in one line were cut rather than kept and
argued about — see [Cases that were cut](#cases-that-were-cut).

## The 12-cell coverage map

The unit of coverage is not the claim, it is the **(claim type × verdict) cell**. Four claim
types × three verdicts = twelve, and all twelve are populated. A claim type that cannot
reach one of its verdicts is not a partial feature — it is a *silent misclassifier*, because
every claim that belonged in the unreachable cell lands somewhere else instead, confidently.

| | Supported | Contradicted | Insufficient-Coverage |
| --- | --- | --- | --- |
| **Freshness** | 2 | 4 | 1 |
| **Ownership** | 3 | 2 | 2 |
| **Classification** | 6 | 9 | 4 |
| **Schema** | 4 | 2 | 1 |

## The cases that make it a benchmark rather than a demo

A benchmark made of easy cases reports a high number and means nothing. These are the ones a
plausible, well-intentioned system gets **wrong**:

**`recipient_email_hash` reads as PII and is tagged `NonPII`.** (`class-07`) It is a salted,
irreversible hash and the catalog says so at column level — while the *table* it sits on is
filed under the `EmailAddress` glossary term, because the table is *about* email. A checker
that reads table metadata answers this one confidently and wrongly. **Contradicted.**

**`CustomerIdentifier` reads as personal and is deliberately not PII.** (`class-05`) It is a
surrogate key. It sits *outside* the PII node in the catalog's glossary hierarchy, and that
placement — a governance act someone performed — is what decides it. Not the fact that the
word "customer" appears in it. A system that infers PII from names flags every table in the
warehouse.

**An untagged `email` column on a table nobody reviewed.** (`class-18`) Not clean. Not dirty.
**Insufficient-Coverage.** This is the distinction that must not blur, and it is the reason
the third verdict exists.

**The same table, two freshness claims, opposite labels.** (`fresh-02`, `fresh-04`)
`revenue_daily` is ~10,003 hours old. "Refreshed within the past year" (8,760h) is
**Contradicted**. "Refreshed at least once in the past two years" (17,520h) is **Supported**.
Freshness is *arithmetic against a stated window*, not a label attached to a dataset — and a
system that had merely memorized "revenue_daily is the stale one" gets exactly one of these
right.

**A claim that cannot be corrected, only stood by.** (`schema-05`) "It has an `ssn` column"
is false, and there is no way to say "it does *not* have an `ssn` column" as a schema claim.
The tempting fix — name a column that *does* exist — replaces a false claim with an unrelated
true one and re-verifies green. The honest move is to stand by the claim and be marked wrong.

**A PII column on a table with no PII markings at all.** (`class-09`) `audit_log` carries no
tag, no term, no property — and its `actor_email` column is tagged PII. "This table is
PII-free" is **Contradicted**, because a table-level PII claim is *existential*: one PII
column settles it. Signals propagate **up**, never down.

**Its mirror image.** (`class-14`, `class-17`) A table tagged PII does *not* make its untagged
`signup_ts` column PII. "Contains PII" means *somewhere in it*, not *everywhere in it*.

## The governance policy the labels apply

Labels are what the **catalog** says, under a policy declared as data
([`checkers/policy.py`](../src/attest/checkers/policy.py)), not what a reasonable person
would guess about the data. The rules, in full:

1. **Absence is not denial.** An untagged table does not say "no PII". An unowned table does
   not say "nobody owns it". A dataset with no `lastModified` is not stale.

2. **Closed-world reasoning is granted by the catalog, never assumed by the checker.** Only a
   `Verified` tag declares a table's classification *complete*. On such a table — and only
   there — a missing PII tag is a reviewed finding, so "this table is PII-free" is
   **Supported**. Without it, the same claim is **Insufficient-Coverage**.

3. **Three signals count as PII, any one is enough:** the `PII` tag; a glossary term filed
   *under the PII node*; the `hasPII` custom property, truthy. `hasPII=false` counts for
   nothing in either direction — a scanner that looked and found nothing is not a review.

4. **Precedence when signals disagree.** Column over table. But a column's PII tag settles a
   *table*-level claim (existential). Within one grain, an explicit tag beats an implied
   signal — a human's classification act outranks a term's subject matter or a machine's
   guess.

5. **Freshness is arithmetic.** Owners and schema columns are **exhaustive** lists — but a
   *missing* ownership aspect, or a missing schema, is silence rather than denial.

## Read this before you read the 100%

A perfect score should make you suspicious, and it should. There are two failure modes that
look exactly like this one from the outside, and neither is ruled out by the number itself:

1. **the benchmark is too easy to be meaningful**, or
2. **the benchmark is overfitted to the implementation it scores.**

So here is the honest account, and it does not involve making the number worse.

### 100% is the EXPECTED result, not a surprising one

Attest's checkers are **deterministic code that implements exactly the rules these labels
encode.** Freshness is a date comparison. Ownership is set membership. Schema is string and
type comparison. Classification applies the precedence rules in
[`policy.py`](../src/attest/checkers/policy.py). The labels apply *those same declared rules*
to *those same catalog facts*.

Given that, **anything below 100% would be a bug, not a difficulty signal.** A freshness case
scoring wrong would mean the date arithmetic is broken. This benchmark is not a test of how
*capable* the system is; it is a **regression net** and a **coverage proof** — that all 12
cells are live, that the hard precedence cases resolve the way the policy says, and that no
model has leaked into the verdict path.

Reporting 97% here would not be more credible. It would mean something was broken.

**So do not read the 100% as "Attest is smart".** Read it as: *the deterministic core does
what it says it does, on every case including the ones designed to break it, every time.*

### The number is only worth anything because it CAN move

That is what the sabotage harness is for, and it is not a footnote — it runs **in the test
suite**, on `just check` and in CI, not only when someone remembers to type `just
bench-sabotage`. Replace the classification checker with one that affirms everything:

| | Healthy | Sabotaged |
| --- | --- | --- |
| Accuracy | 100% | **67.5%** |
| Supported precision | 1.000 | **0.536** |
| Contradicted recall | 1.000 | **0.471** |
| Correctness failures | 0 | **9** |
| Coverage failures | 0 | **4** |

A benchmark that cannot fail is a green light wired to nothing. This one fails, on demand,
and the failure is asserted by a test. The full run, all 13 mis-scored cases named, is
[`results/core-sabotaged-classification.json`](results/core-sabotaged-classification.json);
the mechanics are in [The vacuity check](#the-vacuity-check).

### What this benchmark does NOT prove

Stated plainly, because naming the boundary is what makes the number credible rather than
hollow:

- **It is a seeded catalog, not a real one.** Every dataset here was written by
  [`seed/generate_seed.py`](../seed/generate_seed.py) to be a specific kind of witness. Real
  catalogs are messier: half-finished glossaries, tags applied by three teams with three
  different meanings, `lastModified` timestamps that mean "when the pipeline ran" rather than
  "when the data changed", and columns whose classification is *genuinely* contested by the
  people who own them. **None of that is exercised here.**

  **This one has a partial answer, and it cost us something to get.**
  [`docs/external-trial.md`](../docs/external-trial.md) runs 15 claims through the real
  pipeline against DataHub's own `showcase-ecommerce` datapack — 67 datasets over 7
  platforms, whose metadata nobody here wrote. It is emphatically **not** a second benchmark
  and nothing in it is scored. What it found is that **15 of those 67 datasets cannot be
  audited at all**: Attest's GraphQL query has no `CorpGroup` arm, and because
  `generate_seed.py` emits `CorpUser` owners *exclusively*, no fixture, no offline test and
  no live test in this repository could ever have surfaced it. That is this bullet, proven
  consequential by the first instrument built to test it.
- **The labels apply the policy; they do not validate it.** If
  "an untagged column on an unreviewed table is Insufficient-Coverage" is the *wrong rule*,
  then the checker and the labels are wrong together and score 100% doing it. That is a
  design argument, made in `policy.py`, and no benchmark settles it. (The cross-family
  labeler is a partial check on this — see below — and it found exactly one such gap.)
- **It does not measure whether the four claim types are the right four**, or whether an
  agent's real output decomposes into them cleanly.
- **It measures a system against ground truth it can, in principle, see.** The catalog is the
  oracle *and* the input. That is the correct design for a groundedness auditor — the whole
  point is fidelity to the catalog rather than to the world — but it means this benchmark
  cannot tell you whether the *catalog* is right about the data.

The number that would be genuinely hard to get, and which nobody has, is accuracy against a
production catalog with contested metadata. This is not that, and it does not claim to be.

## Reference results

Measured against Attest ([github.com/…/attest](../README.md)), `gpt-4o-mini`, DataHub Core
v1.5.0.6. `just bench` and `just bench-full` reproduce these, and every figure below opens in
one of the two committed receipts.

| | Deterministic core ([receipt](results/core.json)) | Full pipeline, prose in ([receipt](results/full.json)) |
| --- | --- | --- |
| Accuracy | **100%** (40/40) | **100%** (40/40) |
| Macro F1 | **1.000** | **1.000** |
| Correctness failures (Supported ↔ Contradicted) | 0 | 0 |
| Coverage failures (anything ↔ Insufficient) | 0 | 0 |
| pass@k | **100%** (k=5) | **100%** (k=3) |
| Extraction fidelity | n/a — nothing is extracted | **40/40 exact**, 0 extras, 0 duplicates |
| Cost | $0 | **$0.0153** / 40 claims |

Per verdict, both modes: precision 1.000, recall 1.000, F1 1.000 across Supported (n=15),
Contradicted (n=17), Insufficient-Coverage (n=8).

The full-pipeline run also recorded **39/40 model-authored explanations, 1 template
fallback and 5 guard-rejected drafts**. Both halves of that matter and they pull opposite
ways: a guard that rejected everything would score 0% model-authored and catch every
hallucination. The fallback is the design working — a draft that failed a gate degraded to
something *true* rather than to something plausible — and it is reported rather than
re-rolled.

### The scorer changed. These numbers were produced by v2

**Do not compare the table above to a receipt from before `scorer_version: 2`.** The
verdict metrics are unmoved — accuracy, macro-F1, every per-verdict figure, the confusion
matrix and pass@k are identical under both scorers — but the *method* that decides which
extracted claim answers which labeled one is different, so extraction fidelity is not
comparable across the change.

Scorer **v1** bound a case's verdict to the FIRST audit whose target URN matched, and
judged fidelity with a schema comparison that dropped `native_type`. That let a claim of
the wrong *family* answer a question the label never asked, let extraction ORDER decide
which claim was scored, and discarded every unmatched claim unexamined — so a duplicate
read as a perfect extraction and an unintended extra was invisible.

Scorer **v2** (`matching_policy: canonical-subject-one-to-one-v1`) matches labeled claims
to extracted ones **one-to-one on the full canonical subject** — family, target URN,
family-specific fields, and schema types normalized for formatting only. Wrong-family and
missing extractions score `No-Claim`, never a borrowed verdict; partial subjects are still
scored end-to-end but reported with their field-level difference; extras and duplicates are
counted and named without touching accuracy.

Every full-pipeline receipt now carries `scorer_version`, `matching_policy`,
`scorer_commit` and `scorer_tree_dirty`, so a reader holding two receipts can always tell
which scorer produced which. The pre-v2 receipt is preserved in git history
(`git show <commit>:benchmark/results/full.json`).

**pass@k is not a nice-to-have here, it is a bug detector.** Attest's verdicts come from date
math, set membership and string comparison — so the same claim must produce the same verdict
every time. A pass@k below 100% on the deterministic core would not be a weak score; it
would mean a model had leaked into the verdict path, and it would be a bug. The full pipeline
can in principle dip below 100% *without* such a leak, because extraction is a model call, so
the harness **diagnoses** rather than merely counting: if the extracted claim was identical
across runs and the verdict moved, that is a leak and it is reported in capitals; if the
extracted claim moved, that is extraction variance and a different finding. Neither occurred.

### Where the model risk actually lives — and a correction to the obvious question

The natural question is *"how many cases does the LLM never touch, so that 100% is really
just measuring the checkers?"* The answer needs a correction before it needs a number.

**In `--full` mode, zero of the 40 cases bypass the model.** All 40 go through decomposition.
So the end-to-end 100% is not "the model was uninvolved" — it is *the model transcribed 40
sentences correctly **and** the checkers then decided 40 claims correctly.* Two separate
things, which is exactly why the harness runs both modes and reports extraction fidelity
separately from accuracy.

And the framing "the deterministic checker saves it" is **not what happens.** A checker handed
the wrong claim will faithfully decide the wrong claim — it cannot rescue a mis-transcription,
and it does not try to. What the deterministic core guarantees is that **the verdict is never
invented**. What happens to a mangled claim is that it becomes a *visible gap* rather than a
confident wrong answer: when the decomposer floored *"every 30 minutes"* to
`max_age_hours: 0`, the claim schema rejected it and the case scored `No-Claim` — an audit
with a hole in it, reported as such. That is the design working, and it is a different
guarantee from the one the question assumes.

With that said, here is the split — **27 of 40 cases put a non-trivial demand on the
decomposer**, and 13 need only the claim type and the URN:

| What the decomposer must get right | Cases | Why it's risky |
| --- | --- | --- |
| **column-scoping** (`field_path`) | 8 | Attach the claim to the table instead of the column and, under the precedence rules, the verdict can legitimately differ. |
| **negation** (`present=False`) | 8 | "contains **no** PII". Drop the negation and the claim inverts — and nothing downstream can recover a lost "no". |
| **numeric-window** (`max_age_hours`) | 7 | Prose to arithmetic: "refreshed daily" → 24. |
| **type-assertion** (`native_type`) | 4 | "of type `NUMBER(12,2)`" carried onto the right column. |
| **fractional-window** | 1 | **The one that actually broke.** See below. |
| **term-urn** (glossary, not tag) | 1 | A glossary term URN rather than a tag URN. |
| *type + URN only* | 13 | Still goes through the model; just gives it nothing subtle to get wrong. |

**That second group is where the real risk lives, and it is where the one real failure
happened.** `just bench-full` prints this table and marks any row that mis-extracted.

### What the benchmark caught on its first run

**A real bug, immediately.** The claim *"the table is updated every 30 minutes"* came back
from extraction as `max_age_hours: 0` — the model floored 0.5 to an integer — which the claim
schema correctly rejects (`gt=0`), so the claim was **silently dropped** and the case scored
`No-Claim`. Full-pipeline accuracy was 97.5%, not 100%.

The cause was a prompt bug, not a checker bug: every example in the extraction schema's
description of `max_age_hours` was a whole number ≥ 24 (`'daily' is 24`), so the model had
learned the shape of the field from its examples. The fix was to *widen the description* —
fractions are allowed, `'every 30 minutes' is 0.5` — and emphatically **not** to relax
`gt=0`, which would have admitted a meaningless "updated within zero hours" claim. Accuracy
went to 100%.

That is the benchmark doing its job on day one, and it is why the sub-hour boundary case is
in the dataset.

## The vacuity check

**A benchmark that cannot fail is a green light wired to nothing.** Before trusting any
number above, break the thing it measures and confirm the number moves:

```
just bench-sabotage      # replaces the classification checker with one that affirms everything
```

| | Healthy | Sabotaged |
| --- | --- | --- |
| Accuracy | 100% | **67.5%** |
| Supported precision | 1.000 | **0.536** |
| Contradicted recall | 1.000 | **0.471** |
| Insufficient-Coverage recall | 1.000 | **0.500** |
| Correctness failures | 0 | **9** |
| Coverage failures | 0 | **4** |

Receipt: [`results/core-sabotaged-classification.json`](results/core-sabotaged-classification.json).

It names all 13 mis-scored cases. The command **exits non-zero if the numbers do not move**,
so a metric that had quietly stopped measuring anything fails CI rather than reporting 100%.

## Cross-family calibration: not letting GPT grade GPT's homework

The labels are hand-written by one person. Every number above is measured against them, so
if the labels are wrong, everything downstream is wrong — invisibly.

The obvious check is to ask a model. That check has a documented flaw: **LLM judges favor
their own outputs.** Zheng et al. (2023), *Judging LLM-as-a-Judge with MT-Bench and Chatbot
Arena* ([arXiv:2306.05685](https://arxiv.org/abs/2306.05685)), measure GPT-4 preferring its
own generations at roughly a **10% higher win rate**, and Claude-v1 at roughly **25%**
(self-enhancement bias). Attest runs on `gpt-4o-mini`; validating its labels with a GPT judge
would inflate the agreement number by construction, in the direction that flatters the
report.

So the second labeler is **Nemotron — a Llama-family model, served by NVIDIA** — and *the
family is the point*. A different provider serving similar weights would break nothing.

It is given the same catalog facts (rendered by code, never summarized by a model) and the
same declared policy a human labeler had, plus **four few-shot worked examples**; Zheng et al.
also measure few-shot prompting raising a judge's self-consistency from **65% to 77.5%**. The
examples are **synthetic and none of them is a benchmark case** — a few-shot example drawn
from the set being labeled is answer-key leakage and would show up as agreement.

**It never touches the verdict path.** No model decides a verdict in Attest, and that does not
soften because a second model has appeared in the repository.

### Result: 97.5% agreement, and the one dispute is not a stable property of any label

Two calibration runs are committed, each a single pass of the labeler at `temperature=0`, and
both agree with the hand labels on **39 of 40 cases (97.5%)**:

| Run | Agreement | Case disputed | Receipt |
| --- | --- | --- | --- |
| Before the tie-break rule was declared | **97.5%** (39/40) | `class-15` | [`calibration-before-policy-declared.json`](results/calibration-before-policy-declared.json) |
| After it was declared | **97.5%** (39/40) | `class-03` | [`calibration.json`](results/calibration.json) |

An independent model family, applying the same declared policy to the same catalog facts,
reaches these hand labels 39 times out of 40 in each run. **The disputed case is different
between the two runs**, and comparing them is the finding — not the 97.5%:

- Before the governance tie-break was written into the prompt, Nemotron disputed `class-15`
  and agreed on `class-03`.
- After it was written in, `class-15` **agreed** — declaring the rule resolved it — and
  `class-03` newly disputed.

The dispute is not a stable fact about a label; it moves. What these two runs do **not** show
is a dispute set "reshuffling across N identical runs" — they differ in prompt (the tie-break
rule was added between them), so they cannot demonstrate instability under *identical*
conditions, and this section claims no such thing.

### One dispute is signal, the other is noise — and the receipts say which

**`class-15` is signal: a governance tie-break the labeler surfaced.** *"The `signup_ts` column
of `customer_profile` contains PII."* `signup_ts` is untagged; its table is `Verified`. In the
before-policy run Nemotron decided the column-scoped claim on the column's own (absent) labels
and returned **Insufficient-Coverage**, where Attest returns **Contradicted**. Two declared
rules collided — "column over table" versus "`Verified` licenses closed-world reasoning" — and
the tie-break existed only as a *comment inside a checker*, precisely what
[`policy.py`](../src/attest/checkers/policy.py) exists to prevent. It is now declared as
`COMPLETENESS_REACHES_COLUMNS`, and **the after-policy run agrees** (`Contradicted` /
`Contradicted`). The disagreement pointed at a real gap, the gap was written down, and the
disagreement closed — a transition both receipts record, which is why "signal" is earned rather
than asserted. **Finding a rule that lived in an `if` is the most valuable thing this labeler
did**, worth more than the agreement percentage.

**`class-03` is noise, and the receipts distinguish it from a genuine disagreement.**
*"`email_campaign_stats` contains no PII."* The table carries an explicit `NonPII` tag *and* the
`EmailAddress` term (under the PII node); precedence rule 4 — an explicit tag beats an implied
signal at the same grain — makes it **Supported**. The test for noise is simple: a genuine,
defensible different reading returns the *same* verdict every time. This one does not. Nemotron
labeled `class-03` **Supported** (agreeing) in the before-policy run and **Contradicted**
(disputing) in the after-policy run — the same case receiving both the agreeing and the
disagreeing verdict. And the only declared change between the runs was the
`COMPLETENESS_REACHES_COLUMNS` rule, which governs *untagged columns of Verified tables* and has
nothing to do with a table-level PII-free claim decided by tag-over-term precedence. A model
whose verdict on a case moves when a rule that does not apply to that case is added is
exhibiting instability, not a principled reading: it applied the correct precedence once and
then abandoned it. The label stands; the flip is labeler noise.

One meaningful disagreement, one noisy one, neither stable — a sharper and more honest result
than a single agreement number, and every part of it is in the two committed receipts.

### The argument this still makes

A judge that returns **Supported** on `class-03` in one run and **Contradicted** in the next, at
`temperature=0`, cannot be a stable source of ground truth — it can only calibrate one. Attest's
core is the contrast: asked the same claim five times it returns the same verdict five times
(**pass@5 = 100%**, in the reference results above). That is the entire reason an auditor's
verdicts come from date math and set membership rather than from a model — measured here, in
this repository, not cited from a paper.

### The rules this instrument follows

- **Disagreements are surfaced, never resolved by taking my own side.** Do not edit a label to
  match the model, and do not tune the prompt until it agrees. The one time declaring a rule was
  the right response to a dispute, it was because the rule was genuinely missing from the policy
  (`class-15`) — not to chase agreement.
- **The harness exits non-zero below 90% mean agreement**, because ground truth an independent
  family disputes on more than ~1 case in 10 is shakier than its author thinks.
- **It never touches the verdict path**, and the pipeline default stays `gpt-4o-mini`.

**What agreement does not prove.** The labeler applies the *same policy* the labels encode. So
agreement shows the labels **follow from** the policy — that the rules were not mis-applied or
fat-fingered. It does **not** show the policy is right. If "an untagged column on an unreviewed
table is Insufficient-Coverage" is the wrong rule, both labelers apply the wrong rule and
agree. That is a design argument, and it is made in `checkers/policy.py`, not settled by a
model.

## Why not RAGAS or DeepEval?

Neither is used, and that is a decision rather than an omission. The full reasoning is right
here — the [main README](../README.md)'s Documentation table points back to this section. Both
frameworks score an **LLM-generated answer** against retrieved context. Attest's verdicts are
not LLM-generated — they come from date math, set membership and string comparison — so there
is no generated answer to score. **Precision/recall against hand-labeled ground truth is the
correct method for a deterministic classifier**, and that is `sklearn.metrics`. Adopting a
judge framework would also *imply* that Attest's verdicts are LLM-judged when they explicitly
are not, undercutting the strongest claim the system makes about itself.

## Cases that were cut

Kept honest, because a benchmark that silently drops its ambiguous cases is measuring the
easy ones and reporting the average.

| Cut | Why |
| --- | --- |
| "`customer_profile` contains sensitive data" | "Sensitive" is a tag in this catalog *and* an English word. The claim is about the tag or about a judgement, and the sentence does not say which. Ambiguous → cut. |
| "`orders_fact` is well documented" | Not one of the four claim types, and "well" is not a catalog fact. |
| "`legacy_accounts` should be deleted" | A recommendation, not a claim about data. Nothing in a catalog can support or contradict it. |
| Cross-dialect type equality (`int8` ≈ `BIGINT`) | Attest deliberately does not attempt it (a known boundary), so a case asserting either answer would be testing an unmade decision rather than a made one. |

## Reproducing

```bash
just setup                 # install
just seed                  # generate the catalog and ingest it into DataHub
just bench                 # deterministic core, pass@5. Free.
just bench-full            # whole pipeline, real model. ~1.5 cents.
just bench-sabotage        # the vacuity check. Exits non-zero if the metrics do not move.
just bench-calibrate       # cross-family labels (needs NVIDIA_API_KEY).
```

The catalog is generated by [`seed/generate_seed.py`](../seed/generate_seed.py) and every
dataset in it exists to be a specific kind of witness. The reference `now` for freshness is
**reconstructed from the catalog itself**, never the wall clock — the seed writes relative
timestamps, so a wall-clock `now` would make the freshness labels rot with age and report a
regression against completely correct code.

## Citing

```
@misc{attest-golden-benchmark,
  title  = {The Attest Golden Benchmark: hand-labeled groundedness claims
            with a three-verdict schema},
  note   = {40 cases over a seeded DataHub catalog; 12-cell (claim type x verdict) coverage},
  year   = {2026}
}
```

Licensed under Apache-2.0, with the rest of the repository.
