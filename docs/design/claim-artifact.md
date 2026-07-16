# The claim-level DataHub artifact

**Status:** proposed, awaiting approval. No feature code written.
**Spike:** [`spikes/claim_artifact_probe.py`](../../spikes/claim_artifact_probe.py) (`just spike-claims`)
**Server:** every number and quoted error below is measured against DataHub Core **v1.5.0.6**, pinned.

---

## 1. The problem, stated honestly

Attest's load-bearing thesis is Challenge 1's *"writes results back so the next person or
agent inherits the knowledge."* **That is currently not true**, and the sharpest question in
the room exposes it in one move:

> "Two claims about one dataset are approved. Show me what the next agent inherits from
> DataHub alone."

Today: **an opaque run UUID pointing at application state it cannot reach.**

[`writeback.py`](../../src/attest/writeback.py) writes five dataset-level structured
properties — `attest.verdict`, `.claim_type`, `.checked_at`, `.source_agent`, `.audit_run`.
Structured properties are unversioned and last-write-wins, so:

- **Two claims about one dataset overwrite each other.** The dataset carries the verdict of
  whichever claim was written back last. `writeback.py` already says this out loud: *"a
  dataset with several claims against it in one run carries the verdict of the LAST one
  written back."* It was a documented limitation. It is now the gap.
- **No claim identity, asserted value, grain, evidence, reviewer, or decision** survives the
  write. `attest.verdict = "Contradicted"` does not say *which claim* was contradicted.
- **The history is in Attest's private SQLite store, with no query API.** `attest.audit_run`
  is a join key into a database a second agent cannot open.

The store is right to exist and this design does not remove it (§7). The gap is that
**DataHub holds a verdict with no subject**, and the subject is the entire point.

---

## 2. What the spike proved, and what it cost to learn

Three candidate representations were considered, in order of preference. **Candidate 1 won
on the server, not on paper.** Candidates 2 and 3 are not needed and are not proposed.

| # | Candidate | Verdict |
| --- | --- | --- |
| 1 | **DataHub Assertions** | **PROVEN.** Everything the invariants require. |
| 2 | Custom aspect / entity | Not needed. Would require a fork of the entity registry. |
| 3 | Per-claim-keyed structured properties | Not needed, and would pollute the property list unboundedly (§8). |

**Assertions are NOT Cloud-only.** This was the first thing checked, because it is exactly
the `dataQualityCheck` / `anomalies` trap from Session 0 that cost hours. `Assertion` is in
Core's entity registry at v1.5.0.6, `AssertionType.CUSTOM` is accepted, and the writes
below all succeed against a local quickstart with no token.

### The invariant, measured

```
[3] NO-OVERWRITE — both claims listed on the dataset (THE INVARIANT)
  dataset.assertions.total = 2
    - attest-probe-b7eb3a685d786  type=ATTEST_GROUNDEDNESS_CLAIM  :: analytics.customers.customer_profile contains PII
    - attest-probe-64400a45410cb  type=ATTEST_GROUNDEDNESS_CLAIM  :: column email of customer_profile is a STRING
  PASS  BOTH claims present — neither overwrote the other
```

Two claims. One dataset. Both retrievable, independently, by their own IDs. The thing that
does not work today.

### The history, measured — and it revises a CLAUDE.md premise

```
[4] VERDICTS — all three, and the HISTORY that a last-write-wins field cannot hold
  claim A run events: total=2
    - native=FAILURE  verdict=Contradicted  run=run-NEW  reviewer=bob@example.com
    - native=SUCCESS  verdict=Supported     run=run-OLD  reviewer=alice@example.com
  PASS  re-auditing a claim APPENDS — the earlier verdict SURVIVES
  PASS  the flip Supported->Contradicted is readable from DataHub ALONE
```

CLAUDE.md §2c says *"DataHub is the catalog, not the event store"*, because
`structuredProperties` are last-write-wins and *"a last-write-wins field has no events in
it."* **That is true of structured properties and NOT true of DataHub.**
`assertionRunEvent` is a **timeseries aspect**: it appends. The exact question CLAUDE.md
names as unanswerable — *"was this ever contradicted before someone fixed the tag"* — is
answerable from DataHub alone, above.

**The premise was right about the mechanism and overgeneralized to the product.** §7 says
what this does and does not change about the store.

### The three verdicts survive, structurally

