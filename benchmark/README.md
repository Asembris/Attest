# The Attest Golden Benchmark

**40 hand-labeled groundedness claims about a DataHub catalog, with the rationale for every
label.**

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

This dataset is usable without reading a line of Attest's source. [`cases.json`](cases.json)
is the artifact; everything below is what it means and how it was built.

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

## Reference results

Measured against Attest ([github.com/…/attest](../README.md)), `gpt-4o-mini`, DataHub Core
v1.5.0.6. `just bench` and `just bench-full` reproduce these.

| | Deterministic core | Full pipeline (prose in) |
| --- | --- | --- |
| Accuracy | **100%** (40/40) | **100%** (40/40) |
| Macro F1 | **1.000** | **1.000** |
| Correctness failures (Supported ↔ Contradicted) | 0 | 0 |
| Coverage failures (anything ↔ Insufficient) | 0 | 0 |
| pass@k | **100%** (k=5) | **100%** (k=3) |
| Cost | $0 | **$0.0138** / 40 claims |

Per verdict, both modes: precision 1.000, recall 1.000, F1 1.000 across Supported (n=15),
Contradicted (n=17), Insufficient-Coverage (n=8).

**pass@k is not a nice-to-have here, it is a bug detector.** Attest's verdicts come from date
math, set membership and string comparison — so the same claim must produce the same verdict
every time. A pass@k below 100% on the deterministic core would not be a weak score; it
would mean a model had leaked into the verdict path, and it would be a bug. The full pipeline
can in principle dip below 100% *without* such a leak, because extraction is a model call, so
the harness **diagnoses** rather than merely counting: if the extracted claim was identical
across runs and the verdict moved, that is a leak and it is reported in capitals; if the
extracted claim moved, that is extraction variance and a different finding. Neither occurred.

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

### Result: 38/40, **95% agreement**. Two disputed labels, both kept and both explained.

[`results/calibration.json`](results/calibration.json). `just bench-calibrate` reproduces it.
Disagreements are **surfaced, not resolved.** The harness **exits non-zero below 90%
agreement**, because ground truth an independent family disputes on more than ~1 case in 10 is
shakier than its author thinks.

**`class-15` — a genuine contested rule, and the calibration earned its keep here.**
*"The `signup_ts` column of `customer_profile` contains PII."* `signup_ts` is untagged; its
table is tagged `Verified`. I label it **Contradicted** (the table was reviewed, so an untagged
column is a reviewed finding). Nemotron labels it **Insufficient-Coverage** (a column-scoped
claim is decided by the column's own labels, and this column has none).

Both are applying declared rules — *and the policy never said which rule wins.* "Column over
table" and "`Verified` licenses closed-world reasoning" collide here, and the tie-break existed
only as a **comment inside a checker**, which is exactly the thing
[`policy.py`](../src/attest/checkers/policy.py) exists to prevent. Nemotron was not wrong; it
was under-informed, because nobody had written the rule down. It is now declared
(`COMPLETENESS_REACHES_COLUMNS`), with the asymmetry spelled out: a PII **signal** is a fact
about the *data* and does not distribute over columns; `Verified` is a fact about the *review*
and does. **Finding a rule that lived in an `if` instead of in the policy is the single most
valuable thing this labeler did.**

**And then — declaring the rule did NOT change Nemotron's mind.** Told the tie-break
explicitly, it still labels `class-15` Insufficient-Coverage. So this is not a prompt gap; it
is a **genuinely contested reading**, and the case stays in the benchmark flagged as contested
rather than quietly cut. The policy makes a choice; an independent model, given that choice,
still disagrees with it. That is worth a reader knowing.

**`class-03` — labeler noise, and it is honest to say so.** *"`email_campaign_stats` contains no
PII."* The table carries an explicit `NonPII` tag *and* the `EmailAddress` term (under the PII
node). Precedence rule 4(c) — an explicit tag beats an implied signal, at the same grain —
makes it **Supported**. Nemotron labeled it **Supported on the first run** and **Contradicted on
the second**, with nothing changed but the addition of an unrelated rule to the prompt. It
misapplied its own stated precedence rule. The label stands.

### Two runs, and the number went *down*. Reported anyway.

| | Agreement | Disputed |
| --- | --- | --- |
| Before the tie-break rule was declared ([`calibration-before-policy-declared.json`](results/calibration-before-policy-declared.json)) | **97.5%** (39/40) | `class-15` |
| After it was declared ([`calibration.json`](results/calibration.json)) | **95%** (38/40) | `class-15`, `class-03` |

The published figure is the **95%**, because it is the one the current code and prompt
reproduce. It would have been easy to keep the better number and quietly not mention the
second run.

This is also the sharpest available argument for why **Attest's verdicts do not come from a
model.** A single unrelated addition to a prompt flipped a judge's answer on a case it had
previously gotten right. That is what LLM-as-judge instability looks like from the inside, and
it is why an auditor's verdicts come from date math and set membership instead — and why this
labeler calibrates the ground truth and is *never* allowed near the verdict path.

**What agreement does not prove.** The labeler applies the *same policy* the labels encode. So
agreement shows the labels **follow from** the policy — that the rules were not mis-applied or
fat-fingered. It does **not** show the policy is right. If "an untagged column on an unreviewed
table is Insufficient-Coverage" is the wrong rule, both labelers apply the wrong rule and
agree. That is a design argument, and it is made in `checkers/policy.py`, not settled by a
model.

## Why not RAGAS or DeepEval?

Neither is used, and that is a decision rather than an omission. See the [main
README](../README.md#why-not-ragas-or-deepeval) for the full reasoning. In short: both
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
