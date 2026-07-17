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

- **Python 3.12**, LangGraph, FastAPI, SQLite (stdlib `sqlite3`, no ORM).
- **`langgraph-checkpoint-sqlite` is PINNED to `==3.1.0`.** It is the one real version risk
  in the tree — a separate package on its own release cadence, and it owns the tables a
  parked run is resumed from. See §2d.
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
- [faithfulness.py](src/attest/faithfulness.py) proves **lexical provenance, not semantic
  faithfulness, and the name over-claimed until Session 13 said so.** Every factual token in an
  explanation must appear in the evidence — matching by **contiguous word sequence** (so `PII`
  does not match inside `NonPII`, and `customer_email` cannot be assembled from
  `customer_profile` + `email`), capitalized words are a factual class, the guard **fails
  closed**, derived numbers are rejected even when correct. What it CANNOT catch, and never
  could, is a fluent lie told in ordinary prose that names no false fact — "the catalog supports
  the claim" beside a Contradicted verdict passes every token check. That hole was reproduced on
  the real path; it is closed by polarity.py, not here.
- [polarity.py](src/attest/polarity.py) (Session 13) closes the fluent-lie hole. **The
  deterministic reason is the authoritative explanation; model prose may assert only the
  DIRECTION the verdict reached** — Supported affirms, Contradicted denies, Insufficient-Coverage
  is silent. Prose that affirms where the verdict denies (or the reverse, or claims silence over
  a definite verdict) is rejected and the template ships. It reads PROSE, not identifiers (a tag
  named `Verified` inside a URN is faithfulness.py's charge, not a relational verb), handles
  negation ("does not support" is a denial) and the "neither confirmed nor denied" SILENT idiom
  the checkers actually use. It is a *presentation* guard: the polarity that ships comes from the
  verdict, always. Not a better token filter — a lexical filter cannot prove entailment, so it
  stops the model asserting a direction at all, rather than trying to prove the one it asserted.
- [explain.py](src/attest/explain.py): three gates decide whether model prose ships —
  crosscheck, faithfulness, polarity — and any one failing falls back to a **deterministic
  template** built from the checker's reason and evidence. A failed explanation degrades to
  something *true*, never to something plausible. The template must itself pass the guards — keep
  its scaffolding lowercase (capitalized-word rule), and it leads with the verdict word so it is
  polarity-safe by construction.
- [crosscheck.py](src/attest/crosscheck.py): the model reports the verdict it reads and the
  fields it cited. Disagreement never changes the verdict; it is surfaced as a `Conflict`. Note
  its limits, which is WHY polarity.py exists: it only compares the model's self-reported
  `implied_verdict` and cited fields — a model that reports the RIGHT verdict and cites nothing,
  then writes prose asserting the opposite, sails through it. crosscheck catches a dishonest
  self-report; polarity catches a dishonest sentence.

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
- **A trajectory violation is a HARD GATE, not an alarm (Session 13).** It used to be logged
  and the report stored, served, and approvable anyway — so the strongest claims the system
  makes were enforced only by whoever happened to read the log. Now a violating run is
  `RunStatus.FLAGGED` ([report.py](src/attest/report.py)), set in `graph._report`, which
  overrides both COMPLETE and AWAITING_REVIEW and makes the run **un-approvable**:
  `service.approve` refuses it (`TrajectoryViolation` → HTTP 409) before any rehydrate,
  resume, or write-back. A report the pipeline could not vouch for cannot reach the catalog.
  `tests/test_api.py` tears out the guard node, confirms the run is flagged, and confirms the
  approval is refused with nothing written.
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

**2c. The service (Session 4). The snapshot cache is a CONSISTENCY boundary, and the
checkpoint does not soften because there is now an HTTP surface.**

- [datahub/cache.py](src/attest/datahub/cache.py) — **the cache exists for correctness
  first and speed second, and if it were only about speed it would be optional.** Without
  it, two claims about one dataset in one audit are checked against two separate reads: if
  a tag changes between them, Attest returns "contains PII: Supported" and "PII-free:
  Supported" in the same report, each correct against the catalog as it saw it, and the
  report is incoherent. A run resolves each entity **once**, and every claim in it is
  decided against that one snapshot. That generalizes the rule revise.py already had (a
  revision is re-checked against the same snapshot, never a re-fetch) from one claim to the
  whole run.
- **Cross-run caching would be a LIABILITY, not an optimization. Do not add it.** A
  snapshot carried into a new audit means Attest verifying today's claim against a catalog
  that no longer exists — the exact failure this project was built to catch, committed by
  the tool that catches it. **Cache within an audit for consistency; always re-fetch for a
  new audit for correctness.** Redis buys cross-process sharing and there is no
  cross-process requirement: the only reader of a run's snapshots is the run itself. A dict
  wins.
- **The scoping is structural, not disciplinary.** The cache is created inside
  `Pipeline.run()`, lives on that run's `_Ledger`, and dies with it. `Pipeline` holds no
  cache attribute, so there is no object a second run could reach. Sharing one across runs
  is not forbidden by a convention — it is unreachable from the code. `tests/test_cache.py`
  asserts a second run re-fetches and sees a catalog that changed between runs.
- **MEASURED:** 4 claims over 2 datasets = **2 fetches** (`just live`). Projected at the
  nominal workload — 1000 claims/day over 50 datasets — **1000 fetches becomes 50**
  (`cost.project_fetches`, pinned in tests). The dollars were never the operating cost; the
  load on someone else's GMS was.
- [store.py](src/attest/store.py) — **the audit history lives in Attest's DB, and DataHub
  is the catalog, not the event store.** DataHub's `structuredProperties` are
  **last-write-wins and unversioned**: write a verdict and yesterday's is *gone*, not
  superseded. That is right for "what is true of this dataset now" and useless for every
  question an auditor is for — has this agent's accuracy improved, was this ever
  contradicted before someone fixed the tag, who approved this correction and what evidence
  were they shown. Those are questions about **events**, and a last-write-wins field has no
  events in it.
- **`approvals` is append-only.** Re-deciding a claim writes a second row. Overwriting
  would reproduce, inside Attest's own store, the very property that disqualified DataHub
  from holding the history.
- Plain SQL, no ORM, TEXT/INTEGER/REAL only, ISO-8601 timestamps, 0/1 booleans — the DDL
  moves to Postgres by changing the connection. Anything you would filter BY (verdict,
  claim type, URN, outcome, review status, timestamp) is a real indexed column; only
  read-whole payloads are JSON blobs.
- [writeback.py](src/attest/writeback.py) — on approval, each claim is written back as its
  own **claim artifact**: a CUSTOM DataHub **Assertion** at a URN Attest derives from the
  claim's content, plus one appended `assertionRunEvent` per verdict. **See §10 — this
  replaced the five dataset-level structured properties as the audit record.** The five
  properties are still written as a *glance badge* on the dataset, and they stay
  last-write-wins, which is now honest rather than lossy: the subject-bearing record is the
  artifact, and the badge points at it.
- **There is deliberately no `attest.confidence`.** Attest's verdicts come from
  deterministic code; there IS no confidence, and the third verdict already carries the only
  uncertainty in the system (about the *catalog's coverage*, not about the answer). Writing
  `confidence: 0.95` would be inventing a number to look like an ML system — a fabricated
  figure reported as a measurement, which is precisely what this project exists to catch.
- **Nothing is written to DataHub until a human approves it.** Write-back is called from
  exactly one place: the approval path. `POST /audit` changes nothing in the catalog, ever,
  and `tests/test_api.py` asserts it. There is no `?auto_approve=true` and no "approve all";
  an HTTP surface is exactly where that would have been traded away for convenience.
- **The CHECKPOINT IS A LOOP, and PENDING has to stay reachable (Session 14).** The only
  edge out of `human_checkpoint` is the one that finds nothing pending; anything still
  PENDING routes straight back to it, where `interrupt_before` parks the run again. It used
  to be an unconditional edge to ASSEMBLE, so a partial approval — or an empty one, which
  the API explicitly documents as legal — settled the run COMPLETE and the service then
  deleted its checkpoint. The unnamed proposals stayed PENDING exactly as promised and
  became **permanently un-decidable**: a COMPLETE run is not resumable, so the next approval
  was a 409. "Nothing is accepted by default" is only an accountability guarantee for as
  long as accepting it LATER is still possible. The frontend hid this by requiring every
  proposal decided before submit; the public API contract was broken regardless.
  `test_api.py` decides one of two proposals and asserts the run is still parked, still
  AWAITING_REVIEW in the store, and finishable by a second call.
- **A decision writes back only what THAT decision settled.** Re-deciding became reachable
  the moment runs started re-parking, so `approve` intersects the accepted indices with what
  was still `awaits_human` in the record **as it stood before the resume**. The checkpoint
  node already skips an already-reviewed proposal (a correction is settled once); the
  write-back mirrors that rule rather than re-deriving it, or naming an already-accepted
  claim would replay its catalog write on every later call.
- **The decision log is keyed by CLAIM INDEX, not by target URN.** Two accepted claims can
  name one dataset. Keyed by URN, every decision about that dataset inherited the last
  write's result — so a REJECTED decision was recorded as having been written to the
  catalog. A false entry in the append-only record of who decided what, inside the store
  that exists *because* DataHub cannot hold this history.
- **A failed write-back is reported as failed.** The approval still stands — a human did
  decide — but the catalog does not know, and the store records which. A silent failure
  would leave DataHub disagreeing with the audit history and nobody any the wiser.
- **`target_urns` is a PRECONDITION and it is now enforced in BOTH halves (Session 14).** It
  was validated and then dropped by the route: the caller supplied a constraint that did
  nothing. It is honored rather than deleted, because a working precondition is worth more
  than a field that lies, and the enforcement is two-sided by necessity. Half is knowable at
  request time (the URN must appear verbatim in `agent_output`, or no claim could be about
  it — schemas.py). The other half is only knowable after the decomposer runs: a claim must
  actually have been extracted for each declared URN (`service._require_coverage`). Both
  answer to **422**. It is still **never a filter** — it can demand more coverage, never
  less; letting a caller narrow an audit would let them hide a claim from the auditor.
- **The refused audit is STILL RUN AND STILL STORED, and the 422 names it.** The run read
  the catalog, spent tokens and reached verdicts; the caller's precondition failing is a
  fact about that run, not grounds to pretend it never happened. The error carries
  `GET /audit/{run_id}` and `test_api.py` follows it and asserts the evidence is there.
- **A claim that came back as a `ClaimError` COUNTS as covered.** The precondition is about
  coverage, not verdicts, and an unresolvable URN is a loud outcome sitting in `errors`.
  The silent case — no claim extracted at all — is the one worth refusing, because it is
  the one nobody sees. (This is the same instinct as the benchmark's first real bug: a
  claim the decomposer quietly dropped.)
*(Session 4 serialized audits behind a lock and 409'd a run parked by a dead process. Both
were resolved in Session 5 — see §2d, which supersedes them.)*

**2d. Durable resume and per-run token billing (Session 5). Both were closed by REMOVING a
shared thing, not by guarding it.**

- **`langgraph-checkpoint-sqlite` is PINNED (`==3.1.0`), and it is the one real version
  risk in the tree.** It is a separate distribution on its own release cadence against
  `langgraph>=1.0` — already three major versions of its own — and it owns the tables a
  *parked run* is resumed from. A minor bump that reshapes them turns "approve this
  correction" into a 409 for every run parked before the upgrade, and the audit looks fine
  right up until someone tries to sign it off. Bump it deliberately, with
  `tests/test_resume.py` green, or not at all.
- **TWO durable things, from two places, and only one of them is LangGraph's.** The paused
  *graph* is durable because the checkpointer is; the typed *ledger* is durable because the
  store already was (verdicts and evidence are written the moment a run returns, parked or
  not). `Pipeline.rehydrate` puts them back together — [replay.py](src/attest/replay.py)
  rebuilds the ledger from the stored `AuditRecord`. The ledger still may NOT go into
  checkpointed state: the msgpack landmine is unchanged, and durable resume is precisely
  the path on which it would bite.
- **LangGraph's checkpoints live in their OWN database** (`ATTEST_CHECKPOINT_PATH`, default
  `attest-checkpoints.db`). LangGraph owns those tables and their shape moves with its
  releases; the audit history is Attest's schema and the evidence trail is in it. One file
  would put a dependency's migrations in the same blast radius as the evidence.
- **The resumed run goes through the `human_checkpoint` NODE, and the test that asserts so
  is the whole feature.** Applying the decision straight to the stored record would be half
  the code and would create a second path to the one thing in this system that must not
  have one — unaudited, invisible from outside, taken only after a restart. `test_resume.py`
  asserts `human_checkpoint` appears in the *resumed* trace, at the index the run parked
  at. Without that assertion, "durable resume" and "the Session 4 shortcut we refused" are
  indistinguishable from the outside.
- **"It resumes" was never the bar. "It resumes and the report is identical" is.** A
  restarted run that reports something subtly different is invisible, and it is on the path
  a human uses to approve a change to the catalog. So the resumed record is compared whole
  against an unrestarted run's, modulo the wall clock and the run's identity.
- **MEASURED, and the reason `record.py` grew four fields.** `StepRecord.models` was not
  stored. `Trace.cost` reports a run's dollars as `None` — never `0` — when a model that
  spent tokens has no price, and it finds those models *by name* off the step. Rebuild a
  trace without them and the unpriced set comes back empty, so a resumed run computes
  `usd = sum(...) = 0.0` where the original honestly said "unknown": **a restarted audit
  fabricating a cost figure the original refused to state.** That is None-is-not-zero
  breaking inside Attest's own receipts. Also added: the guard's `rejected` drafts, its
  `faithfulness_violations`, and `StepRecord.error`. And conflicts / dropped claims /
  injection findings became **structured pairs rather than rendered strings** — a `str()`
  cannot be parsed back, so a resumed run could not re-render them.
- **The store schema therefore CHANGED, and a pre-Session-5 database is REFUSED at open.**
  `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table, so an old DB would open
  cleanly and die on the first INSERT — inside a running service.

  **If you cloned this repo before Session 5 and it now fails at startup, the answer is one
  line: `rm attest.db attest-checkpoints.db` and re-run.** Both are gitignored dev state,
  nothing in DataHub is touched, and the next run rebuilds the schema. The `StoreError` says
  exactly this, by name, at open.

  **There is deliberately NO migration.** Three columns changed from a rendered string to
  the structure that produced it, and a string cannot be parsed back into the pair it came
  from — reconstructing them means Attest inventing the contents of its own audit trail,
  which is the one thing it exists not to do. A production deployment would need a real
  migration; this is a hackathon build, and it says so rather than shipping a migration that
  fabricates. Do not "fix" this by writing a lenient parser.
- **The lock is GONE, and what replaced it is not a smaller lock.** The LLM handle now lives
  on the `_Ledger` — one per run, forked via `LLM.for_run()`, sharing the HTTP transport and
  nothing else. Cross-billing is *unreachable* rather than *prevented*, exactly as cross-run
  snapshot reuse is (§2c). Everything else was already per-run or already thread-safe:
  ledgers and checkpointer keyed by thread id, cache on the ledger, store has its own lock,
  httpx is thread-safe.
- **The concurrency test is a test about the RECEIPTS, not about not-crashing.** Two audits
  through one service, with run A's first model call **held open across the whole of run B**,
  so B's tokens are all spent inside the window A's decompose step slices. Shared handle:
  **A bills 480 tokens for 240 tokens of work** (measured, by sabotage). Per-run handle: 240.
  A concurrency fix that silently cross-bills is worse than the queue it replaced. And if
  the lock ever comes back, B cannot start while A is held, so the fake **fails the run by
  name** rather than hanging the suite.
- **`forget` now deletes the run's checkpoints too.** A service is a long-lived process and a
  paused graph per audit, forever, is a leak. A completed run is not resumable, so it keeps
  no pause.
- **A 409 still exists, and it is the honest case:** the store says a run is awaiting review
  and the checkpointer has no paused graph for it (checkpoints wiped, or an in-memory saver
  across a restart). All the stored evidence is right there, which is exactly what would make
  faking the pause feel reasonable. It is still refused.

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

**`COMPLETENESS_REACHES_COLUMNS` (Session 6): the marker propagates DOWN even though signals
do not, and the asymmetry is the point.** A PII *signal* is a fact about the DATA, and
"contains PII" is existential — so a table's PII tag says nothing about its untagged
`signup_ts`. `Verified` is a fact about the REVIEW ("the team looked and tagged what they
found"), so a column they did not tag is a column they reviewed and found clean. Hence: an
untagged column of a **Verified** table is *Contradicted* for "contains PII"; the same
untagged column of an **unverified** table is *Insufficient-Coverage*. Same claim, same
shape; the only difference is whether anyone looked.

**This rule was IN THE CODE before it was in the policy, and that was the bug.** It lived as
a comment inside `check_classification` — a governance semantic buried in an `if`, which is
the exact thing policy.py exists to prevent. It was found by the benchmark's cross-family
labeler (§7): Nemotron, handed the whole declared policy, applied the propagation rule and
returned Insufficient-Coverage where Attest returns Contradicted. **It was not wrong** — it
had been told both rules and never told how they interact, because nobody had written that
down. That is what a second model family is FOR, and it is the only disagreement in 40 cases.

**7. The benchmark is the evidence, and it can fail.** [benchmark/](benchmark/README.md) —
40 hand-labeled claims, 26 hard, all 12 cells, a one-line rationale per label. The
invariants worth not rediscovering:

- **RAGAS and DeepEval are deliberately NOT used, and the reasoning goes in the README.**
  Both score an LLM-generated answer against retrieved context. Attest's verdicts are not
  generated — they are date math and set membership — so there is nothing to score. It is a
  deterministic 3-class classifier and the right tool is precision/recall/F1 against
  hand-labeled truth (`sklearn.metrics`). Worse, adopting a judge framework would *imply*
  the verdicts are LLM-judged when they explicitly are not, undercutting
  `NO_LLM_IN_THE_VERDICT_PATH` — the strongest claim the system makes. **A dependency that
  contradicts a core architectural principle is a liability, not credibility.**
- **Per-verdict metrics, never aggregate accuracy**, and the confusion matrix is *read*:
  Supported↔Contradicted is a CORRECTNESS failure (the worst thing this product can do);
  anything↔Insufficient is a COVERAGE failure (crying wolf, or certifying silence). Different
  bugs, different fixes.
- **Two modes, because "Attest got it wrong" is two different bugs.** `core` feeds the
  structured claim to the checkers (free, exact, no model). `--full` feeds the PROSE to the
  whole pipeline. When full is worse, `extraction fidelity` says whether the decomposer
  mis-transcribed the sentence. A case that is right by luck — wrong claim, right verdict —
  is counted correct AND named, because banking those flatters a broken decomposer.
- **pass@k is a BUG DETECTOR, not a score.** A deterministic checker cannot return two
  answers. pass@k < 100% on the core means a model leaked into the verdict path, and the
  harness diagnoses rather than counts: same extracted claim + different verdict = a LEAK
  (capitals); different extracted claim = decomposition variance, a different finding.
  MEASURED: 100% at k=5 (core) and k=3 (full).
- **MEASURED: 100% / macro-F1 1.000 on both core and full pipeline; $0.0138 per 40 claims;
  40/40 model-authored explanations, 0 guard rejections; 0 correctness and 0 coverage
  failures.**
- **The vacuity check RUNS IN THE SUITE, not only in a command someone has to remember.**
  `just bench-sabotage` exists and exits non-zero if the numbers do not move — but a
  guarantee that only fires when someone types it will rot, exactly like the `just live`
  cadence would without a written rule. So
  `test_benchmark.py::test_breaking_a_checker_collapses_the_benchmark` runs the sabotage on
  every `just check` and in CI: 100% → 67.5%, Supported precision → 0.536, correctness AND
  coverage failures both non-zero. It restores the dispatch tables via monkeypatch — record
  the healthy ones BEFORE sabotaging, or every test after it runs against a checker that
  affirms everything, which is a very funny way to make a suite pass.
- **100% IS THE EXPECTED RESULT, AND THAT HAS TO BE SAID BEFORE A JUDGE DECIDES OTHERWISE.**
  The checkers are deterministic code implementing exactly the rules the labels encode, so
  anything below 100% is a BUG, not a difficulty signal. The benchmark is a REGRESSION NET
  and a COVERAGE PROOF, not a capability score. Do not "make it harder" to get a more
  impressive number — make it FAIL on demand (above), and name the boundary (below).
- **What the benchmark does NOT prove, and the README says so plainly:** it is a SEEDED
  catalog, not a messy real one (no half-finished glossaries, no tags meaning three things to
  three teams, no genuinely contested classifications); the labels APPLY the policy and do not
  VALIDATE it (a wrong rule scores 100% while being wrong); and the catalog is both the oracle
  and the input, so it cannot tell you whether the catalog is right about the DATA. Naming the
  boundary is what makes the 100% credible instead of hollow.
- **ZERO of the 40 cases bypass the model in `--full` mode**, and the flattering read must be
  refused: the score is the model transcribing 40 sentences correctly AND the checkers
  deciding 40 claims correctly. **27/40 put a non-trivial demand on the decomposer** (8
  column-scoping, 8 negation, 7 numeric-window, 4 type-assertion, 1 fractional-window, 1
  term-urn); 13 need only the type and the URN. **And the core does NOT rescue a
  mis-transcription** — handed the wrong claim it faithfully decides the wrong claim. What it
  guarantees is that the VERDICT is never invented, and a mangled claim becomes a visible GAP
  (`No-Claim`) rather than a confident wrong answer. That is a different guarantee from the
  one people assume, and it is the honest one.
- **The benchmark found a real bug on its first run, and the fix was the CLAUDE.md rule.**
  "Updated every 30 minutes" came back from extraction as `max_age_hours: 0` (the model
  floored 0.5), which `FreshnessClaim` rightly rejects (`gt=0`) — so the claim was silently
  DROPPED. Every example in the schema's description was a whole number ≥ 24, so the model
  learned the field's shape from them. Widened the description; did **not** relax `gt=0`,
  which would admit a meaningless "within zero hours" claim. *Widen the evidence, never the
  guard.* 97.5% → 100%.
- **Cross-family calibration: Nemotron (Llama family), and the FAMILY is the point.** LLM
  judges favor their own outputs (GPT-4 ~10% higher win rate, Claude-v1 ~25%; Zheng et al.
  2023, arXiv:2306.05685), so letting GPT validate GPT-labeled ground truth would inflate
  every number by construction. Few-shot, because it raises judge self-consistency 65% →
  77.5% (same paper) — and the shots are **synthetic**, because a few-shot example drawn from
  the labeled set is answer-key leakage. It resolves its model through
  `settings.model_for(Step.CALIBRATION)` and calls it through `llm.py`, so both invariants
  hold. **It never touches the verdict path, and the pipeline default stays `gpt-4o-mini`.**
- **What calibration does NOT prove, and say so:** the labeler is given the same policy the
  labels encode, so agreement shows the labels *follow from* the policy — not that the policy
  is *right*. A wrong rule is applied wrongly by both and they agree. That is a design
  argument and it lives in policy.py.
- **THE JUDGE IS NOT DETERMINISTIC, AND THAT IS THE HEADLINE MEASUREMENT.** Two calibration
  runs are committed, both 39/40 (97.5%) at temperature=0, and the disputed case is DIFFERENT
  between them: the before-policy run disputed `class-15`; the after-policy run agreed on
  `class-15` (the tie-break rule had been declared) and disputed `class-03` instead — and
  `class-03` is a verdict the judge had AGREED with on the earlier run before flipping it. A
  dispute is not a stable property of a label; it moves. **Do NOT re-inflate this into "the
  dispute set reshuffles across N identical runs":** the two committed runs differ in prompt
  (the tie-break rule was added between them), so they do not demonstrate instability under
  identical conditions, and no six-run per-run receipts exist — that claim was RETRACTED
  (Session 12). The labeler runs k times (default 3) and separates "disputed in EVERY run"
  (real) from "disputed in SOME runs" (noise); it also reports judge self-consistency, a number
  about the JUDGE, not the benchmark. Every displayed calibration figure must trace to a
  committed receipt — `tests/test_calibration_consistency.py` fails otherwise, including on any
  arithmetically impossible row (an agreement % that disagrees with its dispute count).
- **This is the sharpest argument in the repo for the whole architecture.** A judge that
  returns Supported on `class-03` in one run and Contradicted in the next, at temperature=0,
  cannot BE ground truth; it can only calibrate it. Attest's core answers identically every
  time — pass@5, 100%. Measured here, not cited.
- **It found a real bug anyway, and that is worth more than the percentage**: `class-15`
  surfaced the undeclared tie-break (`COMPLETENESS_REACHES_COLUMNS`, above), a governance rule
  that lived in an `if` inside a checker. Declaring it is exactly why the after-policy run
  AGREES on `class-15` — the one dispute that was signal, not noise.
- **Disagreements are SURFACED, never silently resolved**, and the harness exits non-zero below
  90% MEAN agreement. **Do not "fix" a dispute by editing the label to match the model, and do
  not tune the prompt until it agrees.** After declaring the rule, both committed runs held at
  97.5% — `class-15` agreed but `class-03` newly disputed — so declaring it did not "raise the
  score", and chasing the new dispute would be the benchmark author's characteristic failure.
  Running it k times is what stops that; the one time declaring a rule was the right response to
  a dispute, it was because the rule was genuinely missing, not to chase agreement.
- **The labeler needs a request TIMEOUT and per-run persistence, and now has both.** The OpenAI
  SDK defaults to a 10-minute timeout with retries, so ONE wedged socket stalls a 40-case run
  for half an hour and looks exactly like a slow one — and with results written only at the
  end, the stall threw away every completed run with it.

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
- **DataHub fabricates a structured-property definition for any well-formed URN, and it
  breaks the WRITE path.** This is the twin of the `dataset(urn:)` / `exists` trap and it is
  nastier, because the read looks fine. `entity(urn:)` on an undefined
  `urn:li:structuredProperty:*` returns a **non-null** entity with a definition whose
  `qualifiedName` is the **empty string**. A bootstrap that asks "is this property defined?"
  and trusts a non-null answer is told *yes* about a property that does not exist, skips
  creating it, and its first upsert then dies inside GMS with:

      Failed to validate MCP ... Unexpected null value found for
      urn:li:structuredProperty:attest.verdict Structured Property Definition.

  which names the *write* and says nothing about the *read* that caused it. So existence is
  computed from `definition.qualifiedName`, in `client.get_structured_property`, which is the
  only supported way in. Pinned by `tests/test_client.py` — including a test that asserts
  DataHub still fabricates, so nobody deletes the check as a redundant null-guard.
- **SOME FAILURES ARE STRUCTURALLY INVISIBLE TO A FAKE, and the TLS repair is the worked
  example. This is the generalizable lesson of Session 5 — read it even if you never touch
  TLS.**

  *What broke.* `truststore.inject_into_ssl()` changes how *new* SSL contexts are created;
  it cannot reach inside the one an existing client's transport already holds. Session 5
  memoized the lazily-built OpenAI client (a handle forked per run must not open a
  connection pool per run), so the TLS retry reused the **pre-injection** client, repeated
  the same handshake against the same untrusting context, and failed identically — while the
  log line cheerfully announced a repair that had done nothing. Fixed by having
  `_use_os_truststore` drop `_built`; `_built` is kept separate from `client` *precisely so
  it can be dropped*, because `client` is the caller's injected fake and must never be
  thrown away.

  *Why `just check` could not have caught it, ever.* The offline suite was **green
  throughout** — not by bad luck, and not because a test was missing. The fake chat client
  is a Python object: it opens **no socket, negotiates no TLS, and has no SSL context**. The
  entire code path that broke **does not execute** when the client is faked. No test written
  against the fake, however thorough, can exercise it. The green tick was not a weak signal,
  it was a signal about a different program.

  *The rule this generalizes to.* **A fake cannot fail in a way the real thing fails
  through machinery the fake does not have.** Transport, TLS, connection reuse, auth
  refresh, rate limits, timeouts, partial reads — all of it is stubbed out by construction,
  so all of it is invisible to `just check` by construction. That is not a gap to be closed
  by writing more offline tests; it is the *price* of a fake, and the price is worth paying
  (see the cadence rule below) — but it must be paid knowingly. **When a change touches how
  the client is BUILT, CACHED, or REUSED, the offline suite is not evidence. Only `just
  live` is.** And note what made this one nasty: it did not look like a semantic-layer
  change at all — it was a memoization, made for a concurrency reason. The cadence rule
  caught a class of bug it was not even written for, which is the best thing that can be
  said about a rule.

  *And it fails LOUD-then-silent.* A repair that logs success and does nothing is worse than
  one that crashes: the operator reads the log, believes the network was fixed, and hunts
  for the bug somewhere else entirely.
- **EVERY RUNTIME NEEDS ITS OWN SYSTEM-CA OPT-IN ON THIS NETWORK. Three so far, and the
  third cost a spike's first ten minutes.** This is a TLS-inspecting network: the corporate
  CA is in the OS trust store and in **no** language runtime's bundled root store. Each
  runtime ships its own CA bundle and consults the OS one only when told, so each one fails
  independently, at first network contact, with a certificate error that looks like an
  outage. The fix is always the same shape — *tell this runtime to use the OS store* — and
  it is spelled differently every time:

  | Runtime | Opt-in | Where |
  | --- | --- | --- |
  | Python | `truststore.inject_into_ssl()` | `llm.py`; the OpenAI SDK, `httpx`, `curl` |
  | Node | `NODE_USE_SYSTEM_CA=1` | `just ui`; `npm install`, `vite build` |
  | uv / uvx (Rust) | `--native-tls` | `just spike-mcp`; any `uvx` tool fetch |

  All three are harmless when the CA is already trusted, so set them unconditionally rather
  than only on failure. **Assume the next runtime you add is a fourth**: reach for its
  system-CA flag before debugging its "network outage". Note `uvx --native-tls` fails at
  *package download*, before the tool runs at all — so it reads as "PyPI is down", not as a
  TLS problem.
- **Never write YAML with PowerShell's `Out-File`.** It emits a UTF-8 BOM that breaks the YAML
  parser. Use `[IO.File]::WriteAllText` or Python.
- **Ingestion recipes must use relative `./` paths.** Absolute Windows paths hit a
  drive-letter parsing bug in the DataHub CLI — it reads `D:\...` as a URI scheme
  (`Did not find a registered class for d`).
- **Attest's own code talks to DataHub via direct GraphQL over `httpx`, not the
  `acryl-datahub` SDK.** The CLI/SDK is for *ingestion only*.
- **THE CATALOG READ IS GRAPHQL AND NOT MCP, and it is a DECISION rather than a landmine —
  see §12.** It sat here as prose until Session 19 moved it: the named integration path was
  scoped, built against, and measured, and a measured architectural decision belongs in the
  log's numbered sections beside the others, not in a list of things that bite. **Do not
  reopen it as a checkbox.** `just spike-mcp` exits non-zero by design and is the tripwire.
- **The MCP server posts usage telemetry to Mixpanel on startup** (`/mp/engage`, `/mp/track`),
  before it is asked to do anything. It only surfaced here because the TLS interception made
  it fail loudly in the logs; on a normal network it is silent. `DATAHUB_TELEMETRY_ENABLED=false`
  turns it off, and `spikes/mcp_reader_probe.py` sets it: a probe that measures the catalog
  should not also report to a third party that it ran.
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
    cache.py           ONE RUN's view of the catalog. A consistency boundary, not a cache.
  api/
    app.py             FastAPI. The checkpoint does not soften here. /claims reads DATAHUB.
    service.py         Run, persist, approve, write back, retrieve. Audits run CONCURRENTLY:
                       the lock is gone because the shared token ledger it guarded is gone.
    schemas.py         Wire types in and out.
  retrieval.py         Claim artifacts, back OUT of the catalog. The three-state read (an
                       absent verdict is not one fact) and the declared push-down report.
  record.py            AuditRecord: the persisted projection of a report. Loses nothing —
                       and since Session 5 a resumed run is REBUILT from it, so what it
                       drops, a restarted run would report differently.
  replay.py            The record, read backwards: a parked run's typed ledger, rebuilt.
  store.py             The audit history. SQLite, plain SQL, Postgres-shaped. Append-only
                       approvals — DataHub is the catalog, NOT the event store.
  writeback.py         Approved verdict -> ONE DataHub claim artifact per claim (a CUSTOM
                       Assertion + appended run events). Idempotent, so a partial write is
                       repaired by repeating it. Written only when a human publishes it.
  llm.py               The only module that calls a model.
  decompose.py         Agent prose -> typed claims. A URN must be quoted, never minted.
  explain.py           Verdict + evidence -> prose. Three gates; falls back to the template.
  faithfulness.py      Lexical provenance. Every factual token must appear in the evidence.
  polarity.py          The prose may assert only the direction the verdict reached.
  crosscheck.py        Model/checker disagreement -> a Conflict, never a changed verdict.
  sanitize.py          Untrusted agent text in, instruction-like spans stripped out.
  graph.py             The LangGraph pipeline. Routing, the loop, the human checkpoint.
                       Injectable saver (durable pause); one LLM handle per run (receipts).
  revise.py            Self-correction. A revision may not change the subject.
  trajectory.py        Seven invariants asserted against the run's own trace.
  observe.py           Step trace: kind, latency, tokens. What trajectory.py reads.
  cost.py              Prices a run. An unpriced model costs None, never 0.
  report.py            AuditReport: verdicts, proposed corrections, receipts.
seed/                  Seed catalog generator + ingestion recipe (ground_truth.json).
benchmark/             The golden benchmark. A standalone, citable artifact.
  cases.py             40 hand-labeled claims. The generator; cases.json is the artifact.
  cases.json           The dataset. Committed, unlike seed's output: cite-able.
  run_eval.py          Precision/recall/F1 per verdict, confusion matrix, pass@k, --sabotage.
  labeler.py           Nemotron as an independent second labeler. NEVER in the verdict path.
  README.md            The dataset, usable by someone who has never seen this code.
spikes/                Throwaway proofs. datahub_probe.py proves the read/write path.
  mcp_reader_probe.py  The Session 17 evaluation of the NAMED integration path. MCP vs
                       GraphQL, every seeded dataset, then the same gap as verdicts. The
                       receipt behind docs/mcp-evaluation.md. Exits non-zero by design.
tests/                 Two-tier suite (Session 8). The OFFLINE tier reads captured fixtures,
                       never the network: it runs anywhere, never skips, gates CI. The
                       INTEGRATION tier (test_client, marked `integration`) and the LIVE tier
                       (test_live + the pin, marked `live`) need the real server and skip
                       LOUDLY when it is down. `-n auto` parallelizes the offline tier; the
                       live suite is refused under parallel by conftest.
  _snapshots.py        Fixture naming + load/dump helpers. Shared by the `snapshot` fixture
                       (reads) and seed/capture_snapshots.py (writes) so they cannot disagree.
  fixtures/snapshots/  Serialized DatasetSnapshots, one per seeded dataset. Captured from live
                       GMS; held equal to it by test_fixture_drift.py in the live tier.
  test_fixture_drift.py  THE ANTI-DRIFT PIN. Re-fetches every seeded URN and asserts it equals
                       the captured fixture. The fixtures are exactly as honest as this test.
seed/
  capture_snapshots.py Capture the offline fixtures from the live catalog. Run by `just seed`.
```

## Commands

```
just setup     # install package + dev deps
just seed      # generate seed metadata, ingest it, and CAPTURE the offline fixtures (last step)
just capture   # regenerate tests/fixtures/snapshots/ from the live catalog (run by `just seed`)
just spike-claims    # prove ONE dataset holds TWO queryable claim artifacts (Session 15)
just spike-mcp # can the MCP server serve a faithful DatasetSnapshot? NO. Exits non-zero BY
               # DESIGN: it is the receipt for the GraphQL read, and the tripwire if that
               # ever changes. Needs `pip install mcp` + uvx. See docs/mcp-evaluation.md.
just probe     # prove DataHub's read/write path
just health    # is the pinned version actually running?
just serve     # run the API on :8003 (docs at /docs). 8003 is pinned: DataHub owns 8080/9002
just demo      # build the UI and serve it WITH the API from one process on :8003
just test      # offline tier + integration tier, ACROSS CORES (-n auto). Integration skips LOUD.
just test-offline    # the TRULY-OFFLINE tier. No DataHub, no key, never skips. What CI runs.
just matrix    # just the 12-cell coverage assertion
just resume    # durable resume + per-run token billing (the two Session 5 properties)
just bench     # the golden benchmark vs the DETERMINISTIC CORE, pass@5. Free.
just bench-full      # ...vs the whole pipeline, real model. ~1.5 cents.
just bench-sabotage  # THE VACUITY CHECK. Non-zero exit if breaking a checker moves nothing.
just bench-calibrate # cross-family labels (Nemotron). Needs NVIDIA_API_KEY.
just check     # lint + test-offline — hermetic, what CI runs. Genuinely offline (Session 8).
just live      # the LIVE tier by marker (-m live): real model AND the anti-drift pin. Costs money.
just preflight # lint + test + live — required before pushing semantic-layer changes
```

## Verification cadence — a rule, not a habit

**Run `just preflight` (lint + test + live) before any push that touches the semantic
layer.** That means any change to `llm.py`, `decompose.py`, `explain.py`,
`faithfulness.py`, `polarity.py`, `crosscheck.py`, `sanitize.py`, `revise.py`, **or any
prompt string or JSON schema in them**. `just check` is not enough for those files, and this
is not a nicety:

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

**8. Parallel test execution (Session 7). The offline suite runs across cores; the live
suite is refused under parallel, structurally.**

- **`-n auto` (pytest-xdist) is wired into `just test`/`just check`.** The offline suite
  parallelizes freely and it is safe by construction, not by luck: every real write a test
  makes is scoped to a per-test `tmp_path` (its store, its checkpoints), and every read from
  the live catalog is idempotent — so N workers are only more LOAD on DataHub, never a
  different answer. Verified: 263 passed identically serial and at every worker count.
- **Isolation did NOT turn out harder than it sounds, and here is why.** The one thing that
  writes to the *shared* catalog is `test_live` (it approves a real audit and writes
  `attest.*` structured properties onto real seeded datasets). Everything else writes to a
  `tmp_path` or to a fake. So there was no per-worker write-target to build: keep the one
  writer serial and the rest parallelize with nothing to coordinate.
- **The live suite is REFUSED under parallel workers, in `pytest_configure`, before any
  worker spawns.** Two live workers — one approving while another audits the same dataset —
  read a catalog the other is halfway through mutating (last-write-wins on shared entities),
  and the flake lands on the one path that writes to someone's catalog. So it is refused by
  name, not discouraged. The refusal is in `pytest_configure` on the CONTROLLER, keyed on
  the mark expression, **not** in `pytest_collection_modifyitems` — that runs on the
  workers, where `numprocesses` is already 0 and a raise would let a sibling worker write
  first. Learned by watching it not fire. `just live` (no `-n`) is serial and never trips it.
- **`just test -n0 …` forces serial** for debugging — the last `-n` wins, so `-n0` disables
  distribution and you get real tracebacks, working pdb, and un-interleaved output.
- **CAVEAT the parallel work surfaced — RESOLVED in Session 8, see §9.** The "offline" suite
  was not offline: ~126 of the 263 tests READ the live catalog and SKIPPED when DataHub was
  down, so the suite read GREEN (`137 passed, 126 skipped`) with half of it never run — the
  same shape as the `just check` blind spot, a green tick about a smaller program, sitting in
  the test suite of the tool built to catch exactly that. Session 8 split the suite in two and
  moved the catalog-reading tests onto captured fixtures, so they no longer skip. The caveat
  is kept here as the problem statement; §9 is the fix.

**9. Two-tier test architecture (Session 8). An honest split: the offline tier is truly
offline and gates CI; the integration tier is live and skips LOUDLY. The fixtures are
exactly as honest as the anti-drift pin, which ships in the same session.**

- **The capture boundary is the `DatasetSnapshot`, NOT the GraphQL response, and that choice
  is the whole design.** A fixture of a raw GraphQL response freezes GMS's wire format: it
  drifts on every DataHub version bump and a stale one still parses and still passes — a green
  tick about a shape the server no longer sends. So `seed/capture_snapshots.py` serializes
  Attest's own normalized read model (round-trip verified lossless across `term_parents`,
  nested `fields`, `last_modified`) to `tests/fixtures/snapshots/*.json`, one per seeded
  dataset. The `snapshot` fixture loads those instead of fetching. The ~120 catalog-reading
  tests — checkers, benchmark, coverage, explain, faithfulness, pii-signals — stop hitting
  the network; together with the tests that were already fake-only (graph, store, api,
  cache, …) the offline tier is 258 tests that run on a bare runner, never skip, and gate CI.
  *Verified: with the containers stopped, 258 passed, 0 skipped.*