`AssertionResultType` has four values (`INIT`/`SUCCESS`/`FAILURE`/`ERROR`) and Attest has
three verdicts. They do not line up, and CLAUDE.md §3 forbids collapsing
Insufficient-Coverage into either direction. Measured:

```
  claim B (Insufficient-Coverage): succeeded=0 failed=0
  PASS  the THIRD verdict is in NEITHER bucket — it stays third
```

Mapping Insufficient-Coverage to `ERROR` puts it in **neither** DataHub's passed nor its
failed rollup — which is exactly correct: the catalog being silent is not a pass and not a
fail. The third verdict stays third *in DataHub's own aggregation*, not merely in ours.

> **The native type is a LOSSY PROJECTION. `nativeResults['attest.verdict']` is
> AUTHORITATIVE** and carries the verdict verbatim. `ERROR` is the least-wrong native
> projection, not the verdict. Any reader that treats `result.type == ERROR` as "Attest
> broke" is reading the projection, not the answer.

### Cross-catalog query, measured

```
  customType = ATTEST_GROUNDEDNESS_CLAIM -> total=2      PASS
  tags = urn:li:tag:Attest-Contradicted  -> total=1      PASS
```

### Four landmines the spike bought, each silent or misdirecting

These cost real debugging cycles. They are the reason this session spiked instead of
designing from docs.

1. **`fieldPath` makes a claim artifact permanently verdict-less.** Setting `fieldPath` on
   a custom assertion sets its `asserteeUrn` to a **schemaField** URN, but the
   `assertionRunEvent` aspect requires a **dataset** URN:

   ```
   msg=Invalid entity type urn validation failure (Required: [dataset]). Path: /asserteeUrn
   Urn: urn:li:schemaField:(urn:li:dataset:(...,customer_profile,PROD),email)
   ```

   The create **succeeds**. The read **looks perfect** — the column grain appears natively
   modelled. Only the verdict write fails, forever. **Never set `fieldPath`.** The grain
   goes in `logic` and is fully recoverable there. Repairable by re-upserting without it.

   Worse: **whether the trap fires depends on index timing.** Across consecutive spike runs
   it produced refusal, index-lag, *and* silent success (on a repaired artifact's stale
   assertee). A field that usually works is a worse trap than one that never does — which is
   why the spike **observes and does not assert** here, per the project's own rule that a
   flaky assertion on a live path is worse than none.

2. **`result.error` is accepted and silently discarded.** We wrote
   `error: {type: INSUFFICIENT_DATA, message: ...}`; the mutation returned `true`; it reads
   back `null`. So `AssertionResultErrorType.INSUFFICIENT_DATA` — which *looks* like the
   perfect home for verdict 3 — **cannot be used**. Loud-then-silent, the worst shape.

3. **`deleteAssertion` rejects CUSTOM assertions**: `Unsupported Assertion Type CUSTOM
   provided`, HTTP 500. Retraction goes through `DELETE /openapi/v3/entity/assertion/{urn}`
   (hard) or `batchUpdateSoftDeleted` (tombstone).

4. **`reportAssertionResult` reads an eventually-consistent index.** Called immediately
   after the upsert it fails with *"does not exist or is not associated with any entity"*.
   Pure latency; a retry clears it. Code that reports once and trusts the exception drops
   verdicts under exactly the conditions a real run creates them.

### Constraints the design must absorb

- **Only `customType` and `tags` are filterable on an assertion.** Measured:
  `entityUrn`, `fieldPath`, `description` all return `total=0`. Per-dataset listing goes
  through `dataset.assertions`, **not** through search.
- **There is no server-side dataset scoping in search at all** — nine candidate field names,
  all `total=0` against an unscoped baseline that returns everything. So the two server-side
  entry points are **disjoint**: `dataset.assertions` scopes but cannot filter;
  `searchAcrossEntities` filters but cannot scope. **This shapes the whole retrieval path —
  §5 states exactly how much is server-side and refuses to oversell it.**
- **Assertions do not support structured properties.** So the latest verdict is filterable
  only as a **tag**.
- `customType` filters accept **multiple values (OR)** — measured — which is what lets
  `claim_type` live there and still answer "every Attest claim".
- **The write is three operations and cannot be atomic — but all three are idempotent**, so
  recovery is repetition rather than a saga (§9a). This holds *only* because a timeseries row
  is keyed by `(urn, aspect, timestampMillis)` and Attest supplies a deterministic timestamp.

---

