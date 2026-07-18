"""The service: run an audit, persist it, and settle its proposals.

Everything the HTTP layer does that is not HTTP. app.py is routing and status codes; this
is where a run is executed, written down, and — only when a human says so — pushed back to
the catalog.

--------------------------------------------------------------------------------
Audits run CONCURRENTLY, and the thing that made that safe is not a smaller lock
--------------------------------------------------------------------------------

Until Session 5 this class took a lock around every audit, and the reason was token
accounting rather than throughput: one `Pipeline` meant one `LLM` handle, which meant ONE
shared `llm.usage` list, and observe.Trace bills a step by slicing that list from a mark
(observe.py). Two audits through one handle would bill each other — run A's decompose step
charged with whatever run B spent while A was in flight. The receipts ARE the product, and a
cost-per-claim figure that silently mixes two runs is exactly the unfounded number Attest
exists to catch, printed by Attest itself.

The fix was to remove the sharing, not to guard it. Each run now forks its own LLM handle
onto its own ledger (`LLM.for_run`, graph.py), sharing the HTTP transport underneath and
nothing else, so cross-billing is unreachable rather than merely prevented — a lock is a
convention, and the next person to hold it wrong is not stopped by a comment. Everything
else here was already per-run or already thread-safe: the ledgers and the checkpointer are
keyed by thread id, the snapshot cache lives on the ledger, the store has its own lock, and
httpx's client is thread-safe.

So the lock is gone, and two users no longer queue behind each other.
`tests/test_concurrency.py` is the proof, and it is a proof about the RECEIPTS: two audits
run at once, one held open across the whole of the other, and each bills only its own
tokens. A concurrency fix that silently cross-billed would be worse than the queue it
replaced.

--------------------------------------------------------------------------------
A parked run survives a restart, and it is resumed through the checkpoint NODE
--------------------------------------------------------------------------------

Two durable things, from two places. The paused GRAPH is durable because the checkpointer
is (`SqliteSaver`, wired in app.py). The typed audit ledger it needs is durable because
this store is: verdicts, evidence, explanations and receipts are written the moment the run
returns, parked or not. `approve()` puts them back together — `pipeline.rehydrate` rebuilds
the ledger from the stored record (replay.py) and `pipeline.resume` runs the graph on from
where it stopped.

**It resumes the graph rather than editing the stored record, and that distinction is the
whole feature.** Applying the decision straight to the record would be half the code and
would produce a second path to the one thing in this system that must not have one — a path
on which `human_checkpoint` never runs, invisible from the outside, taken only after a
restart. What remains a 409 is the honest case: a run whose CHECKPOINT the checkpointer has
no record of was never parked, or was already settled, and no amount of stored evidence can
manufacture a pause that did not survive.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from attest import record as record_module
from attest import writeback
from attest.config import settings
from attest.datahub import DataHubClient, DataHubError
from attest.graph import Pipeline
from attest.record import AuditRecord
from attest.report import Decision, RunStatus
from attest.retrieval import ClaimPage, ClaimQuery, ClaimReader, RetrievedClaim
from attest.store import AuditStore
from attest.writeback import WriteResult

log = logging.getLogger(__name__)


def _write_columns(result: WriteResult | None) -> dict[str, object]:
    """One write-back result, as the decision log's columns.

    The log keeps the outcome TWICE and on purpose: `writeback` is the sentence a human
    reads, and `writeback_ok`/`writeback_step` are the same fact as structure. Both are
    produced HERE, from one `WriteResult`, so the rendering and the structure cannot come
    to disagree about what happened — which is the failure a second call site would invite.

    The structure is what the retrieval path reads, and it has to: telling "the index has
    not caught up with a write that landed" from "the write never landed" is only possible
    against Attest's own record of what it did, and a `str()` cannot be parsed back into
    that record (store.py, and Session 5's lesson).

    `None` — no write was attempted — is NOT `ok=False`. A claim nobody published is not a
    claim whose write failed.
    """
    if result is None:
        return {"writeback": "skipped", "writeback_ok": None, "writeback_step": None}
    return {
        "writeback": str(result),
        "writeback_ok": result.ok,
        "writeback_step": result.failed_step,
    }


class ServiceError(RuntimeError):
    """Something the caller asked for cannot be done. Carries an HTTP-ish reason."""


class RunNotFound(ServiceError):
    pass


class ClaimNotFound(ServiceError):
    """The catalog holds no claim artifact at that URN.

    Distinct from a claim with no VERDICT, which is a claim that exists and reads as
    `incomplete` or `pending-lag` (retrieval.ReadState). Nothing here, versus something here
    whose verdict has not landed — folding them together would report a claim Attest never
    wrote and a claim Attest half-wrote as the same fact.
    """


class NotResumable(ServiceError):
    """The run is not parked at a checkpoint — nothing here is waiting on a human."""


class TargetNotCovered(ServiceError):
    """A URN the caller REQUIRED to be audited produced no claim.

    `target_urns` is a precondition on the EXTRACTION, and it is the only thing a caller can
    say about what an audit must cover. The request-time validator settles half of it — a
    declared URN has to appear in the agent's text, or no claim could ever be about it. The
    other half cannot be known until the decomposer has run: whether a claim was actually
    produced for it.

    A claim that came back but could not be CHECKED (an unresolvable URN — report.ClaimError)
    satisfies this. The precondition is about coverage, not about verdicts, and an error is a
    loud, readable outcome sitting in the record. A claim that was never produced at all — or
    one Attest dropped — is the silent case, and the silent case is the one worth a refusal:
    without it, the sentence the decomposer quietly skipped is a gap nobody sees.
    """


class TrajectoryViolation(ServiceError):
    """The run violated its own architecture and is therefore un-approvable.

    A trajectory invariant failed (trajectory.py) — a checker may have called a model, an
    explanation may have skipped the guard, a correction may have been proposed without
    re-verification. The report is not trustworthy evidence, so nothing it proposes may reach
    the catalog. This is the hard gate the flag exists to enforce: a detected violation must
    be UN-shippable, not merely logged.
    """


class AuditService:
    def __init__(
        self,
        pipeline: Pipeline,
        store: AuditStore,
        client: DataHubClient | None = None,
        write_back: bool = True,
    ) -> None:
        self.pipeline = pipeline
        self.store = store
        # The client used for WRITE-BACK, which is not necessarily the one the pipeline
        # reads through — the pipeline's may be wrapped or faked. Defaults to it.
        self.client = client or pipeline.client
        self.write_back = write_back

    # --- audit ----------------------------------------------------------------

    def audit(
        self,
        agent_output: str,
        source_agent: str = "",
        target_urns: Sequence[str] = (),
    ) -> AuditRecord:
        """Run one audit, persist it, and return what was persisted.

        No lock. Concurrent audits are safe because nothing mutable is shared between them
        — each run has its own ledger, its own snapshot cache, and its own LLM handle with
        its own token ledger. See the module docstring, and note that the property that
        matters is not "it does not crash" but "each receipt bills only its own tokens".

        The record is written BEFORE it is returned. A run whose verdicts reached a caller
        but never reached the store would be an audit with no audit trail.

        `target_urns` is the caller's precondition on what this audit must cover, and it is
        checked AFTER the run because it can only be known then — see `TargetNotCovered`. It
        is never a filter: it cannot narrow what gets audited, only refuse an audit that
        covered less than the caller required.
        """
        run_id = str(uuid.uuid4())
        started = datetime.now(tz=UTC)

        report = self.pipeline.run(agent_output, thread_id=run_id)

        record = record_module.from_report(
            report, run_id=run_id, source_agent=source_agent, created_at=started
        )
        self.store.save(record)

        if report.status is not RunStatus.AWAITING_REVIEW:
            # Nothing is waiting on a human, so nothing needs the typed ledger any more —
            # and this now covers FLAGGED runs too, which are terminal (un-approvable) and so
            # keep no pause worth holding. Left alone, ledgers accumulate for the life of the
            # process — a service is a long-lived process, which a one-shot CLI was not.
            self.pipeline.forget(run_id)

        if not report.trajectory.ok:
            log.error(
                "run %s violated its own architecture and is FLAGGED (un-approvable): %s",
                run_id,
                report.trajectory.summary,
            )

        # LAST, and after the store.save above, deliberately. The audit happened: it read
        # the catalog, it spent tokens, it reached verdicts. Failing the caller's
        # precondition is a fact ABOUT that run, not a reason to pretend it never ran, and
        # the record it refuses to return over the wire is readable at GET /audit/{run_id}
        # — the error says so, by id.
        self._require_coverage(record, target_urns)
        return record

    @staticmethod
    def _require_coverage(record: AuditRecord, target_urns: Sequence[str]) -> None:
        """Hold the finished audit to what the caller required it to cover."""
        if not target_urns:
            return
        covered = {c.target_urn for c in record.claims}
        covered |= {e.target_urn for e in record.errors}
        missing = [u for u in target_urns if u not in covered]
        if not missing:
            return
        raise TargetNotCovered(
            f"this audit covers no claim about {missing}, which target_urns required it to. "
            f"The agent's text names them, but the decomposer extracted no claim about them "
            f"— the run's `dropped` list says whether one was produced and refused. The "
            f"audit itself ran and is stored: GET /audit/{record.run_id}."
        )

    def get(self, run_id: str) -> AuditRecord:
        stored = self.store.load(run_id)
        if stored is None:
            raise RunNotFound(f"no such audit run: {run_id}")
        return stored

    # --- the human checkpoint -------------------------------------------------

    def approve(
        self, run_id: str, decisions: list[Decision]
    ) -> tuple[AuditRecord, tuple[WriteResult, ...]]:
        """Settle a parked run's proposals, and write the accepted ones to the catalog.

        A proposal nobody named stays PENDING. Nothing here has an "approve all", and a run
        that reaches the end of this method with every proposal unreviewed has done exactly
        what it should — and, since Session 14, is still parked and can still be decided.
        A run settles to COMPLETE only once every proposal has been ruled on; until then
        this returns an AWAITING_REVIEW record and may simply be called again. That is why
        an empty `decisions` list is legal rather than pointless: it is a person looking and
        settling nothing, and it costs the run nothing.

        A run parked by a process that has since died is resumed, not shortcut: its ledger
        is rebuilt from the store and the graph runs on from the checkpoint it stopped at,
        through the checkpoint NODE. See the module docstring for why that is not a detail.
        """
        stored = self.store.load(run_id)
        if stored is None:
            raise RunNotFound(f"no such audit run: {run_id}")
        if stored.status is RunStatus.FLAGGED or not stored.receipts.trajectory_ok:
            # THE HARD GATE. A run that violated its own architecture cannot be approved and
            # nothing it proposes may reach the catalog — the write-back path is closed to it
            # here, before any rehydrate or resume. A trajectory violation used to be logged
            # and the report stored, served, and approvable anyway; that made the strongest
            # claim in the system (NO_LLM_IN_THE_VERDICT_PATH, and the correction invariants)
            # enforceable only by whoever happened to read the log. Now it blocks the sign-off.
            raise TrajectoryViolation(
                f"run {run_id} is flagged: it violated its own architecture and cannot be "
                f"approved. {stored.receipts.trajectory_summary}. A report whose trajectory "
                f"failed is not trustworthy evidence, and a correction it proposes must not "
                f"reach the catalog."
            )
        if stored.status is not RunStatus.AWAITING_REVIEW:
            raise NotResumable(
                f"run {run_id} is {stored.status.value}: it has no proposals awaiting a "
                f"decision. A correction can only be settled once, and only while the run "
                f"is parked at the checkpoint."
            )
        if not self.pipeline.rehydrate(run_id, stored):
            # The store says this run parked; the checkpointer has no paused graph for it.
            # The two disagree, and the honest answer is to say so — a decision applied
            # without the checkpoint node running is the one thing this system must not do,
            # and the stored evidence is exactly what would make faking it feel reasonable.
            raise NotResumable(
                f"run {run_id} is recorded as awaiting review, but the checkpointer has no "
                f"paused graph for it: its checkpoints were deleted, or it was parked by a "
                f"process using an in-memory saver. Its verdicts and evidence are stored "
                f"and readable; the pause is not recoverable, and a correction is never "
                f"settled outside the checkpoint node."
            )

        report = self.pipeline.resume(run_id, decisions)

        if not report.trajectory.ok:
            # The stored report was clean but the resumed one is not. The graph is
            # deterministic, so this is not expected — but the write-back path runs AFTER
            # resume, so this is the last point before the catalog is touched, and the gate
            # holds here too. Nothing has been written; the run stays parked and flagged.
            raise TrajectoryViolation(
                f"run {run_id} violated its own architecture on resume and cannot be "
                f"approved: {report.trajectory.summary}."
            )

        settled = record_module.from_report(
            report,
            run_id=run_id,
            source_agent=stored.source_agent,
            created_at=stored.created_at,
        )

        # Write back ONLY the verdicts a human PUBLISHED, and only after they published them.
        #
        # The gate is `publication`, not the correction (Session 15, Option A). It used to be
        # `correction.awaits_human`, which meant the only verdict that ever reached the
        # catalog was a contradiction the agent had successfully self-corrected — so a
        # STOOD_FIRM contradiction, the most damning finding Attest can produce, was silently
        # swallowed, and Supported and Insufficient-Coverage verdicts never reached DataHub
        # at all. See report.PublicationStatus.
        #
        # `awaiting` is what was still on offer when THIS call arrived, read off the record
        # as it stood BEFORE the resume. A run parks again while anything is undecided, so a
        # caller can name a claim they already settled on an earlier call — and the
        # checkpoint node rightly ignores it (a claim is settled once). The write-back
        # ignores it for the same reason: a decision writes back what IT settled, and
        # re-naming a claim decided an hour ago decides nothing now — it would only replay
        # that claim's catalog write on every later call.
        awaiting = {c.index for c in stored.claims if c.publication.awaits_human}
        told_to_publish = {
            d.claim_index for d in decisions if d.publish
        } & awaiting

        results: list[WriteResult] = []
        # Keyed by CLAIM, not by URN: two published claims can name one dataset, and keying
        # the outcome by URN would let the second write's result be reported as the first
        # decision's. A decision log that attributes a write to the wrong decision is a
        # false entry in the one record that exists to say who decided what.
        written: dict[int, WriteResult] = {}
        for claim in settled.claims:
            if claim.index not in told_to_publish:
                continue
            if not claim.publication.published:
                # The checkpoint did not publish it after all. Not an error, and not a write.
                continue
            result = self._write_back(claim, settled, claim.publication.reviewer)
            results.append(result)
            written[claim.index] = result

        # Every decision is logged, accepted or rejected, with what the catalog did about
        # it. The store is append-only here on purpose (store.py).
        for decision in decisions:
            result = written.get(decision.claim_index)
            self.store.record_decision(
                run_id,
                decision,
                **_write_columns(result),
            )

        self.store.save(settled)
        # Only a run with nothing left pending is over. One with proposals still undecided
        # parked again at the checkpoint (graph.py), and forgetting it would delete the
        # pause those proposals are waiting in — which is precisely the bug this session
        # closed, moved down a layer.
        if report.status is RunStatus.COMPLETE:
            self.pipeline.forget(run_id)

        return settled, tuple(results)

    def retry_writeback(self, run_id: str) -> tuple[WriteResult, ...]:
        """Re-run the catalog write for claims a human ALREADY accepted. The recovery path.

        The write-back is three operations — upsert the claim, report the verdict, swap the
        tag — and cannot be atomic, so a partial write is reachable: a claim with no verdict,
        or a verdict whose tag never landed. This repairs one.

        **It is not the approval path, and it must not be.** Re-running `approve` cannot do
        this: a settled claim is no longer `awaits_human`, so the intersection below excludes
        it, and a run whose last proposal was decided is COMPLETE and not resumable at all.
        Before this existed a failed write-back simply STRANDED — honestly recorded, and with
        no way to try again. That was tolerable when the write was one atomic mutation. With
        three, and an index lag that makes the middle one genuinely retryable, it is not.

        **It cannot approve anything, and that is the point.** It reads the stored record and
        re-writes only what is already `ACCEPTED` in it. A claim nobody accepted is not
        reachable from here, a rejected one is not either, and no decision is taken — the
        human act already happened and is in the append-only log. This re-executes a recorded
        decision's side effect, which is why it needs no graph, no checkpoint, and no
        interrupt. There is still exactly one path to approving something.

        Safe to call repeatedly: every write is idempotent (writeback.py). The claim's URN is
        derived from its content, so a re-run lands on the same artifact rather than minting a
        second, and the run event is keyed by the run's own timestamp, so re-reporting
        collapses onto the same row rather than appending a duplicate. It therefore re-runs
        ALL accepted claims rather than tracking which step failed — correctness does not
        depend on the bookkeeping, and `WriteResult.failed_step` exists to make the failure
        legible, not to drive the repair.
        """
        stored = self.store.load(run_id)
        if stored is None:
            raise RunNotFound(f"no such audit run: {run_id}")
        if stored.status is RunStatus.FLAGGED or not stored.receipts.trajectory_ok:
            # The same hard gate as `approve`, for the same reason: a run that violated its
            # own architecture must not reach the catalog by ANY path. A recovery endpoint is
            # exactly where that would quietly be re-opened.
            raise TrajectoryViolation(
                f"run {run_id} is flagged: it violated its own architecture and nothing it "
                f"produced may reach the catalog. {stored.receipts.trajectory_summary}."
            )

        published = [c for c in stored.claims if c.publication.published]
        results: list[WriteResult] = []
        for claim in published:
            result = self._write_back(claim, stored, claim.publication.reviewer)
            results.append(result)
            # The retry is logged too. It writes to the catalog, so it belongs in the
            # append-only record of what was written and when — and a repair that left no
            # trace would be a catalog change with no history, which is the shape this whole
            # store exists to prevent. The decision it names is the one already on file; the
            # reviewer is not re-attributed, because nobody decided anything here.
            self.store.record_decision(
                run_id,
                Decision(
                    claim_index=claim.index,
                    publish=True,
                    reviewer=claim.publication.reviewer,
                    note="write-back retry",
                ),
                **_write_columns(result),
            )
        return tuple(results)

    def _write_back(self, claim, run: AuditRecord, reviewer: str) -> WriteResult:
        """One claim, to the catalog, as a per-claim artifact.

        `checked_at=run.created_at` is load-bearing rather than incidental: it keys the run
        event, so it is what makes a retry collapse onto the same row instead of appending a
        duplicate verdict. `write_claim_artifact` has no default for it precisely so this
        cannot be forgotten here.

        `reviewer` is PASSED IN rather than looked up, and since Session 15 both callers read
        it off `claim.publication.reviewer` — the checkpoint records who published a verdict
        at the moment it settles it. That is what fixed the earlier bug: the approval path
        writes its decisions to the store AFTER the catalog write, so a store lookup here
        found nothing and attributed every artifact to "unknown".
        """
        if not self.write_back:
            return WriteResult(
                ok=False, target_urn=claim.target_urn, detail="write-back is disabled"
            )
        result = writeback.write_claim_artifact(
            self.client,
            claim=claim.claim,
            verdict=claim.verdict,
            run_id=run.run_id,
            checked_at=run.created_at,
            evidence="; ".join(
                f"{e.field}={e.value}" for e in claim.evidence if e.value is not None
            ),
            reviewer=reviewer,
            decision="accepted",
            source_agent=run.source_agent,
            external_url=f"/audit/{run.run_id}",
        )
        # The dataset-level badge, best-effort and deliberately secondary: it is a glance
        # view, and the claim artifact above is the record. A badge failure does not make the
        # claim's verdict any less written, so it does not turn the result red.
        try:
            writeback.write_verdict(
                self.client,
                target_urn=claim.target_urn,
                verdict=claim.verdict,
                claim_type=claim.claim_type,
                run_id=run.run_id,
                source_agent=run.source_agent,
                checked_at=run.created_at,
            )
        except DataHubError as exc:  # pragma: no cover - write_verdict already returns
            log.warning("dataset badge for %s not updated: %s", claim.target_urn, exc)
        return result

    # --- retrieval ------------------------------------------------------------

    def claims(self, query: ClaimQuery, limit: int = 50) -> ClaimPage:
        """Claim artifacts, FROM THE CATALOG. The inheritance half of the thesis.

        **This reads DataHub, not the store, and that is the entire point.** If it answered
        from SQLite it would prove nothing about what a second agent inherits — it would
        just be Attest showing you its own notes. The store is consulted for exactly one
        thing: what Attest DID about a claim whose verdict the catalog is not showing, which
        is the only way to tell an index still catching up from a write that never landed.
        It can never add a claim, remove one, or change a verdict.

        Needs no run, no approval and no pause: this is a read of published facts.
        """
        return ClaimReader(self.client, self.store).list(query, limit=limit)

    def claim(self, claim_urn: str) -> RetrievedClaim:
        """One claim artifact and its whole verdict history."""
        found = ClaimReader(self.client, self.store).get(claim_urn)
        if found is None:
            raise ClaimNotFound(
                f"the catalog holds no claim artifact at {claim_urn}. Attest may never have "
                f"written one (a verdict reaches DataHub only when a human publishes it), "
                f"or the URN may not be a claim artifact's."
            )
        return found

    # --- liveness -------------------------------------------------------------

    def health(self) -> dict[str, str]:
        """Attest's health, and the catalog's, reported separately.

        DataHub being unreachable does not make this service unhealthy — it can still serve
        every stored audit, and a new run would report the resolve failure honestly rather
        than scoring a claim against nothing. Reporting one bad status for both would take
        the service out of rotation for a dependency's outage AND hide the outage.
        """
        try:
            self.client.execute("query { appConfig { appVersion } }")
            catalog = "up"
        except DataHubError as exc:
            catalog = f"unreachable: {exc}"

        return {
            "status": "ok",
            "model": settings.model_default,
            "datahub": catalog,
            # Handed to the frontend so its dataset deep-links point at the right DataHub UI
            # without the built SPA hardcoding a host. See settings.datahub_ui_url.
            "datahub_ui_url": settings.datahub_ui_url,
        }