- **The offline tier does NOT exercise `DatasetSnapshot.from_graphql`, and that is stated so
  nobody assumes otherwise.** The fixtures are `DatasetSnapshot`s already; loading one skips
  the parse. Parsing the wire format is `test_client.py`'s job, and it is LIVE (the
  integration tier) — including the structured-property fabrication landmine, which only a
  real GMS reproduces. Offline coverage of parsing would require a fake that cannot fail the
  way the real parse fails — the "structurally invisible to a fake" rule from Session 5 all
  over again.
- **The fixtures are exactly as honest as `test_fixture_drift.py`, and it is non-negotiable.**
  That pin (live-marked) re-fetches every seeded URN from real GMS and asserts, per URN, that
  the captured snapshot equals the freshly normalized one. Change the seed, or take a GMS bump
  that reshapes the normalized model, and it fails loudly with the exact URN and the recapture
  instruction — but **only when the live tier is actually run**. This tier does not escape the
  cadence rule; it inherits it. Fixtures without the pin are the drift trap, which is why the
  pin shipped in the same session, not deferred. *Verified: hand-corrupt one fixture and the
  pin fails by name; restore it and it passes.*
- **Capture is COUPLED to seeding, not left to memory.** `just capture` regenerates the
  fixtures; `just seed` runs it as its last step, so the fixtures and the catalog they
  describe are regenerated together and cannot drift apart between reseeds. The dataset list
  is `ground_truth.json`'s `datasets` — the same manifest the seed wrote and the pin reads —
  so all three (seed, capture, pin) range over exactly the same set with nothing kept in sync
  by hand.