## 3. The claim artifact

One claim = one `Assertion` entity (`AssertionType.CUSTOM`) + N appended run events.

**The split is the design.** The assertion is the **claim** (stable: what is asserted about
what). The run event is the **verdict at a point in time** (append-only: what the catalog
said, who signed it off). Re-auditing the same claim appends an event; it does not mint a
second artifact. That is what turns a pile of writes into a history.

### Identity

```
urn:li:assertion:attest-<sha256(target_urn|claim_type|field_path|asserted_value)[:20]>
```

Deterministic and content-addressed. Two different claims about one dataset hash
differently and coexist — the invariant. The same claim re-audited next week hashes
identically and appends.

**Asserted value is IN the identity, deliberately.** "Owned by Alice" and "owned by Bob" are
different claims, not one claim with a changing value. A claim's *verdict* may change as the
catalog moves (that is the history); what it *asserts* may not — that is `revise.py`'s
subject rule, one layer out. **Open question for build (§9): a corrected claim gets a new
ID, so the correction's link to the original lives in `attest.corrects` rather than in the
URN.**

### Field mapping

| Invariant field | Where it lives | Filterable |
| --- | --- | --- |
| **stable claim ID** | the assertion URN | by URN |
| **claim type** | `customType` = `ATTEST_CLAIM_{FRESHNESS,OWNERSHIP,CLASSIFICATION,SCHEMA}` | **yes** (OR-able) |
| **asserted value** | `customAssertion.logic` (JSON) | no |
| **grain** (table/column) | `logic.grain` + `logic.field_path` — **never `fieldPath`** (landmine 1) | no |
| **target dataset** | `customAssertion.entityUrn`; the assertion is attached to it | via `dataset.assertions` |
| **verdict** (authoritative) | `runEvent.result.nativeResults['attest.verdict']` | no |
| **verdict** (projection) | `runEvent.result.type` — SUCCESS/FAILURE/ERROR | via `runEvents` counts |
| **verdict** (latest, queryable) | tag `urn:li:tag:Attest-{Supported,Contradicted,Insufficient-Coverage}` | **yes** |
| **evidence references** | `nativeResults['attest.evidence']` (dotted field paths + values) | no |
| **reviewer** | `nativeResults['attest.reviewer']` | no |
| **decision** | `nativeResults['attest.decision']` (accepted / rejected) | no |
| **policy identity** | `nativeResults['attest.policy_version']` | no |
| **snapshot identity** | `nativeResults['attest.snapshot_id']` | no |
| **run link** | `result.externalUrl` + `nativeResults['attest.audit_run']` | no |
| **checked at** | `runEvent.timestampMillis` | ordered |

`claim_type` lives in `customType` rather than only in `logic` because `customType` is one
of exactly two filterable fields, and "every contradicted **ownership** claim this week" —
the query `writeback.py` was written for — needs claim type **and** verdict to be filters at
once. Verdict is the tag; claim type is the customType. Time comes from the run event.

**Still no `attest.confidence`.** For the same reason as today: the verdicts are
deterministic, there is no confidence, and inventing one to look like an ML system is the
exact failure this project exists to catch.

---

## 4. How write-back changes

From *"overwrite the dataset's verdict"* to *"append a claim artifact"*. The **approval gate
does not move** — it is the same single call site.

```
service.approve(run_id, decisions)
  └─ (unchanged) FLAGGED/trajectory gate  -> refuse, write nothing
  └─ (unchanged) rehydrate + resume through the human_checkpoint NODE
  └─ (unchanged) accepted = {decisions where accept} & {still awaits_human}
  └─ for each accepted claim:
       writeback.write_claim_artifact(...)          # NEW - replaces write_verdict
         1. upsertCustomAssertion(urn=claim_id, ...)   # the claim        [idempotent]
         2. reportAssertionResult(urn=claim_id, ...)   # the verdict      [idempotent IFF
            timestampMillis = run.created_at, NEVER now() -- see 9a]      [RETRY: landmine 4]
         3. swap verdict tag (removeTag old, addTag new)  # latest verdict [idempotent]
  └─ (unchanged) store.record_decision(...) keyed by CLAIM INDEX

POST /audit/{run_id}/writeback                      # NEW - repairs a partial write
  └─ reads the STORED record; re-runs 1-3 for already-accepted claims.
     Idempotent, so repetition is the recovery. Touches no graph, approves nothing. §9a.
```

