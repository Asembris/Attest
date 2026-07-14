"""The pipeline: a LangGraph state machine over the pieces the earlier sessions built.

sanitize -> decompose -> [per claim: resolve -> route -> check -> explain -> guard ->
maybe correct] -> human checkpoint -> report.

--------------------------------------------------------------------------------
Why a graph and not a for-loop
--------------------------------------------------------------------------------

This is a fair question and it deserves a real answer rather than a buzzword, because a
for-loop would in fact run these steps in this order. Four things the graph buys, each of
which is a property a loop would have to be trusted to maintain:

**1. The retry cap is an edge, not a counter.** Self-correction is a cycle in the graph
(revise -> recheck -> revise), and the only way out is a conditional edge that reads the
retry count. A `while` loop with a `break` is one careless edit away from unbounded, and
an unbounded correction loop against a paid API is a cost bug that ships silently. Here the
bound is topological, and trajectory.py asserts after the fact that it held.

**2. The human checkpoint is a real pause.** `interrupt_before` parks the run mid-graph,
with its state intact and durable. The graph does not "return a flag saying a human should
look at this" — it *stops*, and it cannot proceed until someone resumes it. That is the
difference between a checkpoint and a to-do comment, and it is the whole accountability
argument (see below).

**3. Routing by claim type is a topological fact, not an `if`.** Each claim type has its
own checker node, and the router sends it there. Because the trace records which node ran,
a misrouted claim is *catchable* — trajectory.py fails a freshness claim that was answered
by the ownership checker. In a for-loop, the dispatch is a `match` statement that agrees
with itself by construction and cannot be audited from outside.

**4. The trajectory is a record, not a story.** Every node records a step with its kind
(deterministic / llm / io), latency, and token spend. That record is what lets Attest prove
its central claim — no model touches a verdict — instead of asserting it. See trajectory.py.

--------------------------------------------------------------------------------
The human-in-the-loop checkpoint: an accountability choice, not a limitation
--------------------------------------------------------------------------------

When a claim is Contradicted, Attest asks the source agent to revise it, and re-verifies
the revision against the same catalog with the same deterministic checker. A revision that
comes back Supported is *still not applied*. It becomes a PROPOSAL, the graph parks at
`human_checkpoint`, and it stays PENDING until a person accepts it.

This is deliberate, and it is not because the loop is unreliable. It is because an auditor
that silently rewrites the thing it audits has stopped being an auditor. The value of
Attest is that a human can point at any verdict and see the catalog field it came from; a
correction that Attest applied to itself, on the strength of a model's revision, would be
the one fact in the system with no independent source. So the default disposition of an
unattended correction is *unreviewed* — never accepted, and never quietly written back.
`ReviewStatus.PENDING` is the resting state, and a run that nobody looks at proposes
changes to nobody.

--------------------------------------------------------------------------------
Why the typed audit ledger sits beside the graph state, not inside it
--------------------------------------------------------------------------------

LangGraph's checkpointer serializes state through msgpack, and it currently deserializes
unregistered classes with a warning that says it "will be blocked in a future version".
Attest's state is full of exactly those: pydantic claims, frozen dataclass explanations,
Faithfulness records. They round-trip today; building the pipeline on that would be
building on a deprecation, and the failure it would eventually produce — typed objects
coming back as bare dicts after a resume — is the kind that surfaces as an AttributeError
in a demo.

So the graph's state holds only primitives (cursor, retry count, decisions), which is all
the *control flow* needs, and the typed audit record lives in a ledger keyed by thread id.
The checkpoint is no less real for it: the interrupt is a genuine pause of a genuine graph.
Only the accumulating output is kept where a serializer cannot maim it.

--------------------------------------------------------------------------------
Durable resume (Session 5): TWO durable things, and only one of them is LangGraph's
--------------------------------------------------------------------------------

The pause is durable because the checkpointer is (`SqliteSaver`, injected — see `saver`).
The typed ledger is durable because the STORE is: a run's verdicts, evidence, explanations
and receipts are written down the moment the run returns, parked or not. So a restart
loses neither, and `rehydrate` puts them back together — the graph's own state from
LangGraph's tables, the typed record from Attest's, rebuilt by replay.py.

**The resumed run goes through `human_checkpoint`, exactly like an unrestarted one.** That
is the point of rehydrating rather than applying the decision straight to the stored
record, which would have been half the code and would have created a second path to the
one thing in this system that must not have one — an unaudited one, invisible from the
outside, taken only after a restart. `tests/test_resume.py` asserts the node appears in the
resumed trace, and that assertion is the whole difference between one durable path and two.

--------------------------------------------------------------------------------
One LLM handle per run (Session 5): the receipts are per-run BY CONSTRUCTION
--------------------------------------------------------------------------------

observe.py bills a step by slicing `llm.usage` from a mark taken when the step opened, so a
handle shared by two concurrent runs bills each run for the other's tokens. Until Session 5
a lock in the service made that unreachable by serializing audits. The lock is gone, and
what replaces it is not a smaller lock: the LLM handle now lives on the `_Ledger` — one per
run, forked from the pipeline's (llm.py `for_run`), sharing the transport and nothing else.
Cross-billing is not forbidden by a convention, it is unreachable, in the same way that
cross-run snapshot reuse is unreachable (datahub/cache.py). `tests/test_concurrency.py`
runs two audits through one service, with one deliberately held open across the whole of
the other, and asserts each receipt bills only its own tokens.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from attest import faithfulness, replay, trajectory
from attest.checkers import (
    check_classification,
    check_freshness,
    check_ownership,
    check_schema,
)
from attest.claims import (
    CheckResult,
    Claim,
    ClaimType,
    ClassificationClaim,
    FreshnessClaim,
    OwnershipClaim,
    SchemaClaim,
    Verdict,
)
from attest.datahub import (
    CacheStats,
    DataHubClient,
    DataHubError,
    DatasetSnapshot,
    SnapshotCache,
)
from attest.decompose import Dropped, decompose
from attest.explain import Explanation, explain, template
from attest.llm import LLM
from attest.observe import StepKind, Trace
from attest.record import AuditRecord
from attest.report import (
    Attempt,
    AuditReport,
    ClaimAudit,
    ClaimError,
    Correction,
    CorrectionOutcome,
    Decision,
    ReviewStatus,
    RunStatus,
)
from attest.revise import Revision, revise
from attest.sanitize import Finding, sanitize
from attest.trajectory import (
    ASSEMBLE,
    CHECKER,
    CHECKPOINT,
    DECOMPOSE,
    EXPLAIN,
    GUARD,
    MAX_RETRIES,
    RECHECK,
    RESOLVE,
    REVISE,
    SANITIZE,
    AuditedClaim,
)

log = logging.getLogger(__name__)

NEXT_CLAIM = "next_claim"


class AuditState(TypedDict, total=False):
    """The graph's state. PRIMITIVES ONLY — see the module docstring for why.

    Everything here survives a checkpoint round-trip as a plain msgpack scalar. The typed
    audit record lives in the ledger, keyed by `thread_id`.
    """

    thread_id: str
    source_text: str
    # Index of the claim being worked on, and how many claims there are to work on.
    cursor: int
    n_claims: int
    # Revisions spent on the CURRENT claim. Reset at each claim. The retry cap reads this.
    retries: int
    # claim index (as a string key, because msgpack maps want string keys) -> accept.
    # Written by a human via `resume`, never by the graph.
    decisions: dict[str, bool]
    run_error: str | None


@dataclass
class _Ledger:
    """One run's typed audit record. Held beside the graph, not inside it."""

    source_text: str
    # The run's view of the catalog. Created per run, held here, and dropped with the
    # ledger — which is what makes cross-run reuse structurally unreachable rather than
    # merely discouraged: the Pipeline holds no cache of its own for a second run to find.
    # See datahub/cache.py — a snapshot carried across runs is a stale ground truth, which
    # is the exact failure Attest exists to catch.
    catalog: SnapshotCache
    # The run's model handle, with the run's OWN token ledger. Same argument as the cache,
    # one floor down: the Pipeline holds a handle, but no run uses it — each forks its own
    # (llm.py `for_run`), so two concurrent audits cannot bill each other's tokens. That is
    # what replaced the lock in api/service.py, and it is structural rather than
    # disciplinary for the same reason the cache is.
    llm: LLM
    trace: Trace = field(default_factory=Trace)

    text: str = ""
    findings: tuple[Finding, ...] = ()
    claims: tuple[Claim, ...] = ()
    dropped: tuple[Dropped, ...] = ()

    audits: list[ClaimAudit] = field(default_factory=list)
    errors: list[ClaimError] = field(default_factory=list)

    # A REHYDRATED run's catalog receipt, replayed from the store. The original run did
    # read the catalog; this process did not, and its cache is empty and must stay so — a
    # resumed run resolves nothing. Reporting the empty cache's 0 fetches would understate
    # what the audit actually cost DataHub, which is a receipt, and the receipts are the
    # product. None on a live run: the cache speaks for itself. See replay.py.
    replayed_catalog: CacheStats | None = None

    # --- scratch for the claim currently in flight ----------------------------
    snapshot: DatasetSnapshot | None = None
    result: CheckResult | None = None
    explanation: Explanation | None = None
    attempts: list[Attempt] = field(default_factory=list)
    revision: Revision | None = None
    proposal: Claim | None = None
    outcome: CorrectionOutcome = CorrectionOutcome.NOT_ATTEMPTED

    @property
    def catalog_stats(self) -> CacheStats:
        return self.replayed_catalog or self.catalog.stats

    def reset_claim(self) -> None:
        self.snapshot = None
        self.result = None
        self.explanation = None
        self.attempts = []
        self.revision = None
        self.proposal = None
        self.outcome = CorrectionOutcome.NOT_ATTEMPTED


