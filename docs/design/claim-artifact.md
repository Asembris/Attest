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
- **Assertions do not support structured properties.** So the latest verdict is filterable
  only as a **tag**.
- `customType` filters accept **multiple values (OR)** — measured — which is what lets
  `claim_type` live there and still answer "every Attest claim".

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
         1. upsertCustomAssertion(urn=claim_id, ...)   # idempotent; the claim
         2. reportAssertionResult(urn=claim_id, ...)   # append; the verdict   [RETRY: landmine 4]
         3. swap verdict tag (removeTag old, addTag new)  # the filterable latest verdict
  └─ (unchanged) store.record_decision(...) keyed by CLAIM INDEX
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

Filter by **verdict** (tag) and **claim type** (customType) — both measured. **Reviewer and
time are NOT filterable** (§2); they are *retrievable* per claim from the run events, so
"filter by reviewer" is a client-side filter over a dataset's or a verdict's claims, not a
server-side one. **Stating that plainly is the point** — the honest boundary, named, rather
than a promise the index will not keep.

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
2. **Three operations where there was one.** `write_verdict` was one atomic mutation by
   design — *"a verdict written as five separate mutations can half-fail, leaving a dataset
   carrying Attest's verdict but not the run id."* The artifact write is **necessarily** three
   (upsert, report, tag) and **cannot** be atomic. A partial failure leaves a claim with no
   verdict, or a verdict with a stale tag. `WriteResult` must name *which step* failed;
   "written" becomes a per-step outcome, not a boolean. **This is the biggest honesty risk in
   the build** — the same shape as `CorrectionOutcome` naming six outcomes rather than a
   success flag.
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

## 10. The question, answered

> "Two claims about one dataset are approved. Show me what the next agent inherits from
> DataHub alone."

**Today:** an opaque run UUID pointing at application state it cannot reach.

**After this:** two independently-addressable claim artifacts on the dataset, each carrying
what it asserted, at what grain, the verdict, the evidence, who approved it and when, every
prior verdict it has ever had — and the whole set is one GraphQL query against the catalog,
with no Attest process running.

Measured on the pinned server before it was designed.