Everything that makes the write-back trustworthy is untouched and is *load-bearing here*:

- **Nothing is written until a human approves.** Still exactly one call site: the approval
  path. `POST /audit` still changes nothing in the catalog. No `?auto_approve=true`.
- **A decision writes back only what THAT decision settled** (Session 14's intersection
  with `awaits_human`). Now more important, not less: a re-decided claim would otherwise
  **append a duplicate run event**, and duplicates in an append-only log are indistinguishable
  from a real re-audit.
- **A failed write-back is reported as failed.** The approval stands; the store records what
  the catalog did. Now three operations can fail independently (§9).
- **A FLAGGED run is un-approvable** and reaches none of this.

The five old properties: see §6.

---

## 5. The retrieval path — the part that makes the artifact real

An artifact nobody can retrieve is unfinished. This is not optional and it is why the
constraints in §2 matter.

### From DataHub alone (what a second agent inherits — no Attest, no SQLite)

```graphql
# Every claim on a dataset, with its asserted value and grain.
query { dataset(urn: $urn) {
  assertions(start: 0, count: 50) {
    total
    assertions {
      urn
      info { description externalUrl customAssertion { type entityUrn logic } }
      runEvents(status: COMPLETE, limit: 20) {
        total failed succeeded
        runEvents { timestampMillis result { type externalUrl nativeResults { key value } } }
      }
    }
  }
} }
```

That single query returns, for each claim: its ID, what it asserts, its grain, every verdict
it has ever had, when, by which run, and who signed each one off. **From DataHub. Without
Attest running.**

```graphql
# Every contradicted ownership claim in the catalog.
query { searchAcrossEntities(input: {
  types: [ASSERTION], query: "*", start: 0, count: 50,
  orFilters: [{ and: [
    { field: "customType", values: ["ATTEST_CLAIM_OWNERSHIP"] },
    { field: "tags",       values: ["urn:li:tag:Attest-Contradicted"] }
  ]}]
}) { total searchResults { entity { urn ... on Assertion {
  info { customAssertion { entityUrn logic } } } } } } }
```

### How much is server-side? — measured, and deliberately not oversold

The thesis is **"retrievable from DataHub"**, and that is true. **"Queryable in DataHub"**
would be an overclaim, and the difference matters, so here is the exact line.

**The two server-side entry points are DISJOINT and do not compose:**

| Entry point | Scopes to a dataset | Filters verdict | Filters claim type | Filters reviewer / time |
| --- | --- | --- | --- | --- |
| `dataset.assertions(urn)` | **yes (server)** | no | no | no |
| `searchAcrossEntities([ASSERTION])` | **no** | **yes (server, tag)** | **yes (server, customType)** | no |

**There is no server-side dataset scoping for assertions.** Nine candidate field names —
`entityUrn`, `asserteeUrn`, `datasetUrn`, `entity`, `dataset`, `.keyword` variants, `urn`,
`assertee` — every one returns `total=0` while the unscoped baseline returns them all. That
is a genuine absence in the index, not a name I failed to guess. Measured in the spike, §7.

So for the compound question — *"all contradicted ownership claims on this dataset caught
this week"* — **1 of 4 predicates is server-side:**

| Predicate | Where |
| --- | --- |
| on this dataset | **DataHub** (`dataset.assertions`) — the most selective one |
| contradicted | Attest (client-side over the response) |
| ownership | Attest |
| this week | Attest |

Drop the dataset and it inverts — *"every contradicted ownership claim this week, catalog-wide"*
is **2 of 3 server-side** (`customType` + `tags`), with time client-side.

**What keeps this honest rather than embarrassing:** the client-side part is a filter over a
**single already-narrowed response**, not a scan and not N+1. Both entry points return the
run events **inline** — measured — so one GraphQL round trip yields dataset, verdict,
reviewer, and timestamp for every candidate, and the filtering is a list comprehension over
≤50 rows. There is no pagination loop and no per-claim fetch.

**The honest summary, and the plan will not say more than this:** *DataHub does the scoping;
Attest does most of the filtering.* The data is entirely in the catalog and comes back in
one query — but the index will not answer "contradicted ownership on this dataset this week"
by itself, and pretending otherwise would be exactly the kind of unfounded claim this project
exists to catch.

**The thesis question is unaffected, and it is worth separating.** *"Show me what the next
agent inherits from DataHub alone"* needs **no filtering at all** — it is
`dataset.assertions(urn)`, one query, fully server-side, complete answer. The weaker part is
*filtering*, not *inheriting*. Those are different claims and only one of them is compromised.

