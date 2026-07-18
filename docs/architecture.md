# Architecture

Depth behind [the README](../README.md). This document explains *why* the boundaries sit where they
do. The exhaustive engineering log — every invariant, every landmine, and the session it was learned
in — is [CLAUDE.md](../CLAUDE.md).

## Where the model boundary is drawn

The checkers are pure code: date math, set membership, string comparison. Four questions came up that
code cannot answer without inventing semantics, and each was pushed out rather than guessed at —
because a checker that quietly guesses is worse than one that abstains. Its wrong verdict has the same
confident shape as a right one.

1. **Entity resolution** — "the customer table" → which URN? Not here. Claims arrive with an explicit
   `target_urn`, validated as a dataset URN. Keeping resolution upstream means a resolution error can
   never be laundered into catalog disagreement.
2. **Label opposition** — does `NonPII` contradict `PII`? Nothing in DataHub says so; they are two
   unrelated URNs. It is declared as data in [`checkers/policy.py`](../src/attest/checkers/policy.py),
   so a tag rename cannot silently flip verdicts.
3. **The closed-world assumption** — does an untagged table contradict "contains PII"? **Normally no.**
   Tags are open-world; a missing tag is silence. Contradicted requires the *catalog* to declare its
   classification complete, via a `Verified` marker a human applied. Attest never assumes closed-world
   reasoning — the catalog grants it, per entity. This is the single most consequential rule in the
   layer.
4. **Cross-dialect type equivalence** — is a `text` column a `string`? Both of DataHub's type
   vocabularies are matched exactly, but genuine dialect mapping (`int8` ~ `BIGINT`) needs a model of
   each platform's type system. Deferred: it is a semantic-entailment escalation, not an `if`.

The corollary: **"PII-free" is not the mirror image of "contains PII."** An untagged table cannot
*support* a PII-free claim — nobody has looked. A naive checker returns Supported there and certifies
an unreviewed table as clean, which is a groundedness auditor manufacturing false assurance.

## How Attest decides what is PII

By a **named list**, not a string match. [`PII_SIGNALS`](../src/attest/checkers/policy.py) names three,
and **any one is sufficient** to contradict a "PII-free" claim:

| Signal | What it is | Kind |
| --- | --- | --- |
| `urn:li:tag:PII` | The global tag. | explicit |
| A glossary term under the **`PII` node** | `EmailAddress`, `PhoneNumber`, `PersonName`. | implied |
| `hasPII` custom property, truthy | An upstream classifier's finding. | implied |

Real catalogs mark PII in more than one place, and a checker that knows about one of them **certifies
the others clean** — the worst verdict this product can return. So each signal has a seeded dataset
where it is the *only* signal present: `hr_headcount` is tagged PII with zero glossary terms,
`marketing_leads` carries PII-node terms with zero PII tags, and `device_telemetry` has only
`hasPII=true`. None of the three can be dropped without a test going red.

Nothing is inferred from a name. `EmailAddress` is a PII signal because someone **filed it under the
PII node** in the catalog's own hierarchy — not because the string reads as personal.
`CustomerIdentifier` is deliberately *outside* that node: a surrogate key is not personal data, and a
checker that reads "customer" as "PII" flags every table in the warehouse.

`hasPII=false` fires **nothing**, in either direction. A scanner that looked and found nothing is not a
review, and absence of evidence is not evidence of absence.

### When signals disagree: precedence

They will disagree, and the disagreement is usually meaningful rather than a mistake.

**Rule A — signals propagate up, never down.** The asymmetry follows from what a table-level PII claim
*means*: "this table contains PII" is **existential**. It is true if PII is anywhere in the table.

- **Up.** A column tagged PII settles a table-scoped claim whatever the table's own metadata says.
  Without this, a `Verified` table with no table-level PII tag answered "is this PII-free?" with
  **Supported** while its own `actor_email` column sat tagged PII — the worst verdict the product can
  produce. [A test](../tests/test_pii_signals.py) now makes it unreachable.