- **The integration-tier skip is LOUD, by the same cadence logic.** The tests that genuinely
  need live GMS are `@pytest.mark.integration` (`test_client.py`) and `@pytest.mark.live`
  (`test_live.py`, plus the pin). They are allowed to skip when the server is down — but
  `conftest.pytest_terminal_summary` prints a red separator naming how many integration tests
  did not run and what coverage was lost, never a line buried in the skip count. A
  suspiciously fast run must say why; this is where it says it. The offline tier never
  contributes to that count, because it never skips.
- **`benchmark.run_eval.Catalog` takes an INJECTED snapshot source, and the vacuity check is
  why.** `test_breaking_a_checker_collapses_the_benchmark` — the guarantee that the benchmark
  *can fail* — used to build a live client inside `Catalog()`, so in a CI-without-DataHub run
  it silently skipped exactly where it mattered most, leaving the benchmark's own can-it-fail
  proof unrun. Now the test injects the fixture loader (`Catalog(snapshot_source=...)`), so
  the vacuity check runs offline and gates CI. The injection keeps `benchmark/` decoupled from
  `tests/`: the loader is passed in, never imported there. `just bench` leaves the source None
  and reads the live catalog, as the measured numbers were taken.
- **The CI gate (`.github/workflows/ci.yml`) exists ONLY because the offline tier is now
  truly offline.** It runs `just check` (lint + `not live and not integration`) on every push
  — no DataHub, no key. It is the "not a toy" signal, and it is honest: a green here is a
  green about the whole offline tier, not half of it skipping. If an "offline" test ever
  reaches for the network it FAILS in CI rather than skipping — which is the point of the
  gate. *Verified: with the containers stopped, the offline tier passes in full — 258 passed,
  0 skipped.*
