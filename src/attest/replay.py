"""A stored audit, read backwards: the typed pieces a parked run needs in order to finish.

record.py projects a finished run onto something flat enough to store. This does the
reverse — it takes an `AuditRecord` back out of the store and rebuilds the typed audit
record a resumed run is assembled from: pydantic claims, frozen Explanations with their
faithfulness results, Corrections with their attempts, and the step trace.

--------------------------------------------------------------------------------
Why this exists, and why "it resumes" was not the bar
--------------------------------------------------------------------------------

LangGraph's SQLite checkpointer makes the PAUSED GRAPH durable: after a restart, the
cursor, the retry counts and the human's decisions are all still there, and the run can be
resumed at the human checkpoint. What it does not make durable is the typed ledger, which
lives beside the graph and cannot go into checkpointed state (see graph.py — the msgpack
landmine has not gone away, and durable resume is precisely the path on which it would
bite). So the ledger is rebuilt, from the one place the run was written down whole: the
store.

The bar for that rebuild is not "the run finishes". It is **the resumed report is the
report the unrestarted run would have produced** — every verdict, every piece of evidence,
every rejected draft, every step, every token, the same. Anything less is an auditor whose
own output depends on whether its process happened to survive, and it would be invisible:
a restarted run that silently reports a slightly different thing looks exactly like one
that does not.

The gap that made the point, and the reason record.py grew four fields: `StepRecord.models`
was not stored. Trace.cost reports a run's dollar total as `None` — never `0` — when any
model that spent tokens has no price, and it finds those models by name from the step's
`models`. Rebuild a trace without them and the unpriced set comes back empty, so the
resumed run computes `usd = sum(...)` where the original had honestly said "unknown". A
restarted audit fabricating a cost figure the original refused to state is the None-is-not-
zero rule breaking inside Attest's own receipts — which is the exact failure this project
exists to catch, committed by the thing that catches it. `tests/test_resume.py` pins it.

--------------------------------------------------------------------------------
What is NOT rebuilt, said out loud
--------------------------------------------------------------------------------

`StepRecord.inputs` / `.outputs` are step summaries for a human reading the log ("cached:
true", "claims: 3"). They are not part of the persisted projection, nothing in the report,
the receipts or trajectory.py reads them, and a resumed run's replayed steps carry them
empty. That is a real difference from an unrestarted run's in-memory trace and it is worth
knowing about; it is not a difference in anything the run REPORTS.

The run's `SnapshotCache` is not rebuilt either, and must not be. A resumed run resolves
nothing — it applies decisions to verdicts that were already reached — so it needs no
snapshots, and a cache rehydrated across a process boundary would be exactly the stale
ground truth datahub/cache.py exists to make unreachable. What IS carried is the original
run's `CacheStats`: the run did read the catalog, in a process that has since died, and a
resumed report claiming 0 fetches would be a different lie than the one it avoided.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import TypeAdapter

from attest.claims import Claim, Evidence, Verdict
from attest.crosscheck import Conflict
from attest.datahub import CacheStats
from attest.decompose import Dropped
from attest.explain import Explanation
from attest.faithfulness import Faithfulness, Violation
from attest.observe import StepKind, StepRecord, Trace
from attest.record import AuditRecord, ClaimRecord
from attest.report import (
    Attempt,
    ClaimAudit,
    ClaimError,
    Correction,
)
from attest.sanitize import Finding

_CLAIM_ADAPTER: TypeAdapter[Claim] = TypeAdapter(Claim)


@dataclass(frozen=True)
class Replay:
    """One stored run, back in the types the pipeline reasons in."""

    source_text: str
    trace: Trace
    claims: tuple[Claim, ...]
    audits: list[ClaimAudit]
    errors: list[ClaimError]
    dropped: tuple[Dropped, ...]
    findings: tuple[Finding, ...]
    # The ORIGINAL run's catalog receipt, carried rather than recomputed. See the docstring.
    catalog: CacheStats


def from_record(record: AuditRecord) -> Replay:
    """Rebuild a run's typed audit record from what was stored. Loses nothing it reports."""
    audits = [_audit(c) for c in record.claims]
    errors = [
        ClaimError(
            index=e.index,
            claim=_CLAIM_ADAPTER.validate_python(e.claim),
            error=e.error,
        )
        for e in record.errors
    ]

    # The decomposed claims, in the order the cursor walked them. Every claim either
    # produced a verdict or produced an error, so between the two lists they are all here
    # — and the index is the cursor's own, not a position in either list.
    by_index: dict[int, Claim] = {a.index: a.claim for a in audits}
    by_index.update({e.index: e.claim for e in errors})

    return Replay(
        source_text=record.source_text,
        trace=_trace(record),
        claims=tuple(by_index[i] for i in sorted(by_index)),
        audits=audits,
        errors=errors,
        dropped=tuple(
            Dropped(reason=d.reason, payload=dict(d.payload)) for d in record.dropped
        ),
        findings=tuple(
            Finding(pattern=f.pattern, matched=f.matched)
            for f in record.injection_findings
        ),
        catalog=CacheStats(
            lookups=record.receipts.catalog_lookups,
            fetches=record.receipts.catalog_fetches,
            entities=record.receipts.catalog_entities,
            hits=record.receipts.catalog_lookups - record.receipts.catalog_fetches,
        ),
    )


def _trace(record: AuditRecord) -> Trace:
    """The step trace, replayed. `models` and `error` are why the receipts survive."""
    return Trace(
        steps=[
            StepRecord(
                name=s.name,
                kind=StepKind(s.kind),
                claim_index=s.claim_index,
                latency_ms=s.latency_ms,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                cost_usd=s.cost_usd,
                # Without these two the resumed run reports a cost the original did not
                # state, and a clean run that was not clean. See the module docstring.
                models=tuple(s.models),
                error=s.error,
            )
            for s in record.steps
        ]
    )


def _audit(claim: ClaimRecord) -> ClaimAudit:
    """One claim, back in the shape the report is assembled from."""
    return ClaimAudit(
        index=claim.index,
        claim=_CLAIM_ADAPTER.validate_python(claim.claim),
        verdict=Verdict(claim.verdict),
        reason=claim.reason,
        evidence=tuple(
            Evidence(field=e.field, value=e.value, note=e.note) for e in claim.evidence
        ),
        explanation=Explanation(
            text=claim.explanation,
            source="template" if claim.explanation_source == "template" else "model",
            faithfulness=Faithfulness(
                ok=claim.faithful,
                violations=tuple(
                    Violation(token=v.token, kind=v.kind)
                    for v in claim.faithfulness_violations
                ),
            ),
            conflicts=tuple(
                Conflict(kind=c.kind, detail=c.detail) for c in claim.conflicts
            ),
            rejected=tuple(claim.rejected),
        ),
        correction=Correction(
            outcome=claim.correction.outcome,
            attempts=tuple(
                Attempt(
                    n=a.n,
                    revised_claim=(
                        _CLAIM_ADAPTER.validate_python(a.revised_claim)
                        if a.revised_claim is not None
                        else None
                    ),
                    verdict=Verdict(a.verdict) if a.verdict else None,
                    reason=a.reason,
                )
                for a in claim.correction.attempts
            ),
            proposal=(
                _CLAIM_ADAPTER.validate_python(claim.correction.proposal)
                if claim.correction.proposal is not None
                else None
            ),
            review=claim.correction.review,
        ),
    )