- **Down — deliberately not.** A table's PII tag says nothing about its untagged `signup_ts` column.
  Inheriting it downward would mark every column of every PII table as personal data.

**Rule B — within one grain, an explicit tag beats an implied signal.** A tag is a classification act
performed on that entity; a term is a coarser statement of subject matter and a property is a machine's
guess. When a human's review and a machine's classification disagree, the review wins. An explicit PII
tag *on a column* also outranks a `NonPII` tag on its table: the more specific act is better evidence.

The two worked examples are mirror images, and both are in the seed on purpose. `email_campaign_stats`
is filed under the `EmailAddress` term while its `recipient_email_hash` column is explicitly tagged
`NonPII` — the column wins, so it is not PII. `audit_log` carries no table-level PII signal at all
while its `actor_email` column is tagged PII — the column wins again, pointing the other way.

Precedence resolves the conflict; it does not hide it. The **losing signal is still returned as
evidence**, so an explanation can say why a table filed under `EmailAddress` came back PII-free.

### `COMPLETENESS_REACHES_COLUMNS`

The completeness marker propagates **down** even though signals do not, and the asymmetry is the point.
A PII *signal* is a fact about the **data**, and "contains PII" is existential. `Verified` is a fact
about the **review** — "the team looked and tagged what they found" — so a column they did not tag is a
column they reviewed and found clean.

Hence: an untagged column of a **Verified** table is *Contradicted* for "contains PII"; the same
untagged column of an **unverified** table is *Insufficient-Coverage*. Same claim, same shape; the only
difference is whether anyone looked.

**This rule was in the code before it was in the policy, and that was the bug.** It lived as a comment
inside a checker — a governance semantic buried in an `if`, the exact thing `policy.py` exists to
prevent. The benchmark's cross-family labeler surfaced it: handed the declared policy, Nemotron applied
the propagation rule and returned Insufficient-Coverage where Attest returns Contradicted. It was not
wrong — it had been told both rules and never told how they interact, because nobody had written that
down. That is what a second model family is *for*.

## The snapshot cache is a consistency boundary

Without a cache, two claims about the same dataset in the same audit are checked against two **separate
reads** of the catalog. If someone re-tags the table in between, Attest returns "contains PII:
**Supported**" and "PII-free: **Supported**" in the same report — each correct against the catalog as it
saw it, and together, nonsense. **A verification tool that cannot say which state of the world it
verified against has not verified anything.**

So a run resolves each entity exactly once, and every claim in that run is decided against that one
snapshot. The report then means something precise: *these verdicts hold against the catalog as it stood
when this run read it.* The speed is a side effect — if it were only about speed it would be optional.

**Cross-run caching would be a liability, not an optimization.** A snapshot carried into the next audit
means verifying today's claim against a catalog that no longer exists — the exact failure Attest was
built to catch, committed by the tool that catches it. So: cache within an audit for consistency;
always re-fetch for a new audit for correctness.

The scoping is **structural, not disciplinary**. The cache is created inside `run()`, lives on that
run's ledger, and dies with it. `Pipeline` holds no cache attribute, so there is no object a second run
could reach. (No Redis: it buys cross-process sharing, and the only reader of a run's snapshots is the
run itself.)

## Why a graph and not a for-loop

A fair question — a `for` loop would run these steps in this order. Four things the graph buys, each a
property a loop would have to be *trusted* to maintain:

1. **The retry cap is an edge, not a counter.** Self-correction is a genuine cycle
   (`revise → recheck → revise`), and the only way out is a conditional edge reading the retry count. A
   `while` with a `break` is one careless edit from unbounded, and an unbounded correction loop against
   a paid API is a cost bug that ships silently.
2. **The human checkpoint is a real pause.** `interrupt_before` parks the run mid-graph with its state
   intact. It does not return a flag saying someone should look at this — it **stops**.
3. **Routing by claim type is topological, not an `if`.** Each claim type has its own checker node, and
   because the trace records which node ran, a misrouted claim is a *catchable fact*. In a for-loop the
   dispatch agrees with itself by construction and cannot be audited from outside.