- **The command map moved: `just check` is now hermetic.** `check` = `lint test-offline`
  (`not live and not integration`), genuinely free and offline — the justfile's old claim to
  that effect finally being true. `just test` still runs offline + integration (integration
  skips loudly if DataHub is down). `just live` runs the live tier by MARKER now (`-m live`),
  not by path, so it picks up both `test_live` and the pin. `just preflight` = `lint test
  live` covers all three tiers.

**10. The claim-level artifact, and the publication gate (Session 15). The write-back had to
start obeying the rule the checkers obey.**

The design is [docs/design/claim-artifact.md](docs/design/claim-artifact.md); the proof it
was designed against is [spikes/claim_artifact_probe.py](spikes/claim_artifact_probe.py)
(`just spike-claims`, 18 checks, exits non-zero on any failure). Everything below was
measured against the pinned Core v1.5.0.6 **before** it was designed.

- **THE PROBLEM, in one question.** *"Two claims about one dataset are approved. Show me what
  the next agent inherits from DataHub alone."* The honest answer was **an opaque run UUID**.
  Structured properties are last-write-wins, so two claims about one dataset overwrote each
  other and the survivor was a verdict with **no subject** — `attest.verdict = "Contradicted"`
  cannot say WHICH claim was contradicted. Challenge 1's thesis is "writes results back so the
  next person or agent inherits the knowledge", and it was not true.