`GET /claims` therefore reports which predicates it pushed down, in the response. A caller
that cannot see where the filtering happened cannot judge what it costs.

### Does the third verdict survive the round trip legibly?

Yes — verbatim. But there is a trap next to it, and the spike measures both (§6):

| State | `runEvents.total` | `succeeded` | `failed` | `result.type` | `attest.verdict` |
| --- | --- | --- | --- | --- | --- |
| Supported | 1 | 1 | 0 | `SUCCESS` | `"Supported"` |
| Contradicted | 1 | 0 | 1 | `FAILURE` | `"Contradicted"` |
| **Insufficient-Coverage** | 1 | **0** | **0** | `ERROR` | `"Insufficient-Coverage"` |
| **half-written** (upsert ok, report failed) | **0** | **0** | **0** | — | **absent** |

**Insufficient-Coverage reads back as the literal string `Insufficient-Coverage`.** The
three-verdict distinction survives; it is not merely parked in a neutral bucket.

**The trap: Insufficient-Coverage and a half-written claim BOTH read `succeeded=0 failed=0`.**
A reader consulting only DataHub's rollup counts cannot tell *"the catalog is silent about
this claim"* from *"Attest never finished writing it"* — two completely different facts with
two completely different fixes, and the first is a valid final verdict while the second is a
bug. **The discriminator is `runEvents.total`** (≥1 vs 0), equivalently the presence of
`attest.verdict`. This is written down because it is the exact shape of the mistake this
project exists to catch: absence read as an answer.

**The rule for every reader, including the UI:**
`attest.verdict` present ⟺ the claim has a verdict, and its value IS the verdict.
`attest.verdict` absent ⟺ the write never completed. Never infer a verdict from
`result.type` or from the rollup counts.

**The one real loss, named:** in **DataHub's own UI**, an Insufficient-Coverage claim renders
as an *error*, because `ERROR` is the only native type that avoids asserting a direction and
`result.error` is silently discarded (landmine 2), so it cannot even be labelled
`INSUFFICIENT_DATA`. Attest's own surfaces are unaffected — they read `attest.verdict`.
**Mitigation:** the assertion `description` leads with the current verdict
(`"Insufficient-Coverage — <claim>"`) and is refreshed on each write, so DataHub's native UI
shows something true in the one place the native type lies. That is presentation, exactly as
`polarity.py` is presentation: the authoritative verdict is carried verbatim and the lossy
projection never feeds back into it.

### From Attest's API (the UI and the convenience path)

Two new read endpoints, thin projections of the above — **no new state**:

| Endpoint | Returns |
| --- | --- |
| `GET /claims?target_urn=&verdict=&claim_type=&reviewer=&since=` | Claim artifacts, filtered. `target_urn` → `dataset.assertions`; `verdict`/`claim_type` → the indexed search; `reviewer`/`since` → applied client-side over the run events, and the response says so. |
| `GET /claims/{claim_id}` | One claim: asserted value, grain, and its full verdict history. |

**These read DataHub, not the store.** That is deliberate and it is the whole thesis: if the
API answered from SQLite, the demo would prove nothing about what a second agent inherits.
The store answers a different question (§7).

**The MCP path is where this lands next**, and it is why the boundary is a query rather than
a report: an agent asking "what is known about this dataset" gets claims, verdicts, and
provenance out of the catalog it already talks to.

---

## 6. Migration and compatibility

**Trivial, and the reason to say so explicitly is that it is trivial *by luck*.**

- The five old properties (`attest.verdict`, `.claim_type`, `.checked_at`, `.source_agent`,
  `.audit_run`) exist today on exactly two datasets, from prior live rehearsals — measured,
  not assumed:

  ```
  datasets carrying attest.verdict today: 2
    attest_db.public.support_tickets   verdict=Contradicted claim_type=ownership       run=2535ed86…
    attest_db.public.hr_headcount      verdict=Contradicted claim_type=classification  run=bfc6b414…
  ```

  **This is the gap, on the live catalog, in five lines.** `hr_headcount` says
  `Contradicted`. Contradicted about *what*? The property cannot say. The `audit_run` UUID
  points into a SQLite file the next agent cannot open. That is the whole problem statement,
  and it is already sitting in the catalog.

  `just seed` rebuilds the catalog and they are gone. Nothing reads them but
  `writeback.find_datasets`.
