# Attest — DataHub Agent Hackathon submission

**Category: Open / Wildcard.**

## What it is

**Attest is a groundedness auditor for AI agents that make claims about data.** An agent
writes *"`customer_profile` holds no PII."* Attest verifies that sentence against **DataHub
Core v1.5.0.6** as ground truth and returns one of three verdicts — Supported, Contradicted,
or Insufficient-Coverage — with **zero verdicts decided by a model**. Once a human publishes
one, Attest **writes it back into DataHub** as its own content-addressed claim artifact with
an append-only verdict history, so the next agent inherits it from the catalog rather than
from Attest's database. We also **built an adapter to the DataHub MCP Server, measured it
against all 16 seeded datasets, and wrote up three upstream issues with reproductions — two of
them filed** ([#169](https://github.com/acryldata/mcp-server-datahub/issues/169),
[#168](https://github.com/acryldata/mcp-server-datahub/issues/168); the third is deliberately
kept as a draft) — the measurement is why the verdict read stays on GraphQL, and it is runnable
as `just spike-mcp`.

## The problem

An agent summarises a warehouse for a governance review and says a table is PII-free. Nobody
checks it, because there is nothing to check it *against*. The table is tagged `PII`. Its
`email` column is tagged `PII`. The catalog said so the whole time.

The failure underneath that one is worse. The next table has **no tags at all**, and *"we
reviewed it and it's clean"* is indistinguishable from *"nobody has ever looked."* A tool that
reports those as the same thing cries wolf on every under-documented table in the warehouse —
which is most of them. That is why the third verdict exists and is load-bearing: **absence is
not disagreement.** Attest never assumes closed-world reasoning; the catalog grants it per
entity, through a `Verified` marker a human applied.

## Zero verdicts decided by a model — and how that is falsifiable

Freshness, ownership, classification and schema verdicts come from date math, set membership
and string comparison. `checkers/` imports no model client and never sees agent text. But
"the source doesn't import it" is only as good as the next commit, so that is not what the
guarantee rests on: **every pipeline step records what kind it is (`deterministic` / `llm` /
`io`) alongside its token spend.** The kind is what a step *claims*; the token count is the
*evidence*. A checker that quietly started calling a model fails the run even if its answers
are right — and a violating run is `FLAGGED` and **cannot be approved** (HTTP 409), so a
report the pipeline could not vouch for never reaches the catalog.

An invariant nobody can falsify is decoration, so: **break one checker on purpose and the
40-case benchmark falls from accuracy 1.000 to 0.675**, Supported precision to 0.536, with 9
correctness and 4 coverage failures named. That sabotage runs on **every** `just check`, not
on demand.

The model is used for exactly two things — decomposing prose into typed claims, and phrasing
an explanation — and the prose passes three gates (crosscheck, lexical faithfulness,
polarity) before it can ship. Any gate failing ships a deterministic template instead.

## What the next agent inherits

One claim becomes one **custom DataHub Assertion**, at a URN derived by sha256 over the
canonical claim, plus one appended run event per verdict. Two consequences, both
load-bearing: two claims about one dataset **coexist** (they hash differently), and
**re-running the write is safe** (no run id, no clock in the identity). A re-audit appends;
it never overwrites.

The proof is a reader constructed with **no Attest database at all** —
`ClaimReader(client, store=None)`. Every claim, verdict, reviewer and full history comes back
out of DataHub alone. That runs live, against real GMS.

**Nothing reaches the catalog without a human.** `POST /audit` writes nothing, ever. Every
audited claim parks for a per-claim decision, and `publish` is separate from
`accept_correction` — because *"your claim was wrong, and the fix you proposed is also
wrong"* is a thing a reviewer needs to be able to say. There is no `?auto_approve=true`.

## Engaging with the DataHub MCP Server

Challenge 1 names the MCP Server as how agents get catalog context, so rather than skip it we
built to the one-method seam an adapter would implement and measured whether its responses
carry the facts a deterministic verdict is made of.

**The server runs** — compatibility is the failure everyone expects and is not what happened:
correct OSS detection, correct version-gating, every call answered for every dataset. The
finding is about what those *successful* responses contain. **Measured on `mcp-server-datahub`
0.6.0 against the pinned Core: parity fails on 16/16 seeded datasets (130 field mismatches),
and four of five true claims change verdict — including `customer_profile.email is PII`
reading back Contradicted.** The tag arrives as the display name `"PII"`, the column reads
unlabelled, the table is `Verified`, and our own hard-won completeness rule turns the loss
into a confident denial. **A transport that is lossy for a language model is not merely lossy
for a checker — it is inverting.**

That is a finding about *structured consumers*, not a defect for the server's intended use,
where the compaction is a feature. Three of the four mechanisms are fixable upstream and are
written up with reproductions. `just spike-mcp` **exits non-zero by design**: if it ever goes
green, the finding has expired and the decision is worth reopening.

## Try it

```bash
just setup && just check      # offline: no DataHub, no API key, no cost. 417 tests, never skips.
just up && just seed          # DataHub Core v1.5.0.6 from a vendored, pinned compose
just demo                     # the UI and API on :8003
just spike-mcp                # the MCP evaluation, against your own catalog. Non-zero by design.
```

## What it does not do

Stated plainly, because omitting it is what would make the rest hollow. The benchmark runs
against a **seeded** catalog, not a messy real one, and its labels *apply* the policy rather
than *validate* it. The guards are **lexical detectors**; they do not prove arbitrary natural
language entailment. It is **local, not hosted, and unauthenticated**. The three catalog
writes are **sequential, not atomic** — though a process death mid-settlement is now replayed
from a durable write-ahead intent, proven by a real SIGKILL at four write points. There is no
bulk publication: a real deployment at the projected workload would need a policy layer, and
this is a hackathon build with a human checkpoint on every claim.

---

Built solo. Full engineering log — every invariant and why it exists — in
[CLAUDE.md](../CLAUDE.md).