- **One claim = one CUSTOM Assertion + N appended run events.** The assertion is the CLAIM
  (stable: what is asserted, about what, at what grain); each run event is the VERDICT at a
  point in time. Attest supplies the assertion's URN, so distinct claims land at distinct
  URNs and coexist. `dataset.assertions(urn)` is the thesis query.
- **`assertionRunEvent` is a TIMESERIES aspect: it APPENDS.** So "was this ever contradicted
  before someone fixed the tag" is answerable from DataHub alone. §2c's *"DataHub is the
  catalog, not the event store"* was true of **structured properties** and overgeneralized to
  DataHub. The store still exists and still holds Attest's own record (§7 of the design doc).
- **THE VERDICT IS A STORED VALUE. It is NEVER inferred.** `nativeResults['attest.verdict']`
  carries it verbatim; `result.type` is a **lossy projection** for DataHub's health rollup
  (Supported→SUCCESS, Contradicted→FAILURE, Insufficient-Coverage→ERROR, which lands in
  NEITHER the passed nor the failed bucket — correct). **Measured: an Insufficient-Coverage
  claim and a HALF-WRITTEN one both read `succeeded=0 failed=0`.** The discriminator is
  `runEvents.total`, equivalently whether `attest.verdict` is present. A reader on the rollup
  counts confuses "the catalog is silent" (a verdict) with "Attest never finished" (a bug).
- **RETRY IS SAFE BECAUSE ALL THREE WRITES ARE IDEMPOTENT, and TWO RULES ARE LOAD-BEARING.**
  The write is necessarily three operations (upsert the claim, report the verdict, swap the
  verdict tag) and cannot be atomic. It needs no saga: a timeseries row is keyed by
  `(urn, aspect, timestampMillis)`, so repetition IS the recovery. That holds only while:
  **(a) `claim_id` is handed the claim and nothing else** — no run id or clock can leak into
  the identity, so a re-run lands on the SAME artifact; **(b) `write_claim_artifact` has NO
  DEFAULT for `checked_at`** — it is the run's stored timestamp, and `now()` would append a
  duplicate verdict on every retry, corrupting an append-only history with an audit that
  never happened. Both are unreachable from the signature rather than forbidden by comment,
  and both have a test that breaks them.
- **`WriteResult` names the STEP that failed** (`upsert`/`report`/`tag`), not a boolean —
  same reason `CorrectionOutcome` names six outcomes. A failed `report` leaves a claim with
  no verdict; a failed `tag` leaves a verdict that is correct and merely not findable by
  search. `POST /audit/{run_id}/writeback` repairs it: it re-runs the idempotent write from
  the stored record, **approves nothing, and cannot reach an unaccepted claim.** It exists
  because a failed write-back used to STRAND — `approve` skips a settled claim and a
  COMPLETE run 409s.
- **NEVER SET `fieldPath` ON A CUSTOM ASSERTION.** It makes DataHub record the assertee as a
  *schemaField* URN while `assertionRunEvent` requires a *dataset* URN, so the artifact is
  created, reads back perfectly, and can **never carry a verdict** — the one thing it exists
  for. Worse, whether it fires depends on index timing, so it sometimes appears to work. The
  grain travels in `logic`. Enforced by `client.upsert_custom_assertion` having no parameter
  for it.
- More measured landmines: **`result.error` is accepted and silently discarded** (so
  `INSUFFICIENT_DATA`, which looks perfect for verdict 3, is unusable); **`deleteAssertion`
  rejects CUSTOM** (HTTP 500 — use `openapi/v3` DELETE); **`reportAssertionResult` reads an
  eventually-consistent index** and must be retried; **a verdict is not readable the instant
  it is written**, so a read right after an approval can legitimately see a claim with no
  verdict — which looks exactly like a half-written one.
- **Only `customType` and `tags` are filterable on an assertion**, and the two server-side
  entry points are **DISJOINT**: `dataset.assertions` scopes to a dataset but cannot filter;
  `searchAcrossEntities` filters verdict/claim-type but cannot scope to a dataset (measured:
  nine candidate assertee field names, all zero). **The honest line, and the plan says no
  more than this: DataHub does the scoping; Attest does most of the filtering.** The thesis
  question needs no filtering at all — it is one fully server-side query.

**10a. OPTION A: every verdict is publishable. The write-back must obey Attest's own rule.**

**The decision, and it is decided — do not re-litigate it.** Publication is its own act,
orthogonal to the correction loop. Every audited claim parks for a human publish/reject,
whatever its verdict; correction is one thing that can happen to a Contradicted claim, not
the gate.

- **What forced it: `STOOD_FIRM` never published.** The gate was `outcome is CORRECTED`, so
  the ONLY verdict that ever reached the catalog was a contradiction the agent had
  successfully self-corrected — one of five contradiction outcomes and zero of the other two
  verdicts. So the most damning thing Attest can find — an agent claims an `ssn` column, is
  shown the catalog, and **stands by the false claim** (the measured 2-in-6 column swap, the
  entire reason `revise.subject()` exists) — **died silently**. A compliance auditor that
  publishes "the agent was wrong and fixed it" while swallowing "the agent was wrong and
  refused to" is publishing the good news and hiding the bad. That alone forced the gate to
  change, which is also why "leave the code alone and fix the framing" was never available.
