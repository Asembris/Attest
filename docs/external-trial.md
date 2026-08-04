# The external trial: Attest against a catalog it did not author

**Every number Attest has is measured against a catalog `seed/generate_seed.py` wrote, with
labels that apply a policy we wrote.** [`benchmark/README.md`](../benchmark/README.md) says
so plainly — *"it is a seeded catalog, not a real one"*, *"the labels apply the policy; they
do not validate it"* — and that caveat, correct as it is, is the project's weakest point.
This is the partial answer to it: 15 claims run through the real pipeline against **DataHub's
own `showcase-ecommerce` datapack**, 67 datasets over 7 platforms, whose metadata nobody here
wrote a line of.

| | |
| --- | --- |
| **Status** | Run, with a committed receipt. A measurement, **not a score**. |
| **Runner** | [`spikes/external_trial.py`](../spikes/external_trial.py) — `just external-trial` |
| **Receipt** | [`docs/external-trial/results.json`](external-trial/results.json) — every figure below opens here |
| **Catalog** | DataHub `showcase-ecommerce` datapack, checksum-pinned — [`external-trial/ingest.md`](external-trial/ingest.md), `just external-ingest` |
| **Measured against** | DataHub Core **v1.5.0.6**, `gpt-4o-mini`, 2026-08-04. 15 claims, **$0.0077**, 117s |

