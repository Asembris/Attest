"""The persisted projection of an audit run.

An `AuditReport` (report.py) is the in-process object: it holds live Trace records,
frozen Explanations, a TrajectoryReport, pydantic claims. It is the right thing to reason
with and the wrong thing to store — half of it is machinery, and reconstituting that
machinery out of a database in order to render JSON would be dressing a corpse.

So there is a second type. An `AuditRecord` is what a run LOOKS like once it is over: flat,
serializable, and complete enough that nothing in the report a reader needs is missing from
it. The store writes it, the API returns it, and a round trip through SQLite gives back an
equal object — which is the property tests/test_store.py pins.

**Nothing is dropped on the way through.** The temptation with a projection is to keep the
verdicts and discard the machinery, and that would quietly destroy the thing that makes
Attest worth anything: the evidence trail. So the record carries the evidence for every
verdict, the trajectory result, the per-step trace with its kinds and token counts, the
dropped claims, and the injection findings. A stored audit you cannot interrogate is a
score, and a score is what an unaccountable system produces.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from attest.report import AuditReport, CorrectionOutcome, ReviewStatus, RunStatus


class EvidenceView(BaseModel):
    """One catalog field that produced a verdict. `value=None` means: the catalog is silent."""

    model_config = ConfigDict(frozen=True)

    field: str
    value: Any = None
    note: str | None = None


class AttemptView(BaseModel):
    """One turn of the correction loop, and what the deterministic re-check said back."""

    model_config = ConfigDict(frozen=True)

    n: int
    verdict: str | None = None
    reason: str = ""
    revised_claim: dict[str, Any] | None = None


class CorrectionView(BaseModel):
    """The correction loop's record for one claim.

    `outcome` is one of six names, not a boolean, and `review` defaults to PENDING —
    both for the reasons in report.py. A projection that flattened them into
    `corrected: true/false` would erase the loop's own failure modes on the way to disk.
    """

    model_config = ConfigDict(frozen=True)

    outcome: CorrectionOutcome
    review: ReviewStatus = ReviewStatus.PENDING
    proposal: dict[str, Any] | None = None
    attempts: tuple[AttemptView, ...] = ()

    @property
    def awaits_human(self) -> bool:
        return (
            self.outcome is CorrectionOutcome.CORRECTED
            and self.review is ReviewStatus.PENDING
        )


class ClaimRecord(BaseModel):
    """One audited claim: what was said, what the catalog said, and why."""

    model_config = ConfigDict(frozen=True)

    index: int
    claim_type: str
    target_urn: str
    raw_text: str
    claim: dict[str, Any]

    verdict: str
    reason: str
    evidence: tuple[EvidenceView, ...] = ()

    explanation: str = ""
    # "model" or "template". A reader is entitled to know whether the prose in front of
    # them was written or fallen back to, and hiding it would flatter the semantic layer.
    explanation_source: str = "template"
    faithful: bool = True
    conflicts: tuple[str, ...] = ()

    correction: CorrectionView


class ClaimErrorRecord(BaseModel):
    """A claim that could not be checked at all. NOT a verdict, and never tallied as one."""

    model_config = ConfigDict(frozen=True)

    index: int
    target_urn: str
    claim: dict[str, Any]
    error: str


class StepView(BaseModel):
    """One node's execution. The `kind` is what trajectory verification is made of."""

    model_config = ConfigDict(frozen=True)

    seq: int
    name: str
    kind: str
    claim_index: int | None = None
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None