- **THE ARGUMENT THAT DECIDES IT, and it is the one to give a judge.** Attest exists to say
  that **absence is not an answer** — an untagged table does not mean "PII-free", it means
  nobody looked. Under the old gate a dataset with no Attest verdict was ambiguous between
  "we checked and it was fine" and "we never checked", and nothing in the catalog could tell
  them apart. **That is Attest committing its own cardinal sin in its own output.** The
  sharpest question was not "show me a Supported claim" but *"your whole thesis is that
  silence is ambiguous — why is your write-back silent?"* There is no answer. Now there is:
  Attest records that it reviewed, whatever the verdict. `policy.py` grants closed-world
  reasoning only because someone recorded that they **looked** (`Verified` is a fact about
  the REVIEW); Attest depended on that and refused to do it.
- **`Decision` SPLITS `publish` from `accept_correction`, and one boolean must never carry
  both.** That is the mistake `CorrectionOutcome`'s six names exist to prevent: a human can
  publish a Contradicted verdict AND reject the fix the agent proposed for it — "you were
  wrong, and your suggested fix is also wrong" — and the old shape could not say it. Both
  are optional; `None` is **"no opinion", not "no"**, and anything unnamed stays PENDING and
  re-parks the run (Session 14's rule at the new granularity).
- **N decisions per run is INTENDED, and the loop cannot strand.** `awaits_human` is monotone
  — every decision moves a claim out of PENDING and nothing moves back — so the checkpoint
  exits as soon as the last claim is ruled on. The only run that still completes unattended
  is one with **no claims**.
- **THE SCALE TENSION, NAMED RATHER THAN QUIETLY RELAXED.** cost.py projects 1000 claims/day.
  Nobody approves 1000 claims a day, so this pushes toward auto-publication — **which
  CLAUDE.md forbids, and which is not built.** *A real deployment needs a policy layer for
  bulk publication; this is a hackathon build with a human checkpoint on every claim, and it
  says so.* Do NOT add `?auto_approve=true` or an "approve all" to close this gap. If a
  deployment wants scale it must decide to relax the rule consciously, and write down that
  it did.
- **The store schema CHANGED and an older database is REFUSED at open** (`claims.publication`,
  `approvals.publish`/`accept_correction`). Same precedent and same fix as Session 5: **`rm
  attest.db attest-checkpoints.db` and re-run.** Both are gitignored dev state; DataHub is
  untouched.
- **MEASURED, live, through the real flow** (`test_live.py::test_all_three_verdicts_reach_
  the_catalog_through_the_real_approval_flow`): one audit, four claims, all three verdicts
  published by a human via `POST /audit/{id}/approve` and read back from DataHub — including
  the unrevisable `ssn` claim. Nothing calls `write_claim_artifact` directly. **Two of those
  three verdicts were unreachable before this.**

**11. THE RETRIEVAL PATH (Session 16). The other half of the thesis, and the read path had
to start obeying Attest's own rule about absence.**

Session 15 proved Attest *writes* results back. Challenge 1's thesis has two halves —
*"writes results back **so the next person or agent inherits the knowledge**"* — and an
artifact nobody can query inherits nothing. [retrieval.py](src/attest/retrieval.py) is the
second half: `GET /claims`, `GET /claims/{claim_urn}`, reading **DataHub**, never the store.

- **THE THREE-STATE READ IS THE LOAD-BEARING PART, and there are FOUR states because an
  absent verdict is not one fact.** The catalog can be silent about a claim's verdict for
  three completely different reasons, and they are **indistinguishable from DataHub alone**:

  | State | What it means | Response |
  | --- | --- | --- |
  | `complete` | the catalog holds the verdict | read it |
  | `pending-lag` | the write **landed**; the index has not caught up | wait ~2s. **Nothing to repair** |
  | `incomplete` | the write **failed** at a step; no verdict is coming | `POST /audit/{id}/writeback` |
  | `unknown` | no verdict, and Attest's record cannot say if one was attempted | **not a diagnosis** |

- **The rule, non-negotiable: NEVER render `incomplete` from DataHub's silence alone.**
  Attest exists to say absence is not an answer — an untagged table does not mean "PII-free",
  it means nobody looked. A read path that saw no verdict and concluded "half-written" would
  be committing this project's cardinal sin in its own output, which is precisely the mistake
  §10a caught in the *write* path. **`unknown` is NOT `incomplete`** and that distinction is
  the same argument one level down: no evidence of a write is not evidence of a failed write.
- **Two questions, two systems, and neither answers the other's.** DataHub: *what does the
  catalog HOLD?* The store: *what did Attest DO?* `read_state()` is four lines and consults
  both — the catalog is asked FIRST and wins (a verdict that is present is present, whatever
  our bookkeeping says; a write recorded as failed may have landed on a call that timed out).
  Only when the catalog is silent does the store's answer decide. **Remove either side and
  the state collapses into a guess.**
- **MEASURED, before any of it was designed** (5 trials, pinned Core v1.5.0.6): a verdict
  becomes readable a **median 2.1s (max 3.2s)** after the report is accepted; the report
  itself is refused by the un-caught-up index for a **median 5.3s (max 7.0s)**, which the
  client already retries past *inside* the write. So **`pending-lag` is the NORMAL state for
  the first seconds after every approval** — a reader that called it broken would be wrong
  loudly, on the happy path, several times a day.
- **The UI auto-polls, BOUNDED (5 checks / 10s), and enters the poll only for `pending-lag`.**
  A claim whose write FAILED will never grow a verdict however long anyone waits — polling one
  would be a spinner over a permanent state. Past the cap it **stops and hands to a human**:
  from there you cannot tell a slow index from a real problem, and a spinner that never gives
  up is a UI asserting "this is fine" about a state it has no evidence for. **`pending-lag`
  and `incomplete` must never share a control** — a retry on a two-second wait invites a human
  to "fix" nothing.
- **THE BOUNDARY, NAMED: a second agent reading DataHub ALONE cannot tell `pending-lag` from
  `incomplete`.** Both are `runEvents.total = 0` with no `attest.verdict`; the separation
  comes from Attest's record, which by definition they do not have. What makes that tolerable
  rather than a hole: it **self-resolves in ~2s**, it is confined to the write's own tail
  (every verdict that landed reads back unambiguously forever), and **the thesis question does
  not touch it** — *"show me what the next agent inherits"* is `dataset.assertions(urn)`, one
  server-side query, complete answer. What a second agent cannot do is diagnose Attest's
  half-finished write, which is Attest's problem to report and not a fact the catalog owes
  anyone. `ClaimReader(client, store=None)` is that reader, in code, and the live test uses it.
- **Every response reports WHERE each predicate was applied, and that is part of the answer.**
  *"Retrievable from DataHub"* is TRUE; *"fully queryable in DataHub"* is FALSE. The two
  server-side entry points are **disjoint**: `dataset.assertions` scopes and filters nothing;
  `searchAcrossEntities` filters `customType` + `tags` and scopes to nothing. `reviewer` and
  `since` can **never** be pushed down — an assertion indexes nothing else. So: **DataHub does
  the scoping; Attest does most of the filtering**, and the API says so on every call rather
  than letting a reader believe the catalog answered what Attest answered.
- **`PUSHES_DOWN` is DECLARED, not read off the query builder** — the trajectory.py rule
  again: derive it from the code that builds the query and it agrees by construction and
  asserts nothing. `test_the_push_down_report_cannot_lie` holds it to what actually went over
  the wire, **in both directions**, and has its own vacuity check. And `_matches_locally` is
  **driven by** the report, so a wrong report produces wrong *results* rather than a quiet lie
  beside a right answer.
- **The stale-tag asymmetry, surfaced rather than hidden.** The tag is written LAST and is a
  derived index, never the verdict. A claim whose `tag` step failed has a correct verdict and
  is **missing from every tag-filtered search**, while a dataset-scoped read finds it. Same
  question, two answers, depending on an entry point the caller did not choose — so the
  verdict-filtered response carries the caveat in its `note`.
- **The store had to grow structure, because a `str()` cannot be parsed back.**
  `approvals.writeback` was `str(WriteResult)` — a *rendering*. Telling index-lag from a
  half-written claim needs the *fact*, so `approvals.writeback_ok` / `.writeback_step` were
  added beside it, and `claims.claim_urn` (derived by `writeback.claim_urn`, never minted) is
  the join key. This is Session 5's lesson at a new column. **An older database is refused at
  open: `rm attest.db attest-checkpoints.db` and re-run.**
- **Fixed on the way, and both were live and silent:**
  - `_refuse_an_incompatible_database` checked `claims.rejected` (Session 5) **and nothing
    else**, so every Session 5–14 database was declared compatible and died on the first
    INSERT *inside a running service*. CLAUDE.md said Session 15's change was refused at open;
    it was not — the schema moved and the guard did not move with it. Now table-driven over
    every column (`_REQUIRED_COLUMNS`): **add a row whenever the schema gains a column.**
  - `ensure_definitions` **skipped any property that already existed**, so a definition
    written once was frozen forever. **Measured: `attest.verdict`'s live description in
    DataHub was the string `"test"`** — a placeholder, on Attest's most-read surface, while
    writeback.py carried three careful sentences and Session 15 rewrote them to stop the badge
    overstating its coverage. The code was right, the catalog never heard, every test was
    green. Definitions are now reconciled (`client.update_structured_property`), idempotently.
  - **The frontend's approve flow had been dead since Session 15**: it sent `{claim_index,
    accept}` and the backend forbids extras → **422 on every approve**. And the review bar
    counted `proposals` while Option A parks on every claim's **publication**, so a four-claim
    run submitted one decision, re-parked, and could never complete while the UI called it
    settled. `PublicationPanel` is the publish/withhold act that had no control at all.
- **The badge descriptions now state what the badge CANNOT do.** Both halves of the old text
  were false: not *"any claim an agent made"* (only **published** verdicts reach the catalog),
  and not *"the most recent one"* (claims published together share a run timestamp, so "last"
  is arbitrary among them). **The badge describes ONE claim and cannot say which** — that was
  the whole problem statement of the claim artifact, and the badge is the thing that still has
  it. A field that overstates its coverage is this project's characteristic bug.
- **`repairable` keys off the FAILED WRITE, not off the state, and the checkpoint is what
  found that.** Gating on `state is INCOMPLETE` looks equivalent and is not: an artifact is
  **content-addressed**, so re-auditing a claim appends to the artifact it already has. A
  claim audited last month, re-audited today, whose new verdict FAILED to write, still shows
  **last month's verdict** — it reads `complete` (not a lie: the catalog does hold a verdict)
  while the latest write sits recorded as broken. Gated on the state it could never be
  repaired, and a stale verdict would stand indefinitely with the failure visible in the
  response and actionable from nowhere. The rule that survives: PENDING_LAG and UNKNOWN are
  never repairable, which now follows from the write rather than from a hand-maintained list.
- **The content-addressing trap bites DEMOS as well as tests, and it bit this one.** A
  read-state can only be shown on a claim nothing has asserted before — on a previously
  audited claim, `pending-lag` and `incomplete` are both masked by the older verdict sitting
  in the artifact. `test_live._await_verdict` documents the same trap for assertions ("wait
  until it has a verdict" is satisfied instantly by a STALE one). **Wait for THIS run's
  event, and demo read-states on fresh claims.**
- **MEASURED, live** (`test_live.py::test_a_published_verdict_is_RETRIEVABLE_from_the_catalog_
  by_a_reader_with_no_store`): publish through `POST /audit` + `/approve`, then read back
  through `GET /claims` **and** through `ClaimReader(client, store=None)` — every claim,
  verdict, reviewer and history out of DataHub alone.
- **MEASURED, by hand, at the Session 16 checkpoint** — all three states on the real server:
  `pending-lag` caught at t+0.0s (`verdict=None`) resolving itself to `complete` at t+1.0s
  with nothing touched; a deliberately half-written claim reading `incomplete` /
  `failed_step='report'` / 0 verdicts, then `POST /audit/{id}/writeback` → `complete`, **the
  same artifact URN**, and **exactly one verdict from that run, not two**.

**12. THE CATALOG READ IS GRAPHQL, NOT MCP (Session 17). Measured, not preferred — and the
finding is Attest's own thesis biting Attest.**

The write-up is [docs/mcp-evaluation.md](docs/mcp-evaluation.md); the receipt is
[spikes/mcp_reader_probe.py](spikes/mcp_reader_probe.py) (`just spike-mcp`). It is a
submission asset, not an apology. **Do not reopen this as a checkbox.**

- **THE GAP WAS REAL AND THE ADAPTER WAS SCOPED.** Challenge 1 names the DataHub MCP Server
  as how agents get catalog context, so "we didn't use the named path" needed an answer
  better than a preference. `cache.Reader` is one method (`fetch_dataset(urn) ->
  DatasetSnapshot`) and the checkers never see the transport, so an MCP adapter was
  well-defined: implement that method, change nothing else. The question was purely whether
  the MCP response **contains the facts a `DatasetSnapshot` is made of.** It does not.
- **THE SERVER RUNS. Compatibility was never the wall**, and that matters because it is the
  failure everyone expects. `is_oss=True`, correct version-gating, every call answered for
  every seeded dataset. The finding is about what those *successful* responses contain.
- **MEASURED: 130 mismatches over 16 datasets (16/16 fail parity), and 4 of 5 TRUE claims
  change verdict — including `customer_profile.email is PII` reading back CONTRADICTED.**
  That row is the finding. `benchmark/README.md` names Supported↔Contradicted a
  **correctness** failure — the worst thing this product can do — as distinct from a coverage
  failure. Over MCP, Attest tells a compliance auditor that a column the catalog *explicitly
  tags PII* is not PII. Not "unsure". Not.
- **THE MECHANISM, and it is ours.** The tag arrives as the display name `"PII"`, so the
  column reads **unlabelled**; `customer_profile` is `Verified`; `COMPLETENESS_REACHES_COLUMNS`
  (§6) propagates that marker down and turns an unlabelled column of a reviewed table into a
  **denial**. Every step is correct in isolation. **Our own hard-won completeness rule is what
  weaponizes the transport's loss into a confident false denial** — because it assumes the read
  is lossless, and until now the read was never a place where meaning could be lost.
- **THE RULE THIS LEAVES BEHIND: a transport that is lossy for an LLM is not merely lossy for
  a checker — it is INVERTING.** Give a language model `"PII"` instead of `urn:li:tag:PII` and
  it does fine; it was going to read the string anyway. Give a deterministic checker the same
  thing and the signal does not weaken, it **reverses**, because the checker's precision is
  exactly what makes it unable to shrug.
- **AND NOTE WHERE THE GUARDS ARE NOT.** faithfulness, polarity, crosscheck, trajectory — all
  of them fail closed on the **MODEL's output**. **Nothing defends the catalog READ**, because
  the read was the one thing that could be trusted to mean what it said. An LLM-shaped
  transport puts a lossy compression stage exactly where the system has no guard, and the
  failure it produces is silent, confident, and wrong.
- **Four defects, verified in the server's own source, none configurable:** a Dataset's
  `lastModified` is requested by **no tool** (so `FreshnessClaim` — one of four claim types,
  three of twelve matrix cells — has *no input*); field tags/terms are flattened to **display
  names**, so a column's term can never join the glossary hierarchy that makes it a signal at
  all; `type` is **commented out** of the server's own fragment while its reader still looks
  for it; and `clean_gql_response` collapses absent and empty. Plus two latent ones: schemas
  truncate to a token budget (a dropped column reads as the catalog *denying* it exists —
  Contradicted, from a token budget), and `get_entities` returns `isError: False` for a
  missing entity.
- **ONE OVERSTATED CLAIM WAS RETRACTED BEFORE IT SHIPPED, and that is the part worth keeping.**
  The tempting story went: `CustomerIdentifier` has no parent nodes — deliberately outside the
  PII node, which is exactly what makes it not-PII — so `parentNodes.nodes` is `[]`, the strip
  deletes it, and *the evidence that a term is deliberately not-PII is precisely what gets
  dropped*. Elegant, thematic, and **false**: `count` is a scalar, so `{count: 0, nodes: []}`
  survives as `{count: 0}` and the term stays legible. The real `term_parents` mismatch is the
  display-name flattening. **The absent-vs-empty collapse is real but costs evidence FIDELITY,
  not a verdict** — Attest still reaches the right answer and can no longer say whether the
  catalog was *silent* or *empty*. Found by checking rather than by shipping it.
- **`just spike-mcp` EXITS NON-ZERO BY DESIGN** and is the tripwire: one server version
  against one DataHub version, and both move. If it ever goes green the finding has expired
  and this decision is worth reopening. Same discipline as `test_fixture_drift.py` — an
  assertion that only ever passes is a green light wired to nothing.
- **Do NOT "close the gap" by adding MCP as a demonstrated-but-non-verdict context path.**
  Considered and refused: running the inverting transport purely to say we touched it is the
  hollow checkbox integration the finding argues against, and a second read of the same
  catalog re-opens the §2c consistency boundary (one run, one frozen snapshot).
- **What it does NOT prove:** not that the server is defective for its intended use (the
  compaction is a *feature* for an agent assembling prose context); not that no adapter is
  possible in principle — only that none is possible on this server's responses without
  inventing data the catalog did not send. Three defects are drafted upstream
  ([docs/upstream/](docs/upstream/)); were they fixed, this should be revisited.

**13. THE REPO TELLS THE CURRENT TRUTH (Session 18). Docs rebuilt to match the shipped
feature, on receipts only.**

No code changed. The README had drifted into describing a system that no longer existed, and
a submission's front page making claims the code contradicts is the same defect class this
project exists to catch — one level up.

- **The README leads with the inheritance thesis** (per-claim write-back to DataHub, so the
  next agent inherits it), not with a session diary. Three Mermaid diagrams depict the REAL
  path — the trajectory 409, the publish/withhold axis, the sequential non-atomic write — and
  four badges, each backed (CI is dynamic and labelled "offline checks"; the Core version pin
  says **tested-against**, not enforced).
- **Hand-typed numbers were replaced by a receipt table** linking `core.json`, `full.json`,
  `core-sabotaged-classification.json`, `calibration.json`. Every displayed figure opens.
- **CUT, as unreceipted:** the 14.3s console line, the 2/6 correction rate, the 2/9 and
  9-of-9 explanation counts, the 2-fetch and 90-entity outputs, the 97.5%→100% history.
- **CORRECTED, as contradicted by the code:** seven routes, not four; write-back is per-claim
  content-addressed artifacts with append-only events, **not** a last-write-wins badge;
  revision calls **Attest's own configured model**, not a callback into the source agent.
- **SCOPED, as overstated:** polarity and faithfulness are **lexical detectors**, not proof of
  entailment; injection cannot reach the verdict, which is **not** injection-proofness; resume
  covers a **parked run**, not the remote writes.
- **DISCLOSED, as real gaps:** the crash-orphan window, retrieval capped at 50 without
  pagination, lossy Insufficient-Coverage evidence, no hosted demo — **and no
  browser-to-DataHub end-to-end test.** That last one is Session 19's subject.
- Depth moved to [docs/architecture.md](docs/architecture.md) (trust boundary, PII policy,
  guards, resume, and the cost projection as a **dated projection**, not a receipt) and
  [CONTRIBUTING.md](CONTRIBUTING.md) (commands, tiers, cadence).

**14. THE BROWSER E2E (Session 19). The one hop no other test made — and the drift class that
shipped twice, caught by a human both times.**

`tests/test_e2e_browser.py`, `just e2e`. The vacuity check is `spikes/e2e_sabotage.py`
(`just e2e-sabotage`), and it is not optional reading.

- **THE GAP, precisely: `test_live.py` looks end-to-end and is not.** It drives
  `fastapi.testclient.TestClient` — an IN-PROCESS ASGI transport. It never binds a port,
  never loads the built bundle, never runs a `fetch`. So **no test executed
  `frontend/src/api/client.ts` or `types.ts` at all**; the TS mirror was hand-kept in sync
  with `record.py` by READING it. The Session 5 rule at the browser boundary: `TestClient`
  has no fetch, no JSON round-trip through TypeScript, and no `extra="forbid"` rejection to
  surface, so it cannot fail the way the real thing fails.
- **BOTH BUGS THIS CATCHES REALLY SHIPPED (6903d6c), both lived a full session in `main`,
  and both were found by a human clicking the app while the entire suite was green.** That is
  why they are the sabotages rather than invented ones. `just e2e-sabotage` re-introduces
  each and **exits non-zero if the E2E does not go red** — same discipline as
  `bench-sabotage` and `spike-mcp`.
- **THE 422 MUST BE CAUGHT AT THE WIRE, NOT WAITED OUT — and the first version got this
  wrong.** The check written to name the 422 sat *below* a `wait_for` on the success text, so
  under the real regression it failed as a **90-second timeout** waiting for text that was
  never coming: a mystery hang, which is the same symptom as five other bugs. `expect_response`
  around the click makes the 422 the FIRST thing that fails, with the server's own words
  (`extra_forbidden`, `body.decisions.0.accept`). **Measured: ~2s instead of 90.** A diagnostic
  that only runs on the happy path is decoration.
- **AN EPHEMERAL PORT, NEVER :8003.** `just serve` runs `--reload`, whose child holds the
  socket and can outlive Ctrl-C (`just port`). A test bound to 8003 would not fail against
  that orphan — it would **silently talk to it**, a different process with a different store
  and an older bundle, and go green about the wrong program. Unreachable, not unlikely.
- **`DELETE /openapi/v3/entity/assertion/{urn}` RETURNS 200 AND DOES NOTHING OBSERVABLE.**
  Measured: the artifact still reads `complete`, `history=1`, after a 200. The timeseries run
  events survive the entity delete. So a "fresh" artifact cannot be made by deleting a used
  URN, and the repair test's read-state would have been masked by an earlier run's verdict —
  it would have passed once and lied forever. **The claim is made fresh by its CONTENT
  instead** (a varying freshness window), which is CLAUDE.md's own prescription: *demo
  read-states on fresh claims*. Same loud-then-silent shape as the TLS repair: a delete that
  reports success and changes nothing.
- **THE FAULT INJECTION IS OUT-OF-BAND, and `writeback.py` / `service.py` get NO scaffolding.**
  A test-side wrapper fails the client's `report` step; product code has no env hook, no
  test-only branch, no `if broken:`. Their correctness IS the product. The wrapper must raise
  **`DataHubError`** — the only exception `write_claim_artifact` catches — because injecting a
  bare `RuntimeError` does not simulate a partial write, it simulates an unhandled crash and
  500s the approve. Got that wrong first; the test failed loudly, which is the trap working.
- **The repair test's server is IN-PROCESS while the main test's is a SUBPROCESS, deliberately.**
  The main test runs `python -m uvicorn attest.api.app:app` exactly as `just demo` does, so the
  shipped command is what is proven. The repair test needs a client whose report step fails,
  and injecting that into a subprocess means a fault hook in product code. In-process is still
  a real server on a real socket answering a real browser; the only thing given up is process
  separation, and what is bought is that writeback.py stays clean.
- **PLAYWRIGHT DRIVES INSTALLED EDGE (`channel="msedge"`). No browser is downloaded.** Its
  downloader is the bundled NODE driver, which would need `NODE_USE_SYSTEM_CA=1` on this
  network — not downloading avoids the class entirely. Pinned in its own `e2e` extra,
  **deliberately not in `dev`**, so CI never installs or runs it. The cost, named: the browser
  is whatever Edge the machine has; pinning playwright does not pin Edge.
- **`curl` IS NOT A VALID TLS PROBE ON THIS MACHINE, and it nearly wrote a false finding into
  the plan.** `curl https://cdn.playwright.dev/` returns 000 — and so does
  `curl https://pypi.org/simple/`, which pip had just used successfully. The failure is
  `CRYPT_E_NO_REVOCATION_CHECK` inside Windows **schannel**, curl's own TLS stack. Probed with
  the repo's own approach (`truststore.inject_into_ssl()` + httpx) both answer. **Probe with
  the runtime that will do the work**, never with whatever is on PATH.