**Jump to:** [the headline finding](#the-headline-a-gap-our-own-seed-could-never-expose) ·
[what the catalog carries](#what-the-external-catalog-actually-carries) ·
[the 15 claims](#the-15-claims) · [what surprised us](#what-surprised-us) ·
[predictions](#the-predictions-made-before-the-run) ·
[what this does not prove](#what-this-trial-does-not-prove)

---

## This is not a second benchmark, and the distinction is load-bearing

Nothing here is scored. No accuracy, no macro-F1, no confusion matrix — and the omission is
deliberate rather than lazy.

The golden benchmark is a **conformance gate**: 40 hand-labeled claims over a catalog built
to be a witness, where **100% is the expected result** because the checkers implement exactly
the rules the labels encode. Re-using that frame over 15 unlabelled claims on a foreign
catalog would import a scoring apparatus this run has not earned, and would quietly undercut
the argument the benchmark makes about itself.

The question here is different, and narrower:

> **Does Attest produce defensible verdicts against metadata it did not design, and where
> does it hit its own documented limits?**

A mixed result was the goal. We got one, and the most valuable part of it arrived before a
single audit ran.

---

## The headline: a gap our own seed could never expose

**15 of the 67 datasets are unauditable.** `client.fetch_dataset` raises
`MalformedResponseError` on every one of them, and the trigger is this, straight off the wire:

```json
{"owner": {}, "ownershipType": {"urn": "urn:li:ownershipType:__system__technical_owner"}}
```

An owner entry whose `owner` object is **empty**. The cause is not the catalog. It is
Attest's own query — [`client.py`](../src/attest/datahub/client.py)'s `DATASET_QUERY` asks
for:

```graphql
owner { ... on CorpUser { urn properties { displayName email } } }
```

**There is no `CorpGroup` arm.** A dataset owned by a group matches no inline fragment, GMS
returns an empty object for it, and Session 23's `_urns` guard — written to stop a
present-but-URL-less entry being normalized to the empty-string URN `''` — correctly refuses
to make data out of it.

Three things about that are worth separating, because they are different facts:

- **The guard did its job.** It failed **closed**. The alternative, and the behaviour Session
  23 was written to remove, is a confident `Contradicted`: *"ORG_DATA_PLATFORM is not an
  owner; the catalog lists `''`"*. Nothing wrong reached a verdict.
- **The diagnosis is wrong.** The error says *malformed catalog response*. The response is
  fine. The **query** is incomplete, and Attest cannot tell the difference — it sees an empty
  object and has no way to know it asked for the wrong shape.
- **No test in this repository could have caught it.** `seed/generate_seed.py` emits owners
  through `make_user_urn` exclusively, so every seeded dataset, every captured fixture in
  `tests/fixtures/snapshots/`, and therefore the entire offline tier, the live tier and the
  12-cell matrix contain **corpuser owners and nothing else**. The offline suite is green.
  The live suite is green. `test_fixture_drift` is green. The gap is invisible from inside.

That is this project's own seeded-catalog caveat, proven consequential by the first
instrument built to test it. It is the same shape as the rule Session 5 left behind — *a fake
cannot fail in a way the real thing fails through machinery the fake does not have* — one
level up: **a seed cannot exercise a shape it never emits.**

**It is documented, not fixed.** Changing the catalog read days from submission, with no
fixture anywhere that exercises group ownership, would be a change whose correctness rests on
the same seed that hid the problem. The honest artifact is the finding.

**What it costs, stated precisely.** Ownership claims about group-owned datasets are
unauditable, and they surface as `ClaimError` — kept out of `audits` entirely, counted in no
verdict tally, named in `errors`. Not a wrong verdict; a refused question. Every other claim
family on those same 15 datasets is equally blocked, because the read fails before any
checker sees it.

---

## What the external catalog actually carries

Read back through Attest's own client after ingestion — never from the pack's documentation.
**52 of 67 datasets readable** (the other 15 are the finding above).

| Aspect | Coverage | What it means for the trial |
| --- | --- | --- |
| `schemaMetadata` | **52/52** | Schema claims fully exercisable — and schema-**Insufficient-Coverage is unreachable** here, because nothing lacks a schema |
| `lastModified` | **25/52** | Freshness reaches all three verdicts |
| `glossaryTerms` | 51/52 | Populated, in a vocabulary Attest cannot read structurally |
| `globalTags` (table) | 12/52 | All operational (`💲 Large Table`, `📈 Most Queried`) — **zero governance tags** |
| `ownership` | **5/52** | 47 absent. Every survivor is a `corpuser` |
| column-level terms | 1/52 | `cust_email` → `Email Address`, `phone_number` → `Phone Number` |

### The governance vocabulary is a near-miss on all three PII signals

[`policy.py`](../src/attest/checkers/policy.py) names three signals, any one sufficient. This
catalog trips **none** of them, and it is not because the catalog is silent about PII — it is
because it says it differently:

| Attest's signal | What this catalog has instead |
| --- | --- |
| the `urn:li:tag:PII` tag | no governance tags at all on any readable dataset |
| a term filed under `urn:li:glossaryNode:PII` | a term named literally **`PII`** — description *"Personally Identifiable Information…"* — on **51 of 52** datasets, filed under a node named **`Classification`** |
| the `hasPII` custom property, truthy | dbt models carry **`contains_pii`** (and set it `"False"`, which by policy fires nothing anyway) |
| the `Verified` completeness marker | **absent everywhere**, so closed-world reasoning is never licensed and *"contains no PII"* can never be Supported anywhere in this catalog |

And the sharpest one: `Email_Address` and `Phone_Number` exist as glossary terms whose own
descriptions read **"Subject to PII handling requirements"** — and they have **no parent node
at all**. Attest reads structure and infers nothing from a name, by design. Here that design
decision costs it the answer, and [`ext-class-02`](#the-15-claims) is what that looks like.

**This is the deferred item `Semantic glossary-term matching` occurring in the wild**, on a
catalog nobody wrote for us. The declared position — *"structure is a declaration; a name is a
guess"* — is unchanged by meeting it. But it is no longer hypothetical.

---

## The 15 claims

Every expectation was formed by **reading the catalog before the run** and applying the
declared policy — never by running the checker and writing down what came out. The
`human reading` column is separate on purpose: it is what a competent person reading the same
DataHub page would say, and **where it disagrees, the disagreement is the finding.**

| # | Claim | Outcome | Human reading |
| --- | --- | --- | --- |
| `ext-fresh-01` | `order_history` updated within 24h | **Contradicted** | agrees — 4530h old |
| `ext-fresh-02` | …refreshed at least once in two years | **Supported** | agrees — same table, opposite verdict, because freshness is arithmetic against a stated window |
| `ext-fresh-03` | dbt `order_history` refreshed daily | **Insufficient-Coverage** | agrees — *but see below* |
| `ext-fresh-04` | PowerBI `ORDER_DETAILS` refreshed weekly | **Insufficient-Coverage** | agrees — no timestamp at all |
| `ext-own-01` | Tableau dataset owned by `brock1@` | **Supported** | agrees |
| `ext-own-02` | PowerBI dataset owned by `brock1@` | **Contradicted** | agrees — the owner is `kirk@`, and the list is exhaustive |
| `ext-own-03` | `customers` owned by `brock1@` | **Insufficient-Coverage** | agrees — unowned |
| `ext-own-04` | dbt `orders` owned by `ORG_DATA_PLATFORM` | **ClaimError** | **disagrees — a human sees the group owner on the page. Attest cannot.** |
| `ext-class-01` | `cust_email` labelled `Email_Address` | **Supported** | agrees — **but right by luck, see below** |
| `ext-class-02` | `cust_email` contains PII | **Insufficient-Coverage** | **disagrees — a human says PII** |
| `ext-class-03` | `customers` contains no PII | **Insufficient-Coverage** | disagrees (it has `cust_email`, `phone_number`, `dob`) — **but this is the conservative error, not the dangerous one** |
| `ext-class-04` | `orders` is *not* labelled the `PII` term | **Contradicted** | agrees |
| `ext-schema-01` | `cust_email` is `VARCHAR` | **Supported** | agrees |
| `ext-schema-02` | has an `ssn` column | **Contradicted** | agrees |
| `ext-schema-03` | `zipcode` is `VARCHAR` | **Contradicted** | agrees — the catalog records `NUMBER(38,0)` |

**All 15 outcomes matched their predeclared expectations — 14 produced an assessment, and one
produced the expected `ClaimError`, which this architecture deliberately does not count as a
verdict.** (`ext-own-04`: refusing to answer is not answering, so it is kept out of `audits`
entirely and tallied in no verdict count.) That sentence is doing less work than it looks like it
is, and the next section is why.

### 5 Insufficient-Coverage, and two of them a human would argue with

`ext-class-02` is the case worth arguing about. A column named `cust_email`, holding email
addresses, carrying a glossary term whose description says *"Subject to PII handling
requirements"* — and Attest returns **Insufficient-Coverage**. Its own reason:

> *column 'cust_email' carries no PII signal, and the table is not marked
> classification-complete. None of the recognized PII signals fired (tag, term, property),
> but absence of a signal is not evidence of absence — nobody has reviewed it.*

That is correct under the declared policy and it is not the answer a compliance officer
wants. **Both halves of that sentence are the result.** The alternative — inferring PII from
a term because its name and description read personal — is precisely what
[`policy.py`](../src/attest/checkers/policy.py) exists to forbid, and the reason it forbids it
is that the same inference flags `CustomerIdentifier` and every other innocuous
personal-sounding column in a warehouse. The trial does not resolve that tension. It shows the
price, on a real catalog, with a receipt.

`ext-class-03` is the mirror and it lands the other way. A human says *"of course it contains
PII"*; Attest says Insufficient-Coverage. But the claim was **"contains no PII"**, and
Insufficient-Coverage is a **refusal to certify** — the false-assurance guard doing exactly
its job. Getting this one "wrong" in the conservative direction is the product working.

---

## What surprised us

### `ext-class-01` was right by luck, and the receipt says so

The sentence *"The `cust_email` column of `<urn>` is labelled `<term urn>`"* came back from
the decomposer as a **`schema`** claim — `columns: [{name: "cust_email", native_type: null}]`
— **not** a classification claim. The `Email_Address` term URN was dropped entirely. The
schema checker then answered a question nobody asked (*"does this column exist?"*), said
**Supported**, and that happens to be the verdict the trial expected.

A naive match counter banks this as a success. It was caught by checking extracted claim type
against intended family, and the receipt now reports **`answered_the_intended_question:
14/15`** and **`right_by_luck: ["ext-class-01"]`** as figures separate from the outcome match,
never netted against it. This is `benchmark/README.md`'s own rule — *a case that is right by
luck is counted correct AND named, because banking those flatters a broken decomposer* —
firing on the first foreign catalog it met.

**So the honest headline carries two numbers and never nets them: 15/15 outcomes matched what was
predeclared — 14 of them assessments, the 15th the expected `ClaimError`, which is not a verdict —
and 14 of the 15 answered the question that was actually asked.** The two 14s count different
things: the `ClaimError` (`ext-own-04`) *did* answer its intended question, by refusing it exactly
as predicted; the case that did not is `ext-class-01`.

### The write-back failed, and the repair path is what fixed it

The first publishing attempt failed on **all three** claims at the `report` step:

> *Assertion with urn `urn:li:assertion:attest-…` does not exist or is not associated with any
> entity*

The assertion **existed** — a direct `get_assertion` returned it in full — but
`dataset.assertions(urn)` returned `total: 0`. This is the eventual-consistency landmine
(CLAUDE.md §10) at a scale the seeded catalog never produces: seeding writes ~90 entities,
this ingest wrote **3563**, and the assertion→dataset index took far longer than
`writeback.py`'s 30-second retry. A repair attempt ~2 minutes later failed identically. The
**same repair, unchanged, succeeded at ~12 minutes.**

Nothing was lost and nothing was faked: the approval stood, the store recorded that the
catalog did not know it, and `service.retry_writeback` — the call `POST
/audit/{run_id}/writeback` makes — re-ran the idempotent write onto the same
content-addressed artifact. The runner now waits it out through that same path rather than
reporting a success it did not have, and gives up **loudly** rather than hanging.

**This is the failure mode a bulk-loaded production catalog will show and the seeded one
never will.**

### The two predictions that failed, failed in Attest's favour

Both are recorded below. Briefly: no URN was garbled (15/15 verbatim, including hex-UUID
Tableau URNs), and **zero** explanations fell back to the template — though the polarity
guard rejected 4 first drafts, which then passed on retry. The guard is doing real work on
foreign evidence, and the second attempt is enough.

### Two claims reached `STOOD_FIRM` on a catalog nobody wrote for us

`ext-fresh-01` and `ext-class-02` were routed to the correction loop, and in both the model
declined to revise: *"the agent stood by its original claim: the evidence does not say what
the correct value is."* The outcome CLAUDE.md calls live-reachable-by-design, reached here
without anything being arranged for it.

---

## Writing verdicts back into a catalog we did not build

Three verdicts — one per verdict type — were **published through the real approval path**.
Nothing calls `write_claim_artifact` directly: a human `Decision` goes through
`service.approve` exactly as the HTTP API does.

| Claim | Verdict | Artifact |
| --- | --- | --- |
| `ext-fresh-01` | Contradicted | `urn:li:assertion:attest-b62eaa238b1b08277ac7` |
| `ext-fresh-02` | Supported | `urn:li:assertion:attest-46df7a4299eb1416f44a` |
| `ext-class-02` | Insufficient-Coverage | `urn:li:assertion:attest-4d4c8ad1f5f1201e07c9` |

All three were then read back with **`ClaimReader(client, store=None)`** — the second agent,
inheriting only what the catalog alone can tell it — and all three come back `state:
complete`, with their verdict, their reviewer, their structured evidence and the
`snapshot_id` of the catalog state they were decided against.

Two datasets, two freshness claims about **the same** dataset with **opposite** verdicts,
coexisting as distinct artifacts. That is the §10 thesis — one claim, one artifact — holding
on a catalog it was not designed against.

**`history_length` is 3 on each, and that is not a defect.** The trial was run three times
during development against the same catalog. The artifact URN is derived from the claim's
content, so every run appended to the **same** artifact rather than minting a new one, and
each verdict event is keyed by its own run's timestamp. Three real audits, three recorded
events, none overwriting another — the append-only history doing precisely what §10 built it
for, demonstrated by accident.

---

## The predictions, made before the run

Recorded in the runner before anything executed, and preserved in the receipt verbatim.
Reporting the misses is the point.

| | Prediction | Held? |
| --- | --- | --- |
| **1** | At least one long UUID-bearing URN is garbled by the decomposer → `EntityNotFoundError` | **NO.** 15/15 transcribed verbatim, including `…tableau,b2fd91.b980a8c5-28eb-119e-f6ca-4da32732e5be,PROD)`. A small real positive result. |
| **2** | Template-fallback rate rises above the seeded catalog's 0-of-40, because `faithfulness.py` treats capitalized words as a factual class and this evidence is UUIDs and `NUMBER(38,0)` | **NO.** 14 model-authored, **0** fallbacks. The polarity guard rejected 4 first drafts; every one passed on retry. |
| **3** | `ext-fresh-03`'s evidence claims the aspect is ABSENT when it is present holding `{time: 0}` | **YES.** The evidence reads *"Aspect absent — the catalog does not record when this last changed"*. Right verdict, imprecise description: GMS returned a present `lastModified` holding a meaningless zero, and Attest's falsy check cannot tell that from absence. An absent-vs-empty distinction one notch finer than `snapshot.py` preserves. |
| **4** | `contains_pii='False'` moves nothing, and no output mentions that the catalog said something PII-adjacent Attest could not read | **YES**, both halves. Correct behaviour; also the silent half of the vocabulary gap. |

---

## The claim we wrote, traced, and threw away

Kept here because dropping it is the methodology being honest about itself.

A 16th claim was drafted: **"the `customer_id` column of the S3 `customers` table is of type
`NUMBER(38,0)`"** — expected **Contradicted**, on the reasoning that S3 records `int64` while
Snowflake records `NUMBER(38,0)`, a genuine cross-platform sibling divergence in this catalog.

Tracing [`checkers/schema.py`](../src/attest/checkers/schema.py)'s `_type_matches` before
writing the label: the matcher accepts **either** of DataHub's two type vocabularies and
strips precision, so the asserted `NUMBER(38,0)` base-matches the abstract type `NUMBER` —
which is what the S3 table carries too. The correct answer is **Supported**.

The claim was dropped rather than shipped with a wrong expectation. A trial whose labels are
written after seeing the output is not measuring anything.

---

## What this trial does NOT prove

Stated plainly, because naming the boundary is what makes the rest worth reading.

- **One small external catalog is not "works in production."** 67 datasets, one datapack, one
  DataHub version, one afternoon. A real enterprise catalog has thousands of datasets, tags
  that mean three things to three teams, and classifications the owners genuinely dispute.
  **None of that is exercised here.**
- **It is a curated demo catalog, not a messy one.** `showcase-ecommerce` was authored by
  DataHub to look good in a product demo. It is *foreign*, which is the property being
  tested — it is not *hostile*, and it is not the half-finished glossary of a real migration.
- **15 claims are not a sample of anything.** They were chosen to exercise four claim
  families and reach all three verdicts, deliberately including five expected
  Insufficient-Coverage. They are not representative of what an agent says, and no rate
  computed from them means anything.
- **The claims were written by the same person who wrote the checkers.** That is exactly the
  circularity `benchmark/README.md` names about the golden benchmark, and this trial does not
  escape it — it only moves the *catalog* outside our control, not the claims.
- **Nothing here validates the policy.** If "a term filed under no node implies nothing" is
  the *wrong rule*, `ext-class-02` is wrong and this document reports it as correct-by-policy.
  That is a design argument, made in `policy.py`, and no trial settles it.
- **The freshness verdicts are as-of the run date.** They are arithmetic against wall-clock
  now, so `ext-fresh-01` and `ext-fresh-02` were chosen with windows far from their
  boundaries (24h against 4530h; 17520h against 4530h) and will hold for years — but they are
  not timeless, and a receipt is a point-in-time measurement.
- **`just discover` fails while this catalog is loaded**, because its live test asserts a
  seeded URN is in the top 10 search hits and then resolves every hit over GraphQL — which
  now includes group-owned dbt datasets that raise. Expected, and cleared by `just reset`.

---

## Reproducing

```bash
just up                    # DataHub Core v1.5.0.6
just external-ingest       # download (checksum-pinned), filter for Core, ingest
just external-trial        # the 15 claims through the real pipeline. ~$0.008
just external-trial --dry-run   # resolve every target, print what the catalog holds. Free.
just external-ingest --plan     # report what Core refuses; ingest nothing. Free.
```

`just reset` returns the catalog to the seeded state, and removes everything this trial
ingested and published.

Every figure in this document opens in
[`docs/external-trial/results.json`](external-trial/results.json).