- **They are dataset-level; claim artifacts are assertion-level. They cannot collide.**
- **Proposal: keep them, and keep them honest.** They remain a correct answer to a *different*
  question — *"what is the latest Attest verdict on this dataset, at a glance, in the
  dataset's own property panel"* — which the assertion list does not answer at a glance.
  They stay last-write-wins and that stays fine, because the claim artifact is now the
  subject-bearing record and the property is a convenience badge.
- **What must NOT happen: the property panel implying it is the whole story.** Today's
  description says "the latest verdict Attest reached about a claim an agent made about this
  dataset", which read as complete when it was one of N. The definition text should say the
  dataset may carry several claims and point at them. **A field that overstates its coverage
  is this project's characteristic bug** (cf. `faithfulness.py` over-claiming until Session
  13 said so).
- **No store migration.** The store schema does not change (§7).

---

## 7. What this does NOT change

This is a **write-back and retrieval** change. It touches nothing that decides anything.

- **The deterministic core.** Checkers, `policy.py`, date math, set membership: untouched.
  No LLM enters the verdict path. `NO_LLM_IN_THE_VERDICT_PATH` is unaffected — the artifact
  is written *after* a verdict exists, on the approval path, from code.
- **The verdict path, the three verdicts, the 12-cell matrix.** Unchanged. The
  `Insufficient-Coverage → ERROR` mapping is a **presentation projection at the catalog
  boundary**, exactly as `polarity.py` is a presentation guard: the authoritative verdict is
  carried verbatim and the projection never feeds back.
- **The explanation guards** (crosscheck, faithfulness, polarity): untouched.
- **The approval gate's human-in-the-loop nature.** One call site. Nothing lands unapproved.
  Session 14's re-park loop and index-keyed decision log are relied on, not relaxed.
- **The trajectory gate.** A FLAGGED run still writes nothing.
- **The benchmark.** No verdict changes, so no label changes and no re-measurement. The
  committed numbers stand.
- **The store, and this is the subtle one.** The spike shows DataHub *can* hold claim
  history, which weakens the "DataHub cannot be the event store" argument for
  `structuredProperties` — but **not** the reason the store exists. The store holds *Attest's
  own* record: which run saw which evidence, what the trajectory said, what the receipts
  cost, who was shown what when they approved. `approvals` stays append-only. The catalog
  gets what belongs in a catalog; the auditor keeps its own books. **What changes is that
  the catalog is no longer the ONLY place a second agent can look and find nothing.**

---

## 8. Why not candidates 2 and 3

- **(2) Custom aspect / entity.** Would need a change to Core's entity registry — a fork, or
  a plugin the quickstart does not load. Candidate 1 already provides identity, grain,
  payload, append-only history, and two filterable dimensions. The extra control buys
  nothing the invariants ask for.
- **(3) Per-claim-keyed structured properties.** Would work mechanically — mint
  `attest.claim_<hash>` per claim — and is the wrong shape: an **unbounded** number of
  property *definitions*, one per claim ever made, in a global namespace, each needing
  bootstrap, none of them removable in practice, and the property list on a busy dataset
  becoming unreadable. It also still has no history: each key stays last-write-wins.
  Candidate 1 is what an assertion *is*.

---

## 9. Effort, and where it will break

**Estimate: two build sessions**, matching expectations.

- **Session A — write path.** `client.py`: three mutations (+ retry for landmine 4).
  `writeback.py`: `write_claim_artifact` replacing `write_verdict`, claim-ID derivation, tag
  swap. `service.py`: one call site changes. Offline tests against a fake; a live test that
  approves two claims on one dataset and reads both back.
- **Session B — read path.** `GET /claims`, `GET /claims/{id}`, the client-side filter
  boundary, the UI, and the demo that answers the sharp question.

### Where it is likely to break, in order

1. **The index lag (landmine 4) reaching the approval path.** The retry loop is the fix, but
   the failure lands *inside a human's approval*, and a 60s poll inside an HTTP request is
   not acceptable. **This is the real design risk of the build**, and the honest options are
   a bounded retry with the write-back reported as failed (the existing `WriteResult` contract
   already handles this — the approval stands, the catalog does not know, the store records
   which) or moving the write-back off the request thread. Recommend the former: it reuses a
   contract that already exists and does not invent an async path.