class Pipeline:
    """Attest's audit pipeline. Build once, run many times, concurrently.

    `llm`, `client` and `saver` are injected so the whole graph tests offline against a
    scripted fake, a fixture catalog and an in-memory checkpointer — the same discipline as
    llm.py. `now` is injected for the same reason conftest derives it from the catalog: a
    freshness verdict that depends on the wall clock is a test that goes red in a fortnight
    against correct code.

    The `llm` handed in here is a TEMPLATE, not the handle a run uses. Every run forks its
    own from it (`LLM.for_run`) and puts it on that run's ledger, which is what makes token
    billing per-run by construction — see the module docstring, and the lock that used to
    be in api/service.py and is not any more.

    `saver` is what makes a parked run survive a restart. In-memory by default (a CLI run
    and the whole offline suite have no restart to survive); the service passes a
    `SqliteSaver`. A pause the checkpointer does not know about cannot be resumed, and
    `rehydrate` says so rather than inventing one.
    """

    def __init__(
        self,
        llm: LLM | None = None,
        client: DataHubClient | None = None,
        now: datetime | None = None,
        max_retries: int = MAX_RETRIES,
        saver: BaseCheckpointSaver | None = None,
    ) -> None:
        self.llm = llm or LLM()
        self.client = client or DataHubClient()
        self.now = now
        self.max_retries = max_retries
        self.saver: BaseCheckpointSaver = saver or InMemorySaver()
        self._ledgers: dict[str, _Ledger] = {}
        self.graph = self._build()

    # --- graph construction ---------------------------------------------------

    def _build(self) -> Any:
        g: StateGraph = StateGraph(AuditState)

        g.add_node(SANITIZE, self._sanitize)
        g.add_node(DECOMPOSE, self._decompose)
        g.add_node(RESOLVE, self._resolve)
        for node in CHECKER.values():
            g.add_node(node, self._checker_node(node))
        g.add_node(EXPLAIN, self._explain)
        g.add_node(GUARD, self._guard)
        g.add_node(REVISE, self._revise)
        g.add_node(RECHECK, self._recheck)
        g.add_node(NEXT_CLAIM, self._next_claim)
        g.add_node(CHECKPOINT, self._checkpoint)
        g.add_node(ASSEMBLE, self._assemble)

        g.add_edge(START, SANITIZE)
        g.add_edge(SANITIZE, DECOMPOSE)

        # Nothing to audit is a legitimate outcome, not a crash: an agent's output may
        # contain no checkable claim at all, and the report has to say so.
        g.add_conditional_edges(
            DECOMPOSE,
            self._has_claims,
            {"audit": RESOLVE, "nothing": ASSEMBLE},
        )

        # THE ROUTER. One checker node per claim type; a claim whose entity could not be
        # resolved skips the whole verdict path rather than being scored from nothing.
        g.add_conditional_edges(
            RESOLVE,
            self._route_by_claim_type,
            {**{t.value: node for t, node in CHECKER.items()}, "unresolved": NEXT_CLAIM},
        )

        # Every checker lands on the explanation step. This is the ONLY place a claim's
        # path touches a model, and trajectory.py asserts it.
        for node in CHECKER.values():
            g.add_edge(node, EXPLAIN)
        g.add_edge(EXPLAIN, GUARD)

        # THE SELF-CORRECTION CYCLE. Contradicted -> revise -> recheck -> (revise | done).
        g.add_conditional_edges(
            GUARD,
            self._needs_correction,
            {"correct": REVISE, "done": NEXT_CLAIM},
        )
        # A revision that changed the subject, or stood firm, is terminal: at
        # temperature=0 the same prompt returns the same refusal, so retrying it would
        # burn a call to reach the same place.
        g.add_conditional_edges(
            REVISE,
            self._revision_usable,
            {"verify": RECHECK, "give-up": NEXT_CLAIM},
        )
        # The retry cap, as an edge. This is the bound, and it is topological.
        g.add_conditional_edges(
            RECHECK,
            self._retry_or_stop,
            {"retry": REVISE, "done": NEXT_CLAIM},
        )

        g.add_conditional_edges(
            NEXT_CLAIM,
            self._more_claims,
            {"more": RESOLVE, "review": CHECKPOINT, "finish": ASSEMBLE},
        )
        g.add_edge(CHECKPOINT, ASSEMBLE)
        g.add_edge(ASSEMBLE, END)

        # interrupt_before is what makes the checkpoint a PAUSE rather than a flag: the run
        # stops with its state intact and cannot proceed until a human resumes it. The
        # saver is what makes that pause outlive the process.
        return g.compile(checkpointer=self.saver, interrupt_before=[CHECKPOINT])

    # --- nodes ----------------------------------------------------------------

    def _ledger(self, state: AuditState) -> _Ledger:
        return self._ledgers[state["thread_id"]]

    def _sanitize(self, state: AuditState) -> AuditState:
        ledger = self._ledger(state)
        source = state["source_text"]
        with ledger.trace.step(SANITIZE, StepKind.DETERMINISTIC, chars=len(source)) as s:
            cleaned = sanitize(source)
            ledger.text = cleaned.text
            ledger.findings = cleaned.findings
            s.outputs = {"redactions": len(cleaned.findings)}
        return {}

    def _decompose(self, state: AuditState) -> AuditState:
        ledger = self._ledger(state)
        with ledger.trace.step(DECOMPOSE, StepKind.LLM, llm=ledger.llm) as s:
            # decompose() sanitizes again, which is idempotent on already-clean text. The
            # SANITIZE node exists so the redaction is a visible stage of the trajectory
            # rather than a side effect buried inside the decomposer.
            result = decompose(ledger.text, llm=ledger.llm)
            ledger.claims = result.claims
            ledger.dropped = result.dropped
            s.outputs = {"claims": len(result.claims), "dropped": len(result.dropped)}

        return {"n_claims": len(ledger.claims), "cursor": 0, "retries": 0}

    def _resolve(self, state: AuditState) -> AuditState:
        """Fetch the claim's entity from DataHub. IO, no decision."""
        ledger = self._ledger(state)
        ledger.reset_claim()
        i = state["cursor"]
        claim = ledger.claims[i]

        with ledger.trace.step(
            RESOLVE, StepKind.IO, claim_index=i, urn=claim.target_urn
        ) as s:
            # Read through the RUN's cache, never the client directly. Two claims about
            # one dataset are decided against one snapshot — the consistency argument in
            # datahub/cache.py, and the reason this is not merely an optimization.
            cached = claim.target_urn in ledger.catalog
            try:
                ledger.snapshot = ledger.catalog.fetch_dataset(claim.target_urn)
                s.outputs = {"resolved": True, "cached": cached}
            except DataHubError as exc:
                # A missing entity is an ERROR, not a verdict — see report.ClaimError.
                # Recorded on the step and carried out of the verdict path entirely.
                s.error = str(exc)
                s.outputs = {"resolved": False, "cached": cached}
                ledger.errors.append(ClaimError(index=i, claim=claim, error=str(exc)))
                log.warning("could not resolve %s: %s", claim.target_urn, exc)

        return {"retries": 0}

    def _checker_node(self, name: str) -> Any:
        """One deterministic checker node. No model, and trajectory.py proves it."""

        def node(state: AuditState) -> AuditState:
            ledger = self._ledger(state)
            i = state["cursor"]
            claim = ledger.claims[i]
            snapshot = ledger.snapshot
            assert snapshot is not None  # the router guarantees it

            # `llm` IS passed, even though a checker must never call a model — precisely
            # BECAUSE it must never call one. Passing the handle is what arms the trap: any
            # tokens spent under this step get billed to it, and trajectory.py fails a
            # verdict step that spent any. Withholding it would leave the step unable to
            # bill tokens, and therefore unable to DETECT them, which is the opposite of
            # what is wanted. The kind is the claim this node makes about itself; the token
            # count is the evidence that checks it.
            with ledger.trace.step(
                name,
                StepKind.DETERMINISTIC,
                claim_index=i,
                llm=ledger.llm,
                urn=claim.target_urn,
            ) as s:
                ledger.result = _CHECK[name](claim, snapshot, self.now)
                s.outputs = {"verdict": ledger.result.verdict.value}
            return {}

        return node

    def _explain(self, state: AuditState) -> AuditState:
        """Phrase the verdict. The model's only turn on this claim, and it cannot move it."""
        ledger = self._ledger(state)
        i = state["cursor"]
        result = ledger.result
        assert result is not None

        with ledger.trace.step(
            EXPLAIN, StepKind.LLM, claim_index=i, llm=ledger.llm, verdict=result.verdict.value
        ) as s:
            ledger.explanation = explain(result, llm=ledger.llm)
            s.outputs = {
                "source": ledger.explanation.source,
                "conflicts": len(ledger.explanation.conflicts),
                "rejected_drafts": len(ledger.explanation.rejected),
            }
        return {}

    def _guard(self, state: AuditState) -> AuditState:
        """The last thing between the model and the reader. Deterministic, and final.

        explain() already runs the faithfulness guard internally and falls back to the
        template on rejection, so in the normal case this re-check passes trivially. It is
        a node anyway, and it re-checks the text that is ACTUALLY about to ship, because
        "the explanation was guarded" must be a property of the pipeline rather than a
        promise made by one function. If explain() were ever refactored into shipping an
        unchecked draft, this is what catches it — and trajectory.py asserts that this node
        ran on every explanation in the report.
        """
        ledger = self._ledger(state)
        i = state["cursor"]
        result = ledger.result
        written = ledger.explanation
        assert result is not None and written is not None

        with ledger.trace.step(
            GUARD, StepKind.DETERMINISTIC, claim_index=i, llm=ledger.llm
        ) as s:
            verified = faithfulness.check(written.text, result)
            if not verified.ok:
                # Should be unreachable via explain(). If it happens, the prose does not
                # ship: we degrade to something true rather than something plausible.
                log.error(
                    "an explanation reached the guard unfaithful — falling back: %s",
                    verified.summary,
                )
                fallback = template(result)
                ledger.explanation = Explanation(
                    text=fallback,
                    source="template",
                    faithfulness=faithfulness.check(fallback, result),
                    conflicts=written.conflicts,
                    rejected=(*written.rejected, f"guard: {verified.summary}"),
                )
            s.outputs = {
                "faithful": verified.ok,
                "shipped": ledger.explanation.source,
            }
        return {}

    def _revise(self, state: AuditState) -> AuditState:
        """Hand the contradicted claim back to the agent, with the catalog's facts."""
        ledger = self._ledger(state)
        i = state["cursor"]
        result = ledger.result
        assert result is not None

        attempt = state.get("retries", 0) + 1
        with ledger.trace.step(
            REVISE, StepKind.LLM, claim_index=i, llm=ledger.llm, attempt=attempt
        ) as s:
            ledger.revision = revise(result, llm=ledger.llm)
            s.outputs = {
                "revised": ledger.revision.ok,
                "rejected": ledger.revision.rejected,
            }

        return {"retries": attempt}

    def _recheck(self, state: AuditState) -> AuditState:
        """Re-verify the revision. Same checker, same snapshot. Code decides, not the model.

        The same snapshot on purpose: the agent is being held to the facts it was SHOWN.
        Re-fetching would let the catalog move underneath the loop, and a correction could
        then pass or fail for a reason nobody in the transcript ever saw.
        """
        ledger = self._ledger(state)
        i = state["cursor"]
        revision = ledger.revision
        snapshot = ledger.snapshot
        assert revision is not None and revision.claim is not None and snapshot is not None

        revised = revision.claim
        with ledger.trace.step(
            RECHECK,
            StepKind.DETERMINISTIC,
            claim_index=i,
            llm=ledger.llm,  # arms the token trap: a re-check that calls a model is not one
            attempt=state["retries"],
        ) as s:
            result = _CHECK[CHECKER[revised.claim_type]](revised, snapshot, self.now)
            s.outputs = {"verdict": result.verdict.value}

        ledger.attempts.append(
            Attempt(
                n=state["retries"],
                revised_claim=revised,
                verdict=result.verdict,
                reason=result.reason,
            )
        )

        # A correction stands only if the catalog now SUPPORTS the revised claim. Anything
        # else is not a correction: an agent that retreats from a false claim into an
        # unverifiable one has not corrected itself, it has stopped being checkable.
        if result.verdict is Verdict.SUPPORTED:
            ledger.proposal = revised
            ledger.outcome = CorrectionOutcome.CORRECTED
        else:
            ledger.outcome = CorrectionOutcome.NOT_CORRECTED

        return {}

    def _next_claim(self, state: AuditState) -> AuditState:
        """Close the books on this claim and move the cursor."""
        ledger = self._ledger(state)
        i = state["cursor"]

        if ledger.result is not None and ledger.explanation is not None:
            ledger.audits.append(
                ClaimAudit.from_check(
                    index=i,
                    result=ledger.result,
                    explanation=ledger.explanation,
                    correction=Correction(
                        outcome=self._final_outcome(state),
                        attempts=tuple(ledger.attempts),
                        proposal=ledger.proposal,
                        review=ReviewStatus.PENDING,
                    ),
                )
            )

        return {"cursor": i + 1, "retries": 0}

    def _final_outcome(self, state: AuditState) -> CorrectionOutcome:
        """Name what happened to the correction. Six answers, and the loop earns one."""
        ledger = self._ledger(state)

        if ledger.result is None or ledger.result.verdict is not Verdict.CONTRADICTED:
            return CorrectionOutcome.NOT_ATTEMPTED
        if ledger.outcome is CorrectionOutcome.CORRECTED:
            return CorrectionOutcome.CORRECTED

        revision = ledger.revision
        if revision is None:
            # The agent was never asked — the loop is switched off (max_retries=0). Saying
            # a correction was EXHAUSTED when none was attempted would report a failure
            # that never happened, on a claim nobody tried to fix.
            return CorrectionOutcome.NOT_ATTEMPTED
        if not revision.ok:
            return (
                CorrectionOutcome.STOOD_FIRM
                if revision.stood_firm
                else CorrectionOutcome.REFUSED
            )
        # Revised, re-verified, still not Supported. Whether that is EXHAUSTED or merely
        # NOT_CORRECTED is a question about the cap, and only the cap can answer it.
        if state.get("retries", 0) >= self.max_retries:
            return CorrectionOutcome.EXHAUSTED
        return CorrectionOutcome.NOT_CORRECTED

    def _checkpoint(self, state: AuditState) -> AuditState:
        """The human's turn. Reached only when there is something to sign off.

        The graph is interrupted BEFORE this node, so it does not run until someone calls
        `resume`. What it does is apply their decisions — and a proposal nobody decided on
        stays PENDING, which is the whole point.
        """
        ledger = self._ledger(state)
        decisions = state.get("decisions") or {}

        with ledger.trace.step(
            CHECKPOINT, StepKind.DETERMINISTIC, proposals=len(ledger.audits)
        ) as s:
            reviewed = 0
            for n, audit in enumerate(ledger.audits):
                if not audit.correction.awaits_human:
                    continue
                verdict = decisions.get(str(audit.index))
                if verdict is None:
                    continue  # unreviewed stays PENDING. Never accepted by default.
                ledger.audits[n] = _with_review(
                    audit,
                    ReviewStatus.ACCEPTED if verdict else ReviewStatus.REJECTED,
                )
                reviewed += 1
            s.outputs = {"reviewed": reviewed}

        return {}

    def _assemble(self, state: AuditState) -> AuditState:
        ledger = self._ledger(state)
        with ledger.trace.step(ASSEMBLE, StepKind.DETERMINISTIC, claims=len(ledger.audits)):
            pass
        return {}

    # --- routers --------------------------------------------------------------

    def _has_claims(self, state: AuditState) -> Literal["audit", "nothing"]:
        return "audit" if state["n_claims"] else "nothing"

    def _route_by_claim_type(self, state: AuditState) -> str:
        """THE ROUTER. Claim type -> its checker. An unresolved entity gets no verdict."""
        ledger = self._ledger(state)
        if ledger.snapshot is None:
            return "unresolved"
        return ledger.claims[state["cursor"]].claim_type.value

    def _needs_correction(self, state: AuditState) -> Literal["correct", "done"]:
        ledger = self._ledger(state)
        contradicted = (
            ledger.result is not None and ledger.result.verdict is Verdict.CONTRADICTED
        )
        # `max_retries=0` disables the loop outright, which is how a caller who wants a
        # pure scorer gets one.
        return "correct" if contradicted and self.max_retries > 0 else "done"

    def _revision_usable(self, state: AuditState) -> Literal["verify", "give-up"]:
        ledger = self._ledger(state)
        return "verify" if ledger.revision and ledger.revision.ok else "give-up"

    def _retry_or_stop(self, state: AuditState) -> Literal["retry", "done"]:
        """THE CAP. The only edge out of the correction cycle, and it counts."""
        ledger = self._ledger(state)
        if ledger.outcome is CorrectionOutcome.CORRECTED:
            return "done"
        if state["retries"] >= self.max_retries:
            return "done"
        return "retry"

    def _more_claims(self, state: AuditState) -> Literal["more", "review", "finish"]:
        if state["cursor"] < state["n_claims"]:
            return "more"
        # Park for a human only if there is actually something to sign off. A run with no
        # corrections should not demand attention it does not need.
        ledger = self._ledger(state)
        pending = any(a.correction.awaits_human for a in ledger.audits)
        return "review" if pending else "finish"

    # --- the public API -------------------------------------------------------

    def run(self, source_text: str, thread_id: str | None = None) -> AuditReport:
        """Audit an agent's output.

        Returns a COMPLETE report, or — if the run produced corrections that a human has
        to sign off — an AWAITING_REVIEW report with the graph parked at the checkpoint.
        The verdicts in a parked report are final and readable; only the CORRECTIONS are
        unsettled. Call `resume` to settle them.
        """
        thread_id = thread_id or str(uuid.uuid4())
        self._ledgers[thread_id] = _Ledger(
            source_text=source_text,
            # A fresh cache, for this run only. Never reused, never shared.
            catalog=SnapshotCache(self.client),
            # A fresh token ledger, for this run only. Same argument: two runs sharing one
            # would bill each other, and the receipts are the product.
            llm=self.llm.for_run(),
        )

        state: AuditState = {
            "thread_id": thread_id,
            "source_text": source_text,
            "cursor": 0,
            "n_claims": 0,
            "retries": 0,
            "decisions": {},
            "run_error": None,
        }
        self.graph.invoke(state, self._config(thread_id))
        return self._report(thread_id)

    def resume(
        self, thread_id: str, decisions: list[Decision] | None = None
    ) -> AuditReport:
        """Sign off (or reject) the proposed corrections, and finish the run.

        A proposal not named in `decisions` stays PENDING. There is no "approve all"
        default, and its absence is the design: see the module docstring.
        """
        config = self._config(thread_id)
        chosen = {str(d.claim_index): d.accept for d in decisions or []}

        self.graph.update_state(config, {"decisions": chosen})
        self.graph.invoke(None, config)
        return self._report(thread_id)

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    def _report(self, thread_id: str) -> AuditReport:
        """Build the report from the ledger, and hold the run to its own architecture."""
        ledger = self._ledgers[thread_id]
        snapshot = self.graph.get_state(self._config(thread_id))

        parked = CHECKPOINT in (snapshot.next or ())
        status = RunStatus.AWAITING_REVIEW if parked else RunStatus.COMPLETE

        verified = trajectory.verify(
            ledger.trace,
            tuple(
                AuditedClaim(
                    index=a.index,
                    claim_type=a.claim.claim_type,
                    has_verdict=True,
                    has_explanation=bool(a.explanation.text),
                    correction_attempts=len(a.correction.attempts),
                    has_proposal=a.correction.proposal is not None,
                )
                for a in ledger.audits
            ),
            max_retries=self.max_retries,
        )
        if not verified.ok:
            # Loud, because a trajectory violation means the pipeline did not do what this
            # report says it did — which makes the report itself untrustworthy.
            log.error("TRAJECTORY VIOLATION: %s", verified.summary)

        return AuditReport(
            source_text=ledger.source_text,
            audits=tuple(ledger.audits),
            errors=tuple(ledger.errors),
            status=status,
            trace=ledger.trace,
            trajectory=verified,
            dropped=ledger.dropped,
            injection_findings=ledger.findings,
            thread_id=thread_id,
            catalog=ledger.catalog_stats,
        )

    # --- durable resume -------------------------------------------------------

    def is_parked(self, thread_id: str) -> bool:
        """Is the GRAPH stopped at the human checkpoint for this run?

        A question for the checkpointer, not for this process's memory. With a durable
        saver it is still true after a restart, which is what makes `rehydrate` possible;
        with the in-memory one it is not, and a caller is told that rather than being
        handed a shortcut.
        """
        state = self.graph.get_state(self._config(thread_id))
        return CHECKPOINT in (state.next or ())

    def rehydrate(self, thread_id: str, record: AuditRecord) -> bool:
        """Rebuild a parked run's typed ledger from the store, so `resume` can finish it.

        Two halves of one run, from two durable places. The graph's own state — cursor,
        retry counts, where it stopped — comes back from the checkpointer on its own. The
        typed audit record cannot be in that state (the msgpack landmine, see above), so it
        is rebuilt from what the store wrote down when the run parked. replay.py does the
        rebuilding, and it is deliberately lossless in every field the report is made of.

        Returns False when the CHECKPOINTER has no parked graph for this thread. That is
        the one thing rehydration cannot manufacture, and it must not: a decision applied
        to a run whose checkpoint node never ran is exactly the unaudited second path this
        design exists to forbid. Better a 409 than a shortcut.
        """
        if thread_id in self._ledgers:
            return True  # never parked by a dead process — the live ledger is authoritative
        if not self.is_parked(thread_id):
            return False

        replayed = replay.from_record(record)
        self._ledgers[thread_id] = _Ledger(
            source_text=replayed.source_text,
            # Empty, and it stays empty: a resumed run resolves nothing. The ORIGINAL run's
            # catalog receipt is carried separately rather than re-derived from this — see
            # `replayed_catalog`, and datahub/cache.py on why a snapshot must never cross a
            # run boundary, let alone a process one.
            catalog=SnapshotCache(self.client),
            llm=self.llm.for_run(),
            trace=replayed.trace,
            claims=replayed.claims,
            dropped=replayed.dropped,
            findings=replayed.findings,
            audits=replayed.audits,
            errors=replayed.errors,
            replayed_catalog=replayed.catalog,
        )
        return True

    def forget(self, thread_id: str) -> None:
        """Drop a finished run's ledger and its checkpoints. Nothing here is resumable after.

        The pipeline is built once and run many times, so in a long-lived process (the API
        is one) the ledgers would otherwise accumulate for the life of the process, each
        pinning a run's catalog snapshots — and, now, the checkpointer's tables would grow a
        graph state per audit forever. Callers that have persisted a COMPLETE report have no
        further use for either and should say so.

        A run parked at the checkpoint must NOT be forgotten. Its typed record could be
        rebuilt from the store, but its PAUSE could not: delete the checkpoint and the only
        honest answer to a later approval is that the run cannot be resumed.
        """
        self._ledgers.pop(thread_id, None)
        self.saver.delete_thread(thread_id)