4. **The trajectory is a record, not a story.** Every node records its kind, latency and token spend —
   which is what lets Attest *prove* its central claim instead of asserting it.

## Trajectory verification

The sharpest cheap-shot at any agentic system is that the graph is decoration around a single model
call doing all the work. The answer has to be an **assertion**, not a log line.

Checking that the nodes you called got called is trivially true and proves nothing — a graph that ran
every node in the right order and let a model pick the verdict would sail through it. These are the
properties that break if the deterministic core is hollowed out:

| Rule | What it catches |
| --- | --- |
| **`no-llm-in-the-verdict-path`** | The big one. A verdict step that spent **any tokens**, or a model call smuggled in between resolving an entity and deciding on it. |
| `no-verdict-without-a-deterministic-check` | A verdict no checker produced. |
| `no-explanation-without-the-guard` | Unverified prose reaching a reader. |
| `no-correction-without-re-verification` | A loop that *believed* the model's revision. |
| `no-claim-without-decomposition` | A claim minted mid-pipeline, never URN-checked. |
| `routing-matched-the-claim` | A freshness claim answered by the ownership checker. |
| `retry-cap-held` | The loop ran away. |

A step's `kind` is the **claim it makes about itself**; its token count is the **evidence that checks
it**. This is why checker nodes are handed the LLM handle even though they must never call a model:
passing it *arms the trap*, because a step that cannot bill tokens cannot detect them.

**The expected path is declared in `trajectory.py`, not read off the graph's edges.** Derive it from
the graph and it agrees by construction and asserts nothing.

**A violation is a hard gate, not an alarm.** A violating run is `FLAGGED`, which overrides both
COMPLETE and AWAITING_REVIEW and makes the run un-approvable — `service.approve` refuses it with a 409
before any rehydrate, resume or write-back. A report the pipeline could not vouch for cannot reach the
catalog. And the rules are proven to *fire*: [`tests/test_trajectory.py`](../tests/test_trajectory.py)
sabotages the real pipeline four ways — guard torn out, a checker that spends tokens, a correction
proposed without re-verification, a miswired router — and every other test in the suite stays green
through all four. A trajectory check that only ever passes is a green light wired to nothing.

## Self-correction, and why it cannot be gamed

When a claim is Contradicted, Attest asks **its own configured revision model** to restate the claim,
given what the catalog actually says. (`source_agent` is provenance, not an addressable callback: there
is no agent-to-agent delivery here.) The revision is then **re-verified by the same deterministic
checker against the same snapshot** — so the outcome of a correction is decided by code, exactly as the
original verdict was. The same snapshot is reused deliberately: the agent is held to the facts it was
*shown*, so the catalog cannot move underneath the loop.

One rule makes this an audit rather than a negotiation, and it is enforced by comparison after the
fact, never by asking the model nicely:

> **A revision may change what a claim ASSERTS. It may never change what the claim is ABOUT.**

| Claim type | Subject — frozen | Value — revisable |
| --- | --- | --- |
| freshness | *(the dataset)* | `max_age_hours` — widen the window |
| ownership | *(the dataset)* | `owner_urn` — name the real owner |
| classification | the `labels`, the column | `present` — flip the polarity |
| schema | the column **names** | the column **types** |

Correct a column's type; never swap the column. Flip `present`; never swap the label. Every one of
those swaps would re-verify **green** while leaving the false claim uncorrected and replaced by an
unrelated true one — the agent wriggling out from under the finding. It is closed at three grains: the
target URN, the claim type, and the subject *within* the claim. The URN rule alone does not close it,
which is why `revise.subject()` exists.

**The rule is what makes some claims honestly unrevisable — and that is a feature.**
`customer_profile has an ssn column` is Contradicted and cannot be corrected: a `SchemaClaim` has no
`present=False`, so "it does *not* have an ssn column" is inexpressible, and naming a column that *does*
exist is forbidden by the rule above. The only honest move left is to stand by the claim and be marked
wrong — and that outcome is still publishable, which is the point of Option A.