- **The playwright import is INSIDE the fixture, never at module scope.** `pytest.importorskip`
  at module scope runs during COLLECTION, before mark filtering, so on a runner without
  playwright it reports a SKIP into the **offline tier** — whose whole claim is that it never
  skips, and whose skips are made loud on purpose. A false alarm about lost coverage in a tier
  that lost none. *Verified: offline tier 340 passed, 0 skipped.*
- **WHAT IT DOES NOT PROVE, and the README says so:** it is ONE path — the demo path and the
  repair path — and **not UI coverage**. It does not prove the crash-orphan window
  (`unknown`): that needs the process to die between DataHub committing and the store
  persisting, which is not simulatable without faking it, and faking it would prove nothing.
  It stays a documented limitation.

## Known deferred items — document, don't fix

| Item | Today | Why deferred |
| --- | --- | --- |
| **Semantic glossary-term matching** | A term implies PII iff it is *filed under the PII node*. A term nobody filed there implies nothing, however personal it reads. | Deciding that an unfiled term *entails* a classification is semantic entailment — the LLM layer's job, evidence-constrained. Structure is a declaration; a name is a guess. |
| **Ownership-type distinctions** | `ownershipType` (technical / business / steward) is ignored; any listed owner satisfies an ownership claim. | "Alice is the *business* owner" is a strictly stronger claim. Checking it needs the role in the claim schema — a schema change, not an `if`. |
| **Cross-dialect type equivalence** | Both DataHub type vocabularies match exactly; `int8` ~ `BIGINT` does not. | Needs a model of each platform's type system. |
| **A step's `inputs` / `outputs` across a restart** | Not persisted, so a *replayed* step carries them empty. **The boundary is ASSERTED, not just documented:** `test_nothing_a_reader_sees_depends_on_a_step_s_inputs_or_outputs` strips the summaries out of a real run's trace and demands the record, the receipts, the summary and the trajectory verdict are all unmoved. | They are a log convenience, and nothing a reader sees may read them. If something ever does, a resumed run starts reporting something an unrestarted one does not — silently, only after a restart, with every other test green, because every other test runs in one process and never replays. That is the TLS bug's shape exactly, which is why this one is nailed down rather than trusted. Two sabotages prove the assertion bites (a receipt reading `outputs['cached']`; a trajectory rule reading `outputs['resolved']`), and the fixture uses two claims over one dataset **so the summaries are truthy** — a one-claim run leaves them falsy and would pass a sabotage it was written to catch. |
| **Store migrations** | None. A database older than Session 16 is refused at open, by name — and since Session 16 the guard checks EVERY column, not just the first one it was written for (`_REQUIRED_COLUMNS`; add a row whenever the schema gains a column). **The fix is one line: `rm attest.db attest-checkpoints.db` and re-run** — both are gitignored dev state and DataHub is untouched. | Inferring the lost structure back out of its rendering is Attest fabricating its own audit trail. A real deployment needs a real migration; this is a hackathon build and says so rather than shipping a lenient parser. |
| **A write-back result is matched to a claim by TARGET URN in the UI** | Two claims about one dataset both display the last write's result. The backend has this right — its decision log is keyed by claim INDEX, precisely because keying by URN attributed a write to the wrong decision — but `WriteBackView` carries `claim_urn` and no claim index. | The UI cannot derive the artifact URN: it is a sha256 over the claim's canonical JSON, and re-implementing that identity rule in TypeScript would be a worse bug than the one it fixes (two implementations of an identity that must never disagree). The fix is a claim index on the wire type, which is the write path's shape to change. |
| **A failed write-back is reported on the results screen with no control to repair it** | `PublicationPanel` renders "Published, but the catalog write failed at {step}" and stops. The remedy is real and one call away, but it lives on the Published-claims screen — a human must navigate there to reach it. | Found by Session 19's E2E, which had to navigate to drive the repair. It is a UX gap, not a defect: the existing path WORKS and is now proven end to end. Adding a second entry point to the one endpoint with a catalog side effect is a change to make deliberately, not to slip in beside a test. |
| **The repair spinner fans out across a run** | `ClaimsExplorer` passes `retrying={retrying === claim.audit_run}`, so repairing one claim shows "Repairing…" on every claim from the same audit. | Cosmetic, and the same URN-vs-index shape as the write-back row below: `retryWriteback` is genuinely run-scoped, so the state is keyed by the thing the call actually takes. The fix is a per-claim key the wire type does not carry. |
| **No reconciler for a stale verdict tag** | A crash between `report` and `tag` leaves a correct verdict that a tag-filtered search cannot find. It is recorded (`writeback_step`), visible (`GET /claims` says so in its `note`), and repairable in one call — but nothing detects it automatically. | A production deployment would want a periodic reconcile comparing each claim's latest run event against its verdict tag. This is a hackathon build, and it says so rather than shipping a sweeper nobody exercises. |

