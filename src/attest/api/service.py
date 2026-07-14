"""The service: run an audit, persist it, and settle its proposals.

Everything the HTTP layer does that is not HTTP. app.py is routing and status codes; this
is where a run is executed, written down, and — only when a human says so — pushed back to
the catalog.

--------------------------------------------------------------------------------
Why audits are serialized, and why that is not a performance failure
--------------------------------------------------------------------------------

One `Pipeline` is built and reused, which means one `LLM` handle, which means ONE shared
`llm.usage` list. observe.Trace bills a step by slicing that list from a mark (observe.py),
so two audits running concurrently through the same handle would bill each other's tokens:
run A's decompose step would be charged with whatever run B spent while A was in flight.

The receipts ARE the product. A cost-per-claim figure that silently mixes two runs together
is precisely the kind of unfounded number Attest exists to catch, and it would be printed
by Attest itself. So `audit()` takes a lock and runs one at a time.

This costs nothing that matters. An audit is seconds, the projected workload is a thousand
claims a day (cost.py), and a queue in front of a service that is idle 99% of the time is
not a bottleneck. Real concurrency means a Pipeline (and an LLM handle) per run, which is a
change to how the graph is constructed, not a lock to remove — a later session, done
deliberately, with the token accounting proven rather than assumed.

--------------------------------------------------------------------------------
A parked run is resumable for the life of the process. Say so, do not hide it.
--------------------------------------------------------------------------------

The graph's human checkpoint is a real interrupt (`interrupt_before`), and the typed audit
ledger that a resume needs lives in memory beside the graph — for the reasons in
graph.py, which have not changed. So a run parked at the checkpoint can be approved for as
long as this process lives, and not after a restart.

The AUDIT is durable regardless: it is in the store, with its verdicts, evidence, and
receipts, the moment the run returns. What does not survive a restart is the ability to
*resume the paused graph*, and `approve()` says so with a 409 rather than inventing a
shortcut. Applying a decision straight to the stored record would be easy and would quietly
bypass the checkpoint node — a second, unaudited path to the one thing in this system that
must never have one. Durable resume is a real feature; it is not this.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime

from attest import record as record_module
from attest import writeback
from attest.config import settings
from attest.datahub import DataHubClient, DataHubError
from attest.graph import Pipeline
from attest.record import AuditRecord
from attest.report import Decision, ReviewStatus, RunStatus
from attest.store import AuditStore
from attest.writeback import WriteResult

log = logging.getLogger(__name__)


class ServiceError(RuntimeError):
    """Something the caller asked for cannot be done. Carries an HTTP-ish reason."""


class RunNotFound(ServiceError):
    pass


class NotResumable(ServiceError):
    """The run is not parked at a checkpoint — nothing here is waiting on a human."""


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
        self._lock = threading.Lock()

    # --- audit ----------------------------------------------------------------

    def audit(self, agent_output: str, source_agent: str = "") -> AuditRecord:
        """Run one audit, persist it, and return what was persisted.

        The record is written BEFORE it is returned. A run whose verdicts reached a caller
        but never reached the store would be an audit with no audit trail.
        """
        run_id = str(uuid.uuid4())
        started = datetime.now(tz=UTC)

        with self._lock:  # see the module docstring: shared token accounting
            report = self.pipeline.run(agent_output, thread_id=run_id)

        record = record_module.from_report(
            report, run_id=run_id, source_agent=source_agent, created_at=started
        )
        self.store.save(record)

        if report.status is RunStatus.COMPLETE:
            # Nothing is waiting on a human, so nothing needs the typed ledger any more.
            # Left alone, these accumulate for the life of the process — a service is a
            # long-lived process, which a one-shot CLI was not.
            self.pipeline.forget(run_id)

        if not report.trajectory.ok:
            log.error(
                "run %s violated its own architecture: %s",
                run_id,
                report.trajectory.summary,
            )
        return record

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
        what it should.
        """
        stored = self.store.load(run_id)
        if stored is None:
            raise RunNotFound(f"no such audit run: {run_id}")
        if stored.status is not RunStatus.AWAITING_REVIEW:
            raise NotResumable(
                f"run {run_id} is {stored.status.value}: it has no proposals awaiting a "
                f"decision. A correction can only be settled once, and only while the run "
                f"is parked at the checkpoint."
            )
        if not self.pipeline.is_resumable(run_id):
            raise NotResumable(
                f"run {run_id} was parked by a previous process and cannot be resumed. Its "
                f"verdicts and evidence are stored and readable; only the paused graph is "
                f"gone. Durable resume is not built — see api/service.py."
            )

        with self._lock:
            report = self.pipeline.resume(run_id, decisions)

        settled = record_module.from_report(
            report,
            run_id=run_id,
            source_agent=stored.source_agent,
            created_at=stored.created_at,
        )

        # Write back ONLY what a human accepted, and only after they accepted it.
        results: list[WriteResult] = []
        accepted = {d.claim_index for d in decisions if d.accept}
        for claim in settled.claims:
            if claim.index not in accepted:
                continue
            if claim.correction.review is not ReviewStatus.ACCEPTED:
                # The human named a claim that had nothing to accept. Not an error, and not
                # a write: there is no proposal here to approve.
                continue
            results.append(self._write_back(claim, settled))

        # Every decision is logged, accepted or rejected, with what the catalog did about
        # it. The store is append-only here on purpose (store.py).
        written = {r.target_urn: r for r in results}
        for decision in decisions:
            claim = next(
                (c for c in settled.claims if c.index == decision.claim_index), None
            )
            result = written.get(claim.target_urn) if claim else None
            self.store.record_decision(
                run_id,
                decision,
                writeback=str(result) if result is not None else "skipped",
            )

        self.store.save(settled)
        if report.status is RunStatus.COMPLETE:
            self.pipeline.forget(run_id)

        return settled, tuple(results)

    def _write_back(self, claim, run: AuditRecord) -> WriteResult:
        if not self.write_back:
            return WriteResult(
                ok=False, target_urn=claim.target_urn, detail="write-back is disabled"
            )
        return writeback.write_verdict(
            self.client,
            target_urn=claim.target_urn,
            verdict=claim.verdict,
            claim_type=claim.claim_type,
            run_id=run.run_id,
            source_agent=run.source_agent,
            checked_at=run.created_at,
        )

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
        }