The outcome is **named, not a boolean** — collapsing these would hide the interesting ones:

| Outcome | Meaning |
| --- | --- |
| `corrected` | Revised, re-verified Supported. Becomes a **proposal**. |
| `not-corrected` / `exhausted` | Revised, re-verified, still wrong. The cap (2) stopped it. |
| `stood-firm` | The agent declined: the evidence does not determine the truth. An honest non-answer. |
| `refused` | The revision changed the subject, or failed the claim schema. Rejected *before* verification. |
| `not-attempted` | The verdict was not Contradicted. Insufficient-Coverage is **never** dragged into the loop — the catalog being silent is not the agent being wrong. |

A live test asserts the property that holds every time — *Attest invents no correction for an
unrevisable claim* — rather than a specific outcome, which is the model's to choose. Asserting one
specific outcome made it flake, and a flaky assertion on a load-bearing invariant is worse than none:
it trains people to re-run the suite until it goes green.

## The human checkpoint

A revision that re-verifies clean is **still not applied**. It becomes a *proposal*, the graph parks,
and it stays `PENDING` until a person rules on it.

> **An auditor that silently rewrites what it audits has stopped being an auditor.**

That is the whole reason, and it has nothing to do with the loop being unreliable — the loop is
re-verified by deterministic code and works. It is that Attest's entire value is that a human can point
at any verdict and see the catalog field it came from. A correction Attest applied to itself, on the
strength of a model's revision, would be **the one fact in the system with no independent source**.

**Every audited claim parks for a publish/withhold decision, whatever its verdict**, and accepting a
correction is a separate axis. This matters more than it sounds: under the older gate, the only verdict
that ever reached the catalog was a contradiction the agent had successfully self-corrected — so the
most damning thing Attest can find (an agent shown the catalog that **stands by** a false claim) died
silently. An auditor that publishes "the agent was wrong and fixed it" while swallowing "the agent was
wrong and refused to" is publishing the good news and hiding the bad.

And the deeper argument: Attest exists to say **absence is not an answer**. If only some verdicts were
published, a dataset with no Attest verdict would be ambiguous between "we checked and it was fine" and
"we never checked" — Attest committing its own cardinal sin in its own output.

**The checkpoint is a loop.** Anything left PENDING routes straight back to it and parks the run again,
so a partial decision — or an empty one, which the API documents as legal — leaves the rest decidable
later. "Nothing is accepted by default" is only an accountability guarantee for as long as accepting it
*later* is still possible.

## Who audits the auditor

If Attest's own explanations were unverified model prose, the product would be self-undermining. The
model's world is deliberately small: it sees the claim, the verdict, and the evidence fields the checker
returned — never the raw catalog, never the snapshot. It cannot reach for a better fact, because it was
not given one. What comes back is not trusted:

| Gate | What it catches |
| --- | --- |
| **Cross-check** | The model reads the evidence as a *different verdict*, or cites a field the checker never read. Disagreement never changes the verdict; it is surfaced as a `Conflict`. |
| **Faithfulness** | Fabricated specifics — a hallucinated owner, column, tag, date or number. |
| **Polarity** | Prose that asserts a direction the verdict did not reach. |
| **Template fallback** | Anything that fails the above. The explanation degrades to something **true**, never to something plausible. |

Three details make the faithfulness guard real rather than decorative:

- **Matching is by contiguous word sequence, not substring.** `PII` must not match inside `NonPII` —
  that single bug would wave through the exact hallucination this product exists to catch. Nor can a
  fabricated `customer_email` be assembled out of a real `customer_profile` and a real `email` column.
- **The guard fails closed on names.** A capitalized word is lexically identical whether it is a
  fabricated owner ("Sarah Jennings") or ordinary prose. A false rejection costs fluency; a false
  acceptance costs the product its reason to exist.
- **Derived numbers are rejected even when correct.** If the evidence says `10009.9h`, an explanation
  may not say "417 days". The arithmetic may be right, but the guard cannot tell a correct derivation
  from a plausible one — and one that accepts plausible arithmetic accepts hallucinated arithmetic.