**Durable resume is now BUILT** (Session 5). A run parked at the human checkpoint survives
the death of its process: the paused graph comes back from `SqliteSaver`, the typed ledger
is rebuilt from the store by [replay.py](src/attest/replay.py), and the resumed run goes
through the `human_checkpoint` node like any other. See §2d.

**Concurrent audits are now BUILT** (Session 5). The lock is gone, because the thing it was
guarding — one shared `llm.usage` list — is gone: each run forks its own handle. Two audits
run at once and each receipt bills only its own tokens. See §2d.

**Entity-not-found is now DECIDED** (it was the pipeline's call to make, and Session 3 made
it). A claim about a dataset that does not exist surfaces as a `report.ClaimError`, kept
out of `audits` entirely and counted in no verdict tally. It is **not** a verdict: the
catalog neither disagrees with the claim nor is silent about it — the question was
malformed. Scoring it Insufficient-Coverage would launder a hallucinated URN into a
legitimate-looking audit result, and the bad URN would never be seen.

## Commit convention — follow strictly

**Git flow: all session work commits directly to `main`.** That is the established workflow —
the author reviews the diff on `main` and pushes. Do NOT create feature or session branches, do
NOT open a fast-forward-only sidetrack "to be safe", unless the author explicitly asks for one.
The default "branch off the default branch first" habit is wrong for this repo; every session's
history is linear on `main`, and a stray branch is churn the author then has to unwind.

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