2. **Three operations where there was one — RESOLVED BY MEASUREMENT, see §9a.** It does not
   need a saga. The three writes are all idempotent, so a partial failure is repaired by
   repeating it. The remaining work is making the partial state *visible* and *re-runnable*,
   which is four small things and one hard rule.
3. **Someone sets `fieldPath`.** It looks exactly right, the write succeeds, the read looks
   perfect, and it *sometimes* works. This needs a test that breaks it, in the project's
   tradition — assert the artifact is written without `fieldPath`, because the runtime
   symptom is timing-dependent and therefore untrustworthy as a gate.
4. **The offline suite cannot see any of this.** Landmines 1–4 are all server behaviours: a
   fake has no index, no timeseries aspect, and no MCP validation. This is the Session 5 rule
   exactly — *a fake cannot fail in a way the real thing fails through machinery the fake does
   not have.* **`just live` is the only evidence for this feature.** The offline tier proves
   the shape of the payload; it cannot prove the write lands.
5. **Assertion count on a busy dataset.** One artifact per distinct claim, forever. A dataset
   claimed about in many ways accumulates assertions and `dataset.assertions` is paged at 50.
   Not a v1 problem; name it before it is one.
6. **The verdict tag needs bootstrapping**, like the structured properties do today —
   `createTag` for three verdict tags, idempotently, in `ensure_definitions`' successor.

---

## 9a. The non-atomic write-back, decided

The old `write_verdict` was **one atomic mutation, deliberately**: *"a verdict written as
five separate mutations can half-fail, leaving a dataset carrying Attest's verdict but not
the run id."* The artifact write is **necessarily three** — upsert (the claim must exist
before a result can be reported against it), report (the verdict), tag (the filterable
latest verdict) — and **cannot** be made atomic. That is a real integrity risk and it is
the one thing in this design worth deciding consciously.

**It does not need a saga, and the reason is measured, not argued.**

### The finding that dissolves it: all three writes are idempotent

A timeseries row is keyed by **`(urn, aspect, timestampMillis)`**. Report the same event at
the same timestamp three times and you get **one** run event, not three (spike §5):

```
[5] IDEMPOTENCE — is a partial write re-runnable, or does it need a saga?
  reported the SAME verdict at the SAME timestamp (1784100000000) three times
    -> 1 run event at that ts; attempt=3 verdict=Contradicted
  PASS  3 identical reports collapse to ONE run event (retry is SAFE)
```

So: **upsert** is idempotent by definition, **tag** is idempotent by set semantics, and
**report** is idempotent by key. Re-running the entire write-back is a no-op on whatever
already landed. **Recovery is repetition.** No compensating transactions, no outbox, no
ordering state machine — none of the Session 14 saga territory.

### THE RULE THIS BUYS, AND IT IS LOAD-BEARING

> **`timestampMillis` MUST be the run's stored `created_at`. NEVER `now()`.**

With `now()`, every retry mints a new key, **appends a duplicate run event**, and corrupts
the append-only history — a retry inventing an audit that never happened, inside the log
that exists to record what did. It is invisible until the retry path runs, every test
passes, and the corruption is indistinguishable from a real re-audit. This is the
None-is-not-zero receipt bug's exact shape (§2d), and it is why the rule is stated as a
rule rather than left to whoever writes the call.

`checked_at` is already stored and already passed to `write_verdict` today, so this costs
nothing — it only has to not be thrown away.

### The three questions, answered

**(a) Upsert succeeds, report fails — is the claim visibly incomplete, or silently
verdict-less?** **Visibly incomplete.** Measured (spike §6): the artifact exists with
`runEvents.total = 0` and **no `attest.verdict`**. The retrieval path renders it as
`INCOMPLETE`, never as a claim without an opinion. And since Attest writes only on approval,
`total = 0` ⟺ half-written — there is no benign cause to confuse it with.

The trap sits right here and §5 names it: an Insufficient-Coverage claim **also** reads
`succeeded=0 failed=0`. `runEvents.total` is what separates "the catalog is silent" from
"the write never finished". A reader on the rollup counts conflates a valid verdict with a
bug.

**Ordering is chosen so partial states degrade downward, not falsely:**

| Fails at | Catalog state | How it reads |
| --- | --- | --- |
| upsert | nothing exists | no artifact — absent, nothing false |
| report | claim, no verdict | **INCOMPLETE** — visible, honest |
| tag | claim + verdict, stale/absent tag | per-dataset read is **correct**; the cross-catalog query misses it |