def _with_review(audit: ClaimAudit, review: ReviewStatus) -> ClaimAudit:
    """A reviewed copy. Correction is frozen, so a decision replaces rather than mutates."""
    return ClaimAudit(
        index=audit.index,
        claim=audit.claim,
        verdict=audit.verdict,
        reason=audit.reason,
        evidence=audit.evidence,
        explanation=audit.explanation,
        correction=Correction(
            outcome=audit.correction.outcome,
            attempts=audit.correction.attempts,
            proposal=audit.correction.proposal,
            review=review,
        ),
    )


def _freshness(claim: Claim, snap: DatasetSnapshot, now: datetime | None) -> CheckResult:
    assert isinstance(claim, FreshnessClaim)
    return check_freshness(claim, snap, now=now)


def _ownership(claim: Claim, snap: DatasetSnapshot, now: datetime | None) -> CheckResult:
    assert isinstance(claim, OwnershipClaim)
    return check_ownership(claim, snap)


def _classification(
    claim: Claim, snap: DatasetSnapshot, now: datetime | None
) -> CheckResult:
    assert isinstance(claim, ClassificationClaim)
    return check_classification(claim, snap)


def _schema(claim: Claim, snap: DatasetSnapshot, now: datetime | None) -> CheckResult:
    assert isinstance(claim, SchemaClaim)
    return check_schema(claim, snap)


# Node name -> the checker it runs. Keyed by NODE, not by claim type, so the router and
# the trajectory verifier are talking about the same thing: if a claim reaches the wrong
# node, it runs the wrong checker and trajectory.py catches it. A dispatch keyed by claim
# type could not be wrong, and therefore could not be audited.
_CHECK: dict[str, Any] = {
    CHECKER[ClaimType.FRESHNESS]: _freshness,
    CHECKER[ClaimType.OWNERSHIP]: _ownership,
    CHECKER[ClaimType.CLASSIFICATION]: _classification,
    CHECKER[ClaimType.SCHEMA]: _schema,
}