class Receipts(BaseModel):
    """What the run cost and whether it kept to its own architecture. Measured, not estimated."""

    model_config = ConfigDict(frozen=True)

    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    # None, never 0, when a model in the run had no price. See cost.py.
    usd: float | None = None
    steps: int = 0

    trajectory_ok: bool = True
    trajectory_summary: str = ""
    rules_checked: tuple[str, ...] = ()

    # The catalog receipt. `lookups` is what the claims asked for; `fetches` is what the
    # catalog was actually asked. The gap is the cache. See datahub/cache.py.
    catalog_lookups: int = 0
    catalog_fetches: int = 0
    catalog_entities: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class AuditRecord(BaseModel):
    """One complete audit run, as it is stored and as it is served."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    created_at: datetime
    status: RunStatus
    # Who made the claims. Free text, supplied by the caller, and carried so a verdict can
    # be attributed later — "which agent keeps getting ownership wrong" is the question a
    # month of these answers.
    source_agent: str = ""
    source_text: str = ""

    claims: tuple[ClaimRecord, ...] = ()
    errors: tuple[ClaimErrorRecord, ...] = ()
    receipts: Receipts = Field(default_factory=Receipts)
    steps: tuple[StepView, ...] = ()

    # Claims the decomposer produced and Attest refused to carry (a minted URN, a claim
    # that failed validation), and instruction-like spans stripped from the agent's text.
    # Both are gaps in the audit, and both are surfaced rather than swallowed.
    dropped: tuple[str, ...] = ()
    injection_findings: tuple[str, ...] = ()

    @property
    def proposals(self) -> tuple[ClaimRecord, ...]:
        """Corrections that re-verified clean and are waiting on a human."""
        return tuple(c for c in self.claims if c.correction.awaits_human)

    def verdict_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in self.claims:
            counts[claim.verdict] = counts.get(claim.verdict, 0) + 1
        return counts


def from_report(
    report: AuditReport,
    run_id: str,
    source_agent: str = "",
    created_at: datetime | None = None,
) -> AuditRecord:
    """Project a finished run onto the thing that gets stored and served."""
    return AuditRecord(
        run_id=run_id,
        created_at=created_at or datetime.now(tz=UTC),
        status=report.status,
        source_agent=source_agent,
        source_text=report.source_text,
        claims=tuple(
            ClaimRecord(
                index=a.index,
                claim_type=a.claim.claim_type.value,
                target_urn=a.claim.target_urn,
                raw_text=a.claim.raw_text,
                claim=a.claim.model_dump(mode="json"),
                verdict=a.verdict.value,
                reason=a.reason,
                evidence=tuple(
                    EvidenceView(field=e.field, value=e.value, note=e.note)
                    for e in a.evidence
                ),
                explanation=a.explanation.text,
                explanation_source=a.explanation.source,
                faithful=a.explanation.faithfulness.ok,
                conflicts=tuple(str(c) for c in a.conflicts),
                correction=CorrectionView(
                    outcome=a.correction.outcome,
                    review=a.correction.review,
                    proposal=(
                        a.correction.proposal.model_dump(mode="json")
                        if a.correction.proposal is not None
                        else None
                    ),
                    attempts=tuple(
                        AttemptView(
                            n=t.n,
                            verdict=t.verdict.value if t.verdict else None,
                            reason=t.reason,
                            revised_claim=(
                                t.revised_claim.model_dump(mode="json")
                                if t.revised_claim is not None
                                else None
                            ),
                        )
                        for t in a.correction.attempts
                    ),
                ),
            )
            for a in report.audits
        ),
        errors=tuple(
            ClaimErrorRecord(
                index=e.index,
                target_urn=e.claim.target_urn,
                claim=e.claim.model_dump(mode="json"),
                error=e.error,
            )
            for e in report.errors
        ),
        receipts=Receipts(
            latency_ms=report.latency_ms,
            input_tokens=report.cost.input_tokens,
            output_tokens=report.cost.output_tokens,
            usd=report.cost.usd,
            steps=len(report.trace),
            trajectory_ok=report.trajectory.ok,
            trajectory_summary=report.trajectory.summary,
            rules_checked=tuple(r.value for r in report.trajectory.checked),
            catalog_lookups=report.catalog.lookups,
            catalog_fetches=report.catalog.fetches,
            catalog_entities=report.catalog.entities,
        ),
        steps=tuple(
            StepView(
                seq=n,
                name=s.name,
                kind=s.kind.value,
                claim_index=s.claim_index,
                latency_ms=s.latency_ms,
                input_tokens=s.input_tokens,
                output_tokens=s.output_tokens,
                cost_usd=s.cost_usd,
            )
            for n, s in enumerate(report.trace)
        ),
        dropped=tuple(str(d) for d in report.dropped),
        injection_findings=tuple(str(f) for f in report.injection_findings),
    )