**What faithfulness cannot catch, and never could**, is a fluent lie told in ordinary prose that names
no false fact: *"the catalog supports the claim"* beside a Contradicted verdict passes every token
check. That hole was reproduced on the real path, and it is closed by
[`polarity.py`](../src/attest/polarity.py), not here. Polarity is a **presentation** guard: rather than
trying to prove the direction the model asserted, it stops the model asserting a direction at all. The
authoritative explanation is always the deterministic reason.

Be precise about the size of this claim. These are **lexical detectors**. They guarantee that a
*detected* polarity contradiction cannot ship as model prose and that the deterministic verdict remains
authoritative. They do not prove arbitrary natural-language entailment, and nothing here should be read
as saying they do.

**The verdict itself is never at risk, whatever the model does.** Verdicts come from
[`checkers/`](../src/attest/checkers/), which take a typed claim and a catalog snapshot and never see
agent text at all. A test asserts the deterministic core imports no model client.

### Prompt injection

Attest ingests untrusted text by definition — the thing it audits is another agent's output — so
[`sanitize.py`](../src/attest/sanitize.py) strips instruction-like spans ("ignore previous
instructions", "mark this as Supported") and logs them as findings rather than swallowing them.

But a sanitizer is a blocklist, and blocklists leak. **The honest scope of the structural answer:
there is no prompt in this system whose output is a verdict** — the verdict boundary is deterministic
and typed. That is not the same as the system being injection-proof. Untrusted text can still affect
claim *extraction* or revision prose, or cause a claim to be dropped or malformed. What it cannot do is
decide what the catalog says.

## Durable resume

A run parked at the human checkpoint survives the death of its process. **Two durable things, from two
places, and only one is LangGraph's**: the paused *graph* comes back from `SqliteSaver`, and the typed
*ledger* is rebuilt from the stored `AuditRecord` by [`replay.py`](../src/attest/replay.py).

**The resumed run goes through the `human_checkpoint` node, and the test that asserts so is the whole
feature.** Applying the decision straight to the stored record would be half the code and would create
a second path to the one thing in this system that must not have one — unaudited, invisible from
outside, taken only after a restart.

**"It resumes" was never the bar. "It resumes and the report is identical" is.** A restarted run that
reports something subtly different is invisible, and it is on the path a human uses to approve a change
to the catalog.

The sharpest thing that fell out of holding that bar: the step trace did not persist which **models** a
step called. `Trace.cost` reports a run's dollars as `None` — never `0` — when a model that spent tokens
has no price, and it finds those models *by name* off the step. Rebuild the trace without them and a
resumed run computes `usd = sum(...) = 0.0` where the original honestly said *unknown*: **a restarted
audit fabricating a cost figure the original refused to state.** That is None-is-not-zero breaking
inside Attest's own receipts.

**Scope this claim carefully.** Resume is strong for a run parked *before* a decision. It does **not**
make the three remote write operations atomic. What happens *after* the decision — the catalog write and
the store commit — is covered by a separate mechanism, below.

### Crash-recoverable settlement

Durable resume closes the window *before* the human decides. The window *after* — from the moment the
checkpoint node consumes the decision, through the three non-atomic catalog writes, to the store commit —
is closed by a **write-ahead intent**. Before the first catalog mutation, `approve` persists a durable
record of exactly what this settlement will do (the settled projection, the decisions, the claims it
publishes) and marks it unsettled. The catalog writes then run, and the settled record, the decision
rows, and the intent's flip to *settled* all commit in **one** store transaction. A process death
anywhere in between leaves the intent unsettled, and a fresh process replays it on startup: the three
writes are idempotent (same content-addressed artifact, same timestamp-keyed event), so replay collapses
onto the same history, and the store commit is atomic, so the decision log is never double-appended.
Recovery is therefore **re-entrant** — a death *during* recovery just leaves the intent unsettled for the
next pass.

Two things make this safe rather than a second way into the catalog. It **never runs the graph or invents
a decision** — it re-executes the side effect of a decision a human already made and the checkpoint node
already consumed, exactly as the repair endpoint does; that is why it works even though the crashed resume
already advanced the graph past the checkpoint. And the **hard gate stays upstream**: an intent is written
only after the resumed report passes trajectory verification, so a flagged run can never leave one behind.
This is deliberately **not** a distributed saga (every remote projection has a stable idempotency key, so
at-least-once replay suffices) and **not** a concurrent-settler guarantee (it is single-process,
single-settler, and says so).

**The residual, named:** a crash in the brief, purely in-memory window *before* the intent is persisted —
after the graph consumes the decision, before any catalog write — strands the run at a 409 with the
catalog untouched; the decision is re-made by re-auditing. Closing that too would mean recomputing the
settled projection *outside* the graph and colliding with the "a resumed run reports identically"
guarantee above, for a microsecond window with no external side effect — so it is documented, not chased.
Proven by a real SIGKILL at four catalog-write points and once during recovery
([`tests/test_settlement_recovery.py`](../tests/test_settlement_recovery.py)), falsified by
[`spikes/settle_sabotage.py`](../spikes/settle_sabotage.py). See the README's
[limitations](../README.md#scope-and-limitations).

### Concurrency

The service's lock is gone, and what replaced it is not a smaller lock. It was never about throughput:
one pipeline meant one LLM handle meant one shared token ledger, and two concurrent runs would have
billed each other. So the **sharing** went, not the safety — each run forks its own handle via
`LLM.for_run()`, sharing the HTTP transport and nothing else. Cross-billing is *unreachable* rather
than *prevented*.

The test is about the **receipts**, not about not-crashing: it holds one audit's first model call open
across the whole of another and asserts each receipt bills only its own tokens. With a shared handle,
run A is billed 480 tokens for 240 tokens of work. A concurrency fix that silently cross-bills is worse
than the queue it replaced. (Concurrent *audits* are tested; that is not a claim that arbitrary
concurrent approvals and process deaths are safe.)

## The append-only history, and its one collision boundary

A published claim is a CUSTOM Assertion plus an append-only timeseries of verdict events, so
"was this ever contradicted before someone fixed the tag" is answerable from the catalog
alone. That guarantee rests on two properties of the run event, both pinned against real GMS:

- **A retry does not double-count.** The event is keyed by `(urn, aspect, timestampMillis)`
  and the timestamp is the audit run's own `created_at`, so re-running a write-back — the
  repair path — re-reports the same verdict at the same key and collapses onto the same row.
  A retry that appended would forge a second audit in an append-only log.
- **A real re-audit appends.** A later audit has a later `created_at`, a different key, and a
  new event. The history is real history, not a last-write-wins field.

**The residual, named rather than hidden.** Two *distinct* runs that share a start-millisecond
**and disagree** collapse to one event, and the loser's verdict is lost. This cannot be keyed
away server-side: `AssertionResultInput` exposes **only `timestampMillis`** — no `messageId`,
`runId`, or `partitionSpec` (introspected on the pinned server) — so there is no field to carry
run identity into the key. Two things make it tolerable rather than a hole:

1. **It is unreachable by construction.** Two runs reach *different* verdicts only if the
   catalog changed between them — and the deterministic core returns the same verdict for the
   same snapshot, so a disagreement requires a catalog change *within the runs' shared
   millisecond*. Same-verdict collisions are harmless: they are the retry dedup, generalized.
2. **The alternatives are worse.** A write-time anomaly guard would read-before-write against
   an eventually-consistent index to catch a sub-millisecond collision, and would still
   birthday-collide; contorting the timestamp to carry run identity degrades its fidelity as a
   time. Both were considered and refused as padding. The honest move is to pin the two real
   guarantees and name the boundary.

## Entity-not-found is not a verdict — and neither is a malformed one

A claim about a dataset that does not exist surfaces as a `ClaimError`, kept out of `audits` entirely
and counted in no verdict tally. The catalog neither disagrees with the claim nor is silent about it —
the *question was malformed*, most likely a bad URN from upstream entity resolution. Scoring it
Insufficient-Coverage would launder a hallucinated URN into a legitimate-looking audit result, and the
bad URN would never be seen.

The same rule reaches one layer down, to a response that is *structurally* broken rather than absent
(Session 23). A present-but-URL-less association entry (`{"owner": null}`) once normalized to the
empty-string URN `''` — a populated-looking list of garbage that drove a confident Contradicted — and
a wrong-shaped response (a field with no `fieldPath`, a null `urn`) crashed the whole run. Both now
raise `MalformedResponseError`, a `DataHubError`, so `resolve` turns them into the same `ClaimError`:
not a verdict, not a crash. The line that must not be crossed is that a legitimately **empty** (`[]`)
or **absent** (`null`) aspect is a valid Insufficient-Coverage, never an error — absence is not
malformation, just as it is not disagreement.

A **future** `lastModified` is the same instinct in the freshness checker: `now - last_modified` is
negative, so a naive `age <= window` scores a bad upstream clock as *very fresh* and returns a
confident Supported. Beyond a small clock-skew grace it is Insufficient-Coverage with the implausible
value shown as evidence (distinct from the absent case, whose evidence is `None`); within the grace the
age clamps to zero, because a just-modified dataset genuinely is fresh. A bad clock is not a freshness
signal, exactly as a broken response is not a catalog reading.

## The cost projection

**A dated projection, not a receipt.** Prices, model mix and workload assumptions rot; this is
arithmetic over measured per-step token counts ([`cost.py`](../src/attest/cost.py), pinned by
[tests](../tests/test_cost.py)), not a committed benchmark artifact. For a number you can open, see the
[receipts table](../README.md#receipts-not-headlines).

*As of 2026-07-16, against `gpt-4o-mini` at then-current prices:*

| Per unit | Tokens | Cost |
| --- | --- | --- |
| One claim | 895 in / 216 out | $0.000264 |
| One correction attempt | 1140 in / 125 out | $0.000246 |

*Assumptions: 1000 claims/day, 50 datasets, one org, one correction attempt per contradicted claim.*

| Contradiction rate | $/day | $/month |
| --- | --- | --- |
| 5% | $0.28 | $8 |
| **10%** (nominal) | **$0.29** | **$9** |
| 25% | $0.36 | $11 |
| 100% (every claim wrong, cap spent) | $0.76 | $23 |

**It is not alarming, and that is itself the finding.** Token cost is not the constraint on continuous
monitoring at one-org scale, so it is *not* the argument for sampling — and it would have been easy to
assume it was. Three things the numbers actually say:

1. **The cost bound is structural, not aspirational.** Corrections, not claims, move a bill: a revision
   hands the evidence back to the model, making it the priciest call in the pipeline, and it fires
   *only* on Contradicted claims. The worst case is 2.86× the quiet case, and the reason that is a
   *ceiling* rather than a hope is that **the retry cap is a graph edge, not a runtime check**. There is
   no path through the graph that revises a claim three times. Raise the cap and the ceiling rises with
   it, deliberately and in one place.
2. **The real scaling pressure is catalog reads, not tokens.** `resolve_entity` used to fetch once per
   *claim*; the run-scoped snapshot fetches once per *entity*. At the nominal workload that is the
   difference between 1000 fetches and 50. The dollars were never the operating cost; the load on
   someone else's GMS was.
3. **Per-tenant budget caps are a multi-tenancy concern, not a single-org one.** Cost is linear in
   claims/day: ~$9/mo per org means ~$900/mo at 100 orgs. The cap matters *there*, and it should be
   enforced per tenant rather than globally.

**The scale tension, named rather than quietly relaxed:** nobody approves 1000 claims a day, and a
human checkpoint on every claim is what this build ships. A real deployment needs a policy layer for
bulk publication. That is a deliberate gap, not an oversight — if a deployment wants scale, it must
relax the rule consciously and write down that it did.