The tag failure is the subtle one: the claim is right, the *index* is stale. **The tag is a
derived search accelerator, never the truth** — the authoritative verdict is in the run
event, so a stale tag degrades *findability*, not correctness. The store records that the
tag step failed, and the retry repairs it.

**(b) Is a failed write retryable by re-running the same approval?**

**No — and this is a real pre-existing finding, not something this design introduces.**
Read off `service.approve`:

```python
awaiting = {c.index for c in stored.claims if c.correction.awaits_human}   # line 287
accepted = {d.claim_index for d in decisions if d.accept} & awaiting       # line 288
```

Once a claim is decided it is no longer `awaits_human`, so a re-approve **excludes** it and
writes nothing. If it was the last proposal the run went `COMPLETE`, and line 241 then
refuses the call outright (`NotResumable` → 409). **Today, a failed write-back strands:**
the store honestly records `ok=False`, the approval stands, the catalog never learns, and
**there is no supported path to try again.** That is tolerable with one atomic mutation that
rarely half-fails. With three operations and a retryable index lag (landmine 4) it is not.

So the minimum includes **one small new thing**: a retry path that is **not** the approval
path.

```
POST /audit/{run_id}/writeback     # re-run the write-back for accepted claims
```

It reads the **stored record**, finds claims that were accepted, and re-runs the three
idempotent calls. **It does not touch the graph, the checkpointer, or the human checkpoint,
and it must not** — the human decision already happened and is recorded; re-executing a
recorded decision's side effect is not a new decision. There is no second path to approving
anything, which is the thing that must never have one (§2d).

**This does not weaken "nothing is written until a human approves it."** The endpoint can
only write what a human already accepted, read from the append-only record. It cannot accept
anything, and a claim that was never accepted is not reachable from it. A `FLAGGED` run is
refused here exactly as it is in `approve`.

Because every step is idempotent, it may simply re-run **all** accepted claims rather than
tracking which step failed — correctness does not depend on the bookkeeping. It filters to
recorded failures only to save network calls.

**(c) Does retrieval distinguish fully-written from half-written?** **Yes**, measured, and
it is free: `runEvents.total = 0` / `attest.verdict` absent. `GET /claims/{id}` returns an
explicit `status: complete | incomplete`, and the UI renders `incomplete` as a visible
pending state with the retry available — not as a claim that mysteriously has no verdict.

### The minimum, and it is less than an outbox

1. **`timestampMillis` = the run's `created_at`, never `now()`.** The rule everything rests
   on. Needs a test that breaks it: report twice, assert one event.
2. **`WriteResult` names the step** — `upsert | report | tag` — and the outcome. "Written"
   becomes a per-step result, not a boolean. Same shape as `CorrectionOutcome` naming six
   outcomes rather than a success flag: a boolean would hide exactly the failure modes this
   feature introduces.
3. **`POST /audit/{run_id}/writeback`** — re-runs the idempotent write from the stored
   record. Not the approval path, and cannot approve.
4. **Retrieval renders no-verdict as INCOMPLETE.** Free; only has to not be skipped.

**That is the honest bar the author asked for: partial failure is visible and re-runnable,
not invisible and stranded.** It is achievable at this scope, and it is strictly smaller
than the transactional work ruled out in Session 14 — because the server's own key semantics
do the work a saga would otherwise have to.

### What is still not guaranteed, said plainly

**There is no atomicity and this design does not pretend to add it.** A crash between report
and tag leaves a stale index until someone retries, and **nothing detects that automatically**
— no reconciler, no sweeper. The failure is *recorded* (the store says which step failed) and
*repairable* (one call), but it is not *self-healing*. A production deployment would want a
periodic reconcile comparing each claim's latest run event against its verdict tag; this is a
hackathon build, and it says so rather than shipping a sweeper nobody exercises. Naming the
boundary is what makes the rest credible.

---

## 10. The question, answered

> "Two claims about one dataset are approved. Show me what the next agent inherits from
> DataHub alone."

**Today:** an opaque run UUID pointing at application state it cannot reach.

**After this:** two independently-addressable claim artifacts on the dataset, each carrying
what it asserted, at what grain, the verdict, the evidence, who approved it and when, every
prior verdict it has ever had — and the whole set is one GraphQL query against the catalog,
with no Attest process running.

Measured on the pinned server before it was designed.
