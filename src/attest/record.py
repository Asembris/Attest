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

--------------------------------------------------------------------------------
Session 5: the record is what a resumed run is rebuilt FROM, so its gaps became bugs
--------------------------------------------------------------------------------

Durable resume (replay.py) reconstitutes a parked run's typed ledger out of this record.
That turned every lossy field here from a cosmetic omission into a correctness one: what
the record does not carry, a restarted run cannot report, and it would report something
ELSE instead — silently, and only on the resume path.

The sharpest was `StepView.models`. Trace.cost declares the run's dollar total unknown
(`None`, never `0`) when any model that spent tokens has no price, and it identifies those
models from `StepRecord.models`. Drop the model names on the way to disk and a rehydrated
run recomputes `usd = sum(...)` where the original correctly said "I do not know" — a
restarted audit fabricating a cost figure the original honestly refused to state. That is
the None-is-not-zero rule (cost.py) breaking inside Attest's own receipts, which is the
exact class of failure this project exists to catch.

So four things were added, and one changed shape:

  - `StepView.models` and `StepView.error` — see above; and a step that FAILED must still
    say so after a restart, or the resumed trace shows a clean run that was not one.
  - `ClaimRecord.rejected` — the model drafts the guard threw away. An auditor that
    quietly retries until something passes is hiding its own failure rate; one that
    forgets it did so on restart is hiding it twice.
  - `ClaimRecord.faithfulness_violations` — WHICH tokens failed the guard, not just that
    something did. `faithful: false` with no violations is a verdict with no evidence.
  - conflicts, dropped claims and injection findings became STRUCTURED pairs rather than
    rendered strings. A `str()` cannot be parsed back, so a rehydrated run could not
    re-render them and the resumed report differed from the original in exactly the
    fields that describe what went wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from attest.report import (
    AuditReport,
    CorrectionOutcome,
    PublicationStatus,
    ReviewStatus,
    RunStatus,
)


class EvidenceView(BaseModel):
    """One catalog field that produced a verdict. `value=None` means: the catalog is silent."""

    model_config = ConfigDict(frozen=True)

    field: str
    value: Any = None
    note: str | None = None


class ConflictView(BaseModel):
    """A disagreement between the model and the deterministic core. Never resolved."""

    model_config = ConfigDict(frozen=True)

    kind: str
    detail: str


class ViolationView(BaseModel):
    """One factual token in an explanation that the evidence did not support."""

    model_config = ConfigDict(frozen=True)

    token: str
    kind: str


class DroppedView(BaseModel):
    """A claim the decomposer produced and Attest refused to carry, and why.

    `payload` is the model's raw output for it — kept whole, because a hallucinated URN is
    only inspectable if you can see what was hallucinated.
    """

    model_config = ConfigDict(frozen=True)

    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)


class FindingView(BaseModel):
    """One instruction-like span redacted from the agent's output before a model saw it."""

    model_config = ConfigDict(frozen=True)

    pattern: str
    matched: str


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


class PublicationView(BaseModel):
    """Whether this claim's VERDICT has been cleared to reach the catalog.

    Separate from `CorrectionView.review`, and the separation is the Option A decision
    (report.PublicationStatus): publishing a verdict is not the same act as accepting a
    correction, and a projection that folded them back together would re-create exactly the
    coupling that kept STOOD_FIRM contradictions — and every Supported and
    Insufficient-Coverage verdict — out of the catalog entirely.
    """

    model_config = ConfigDict(frozen=True)

    status: PublicationStatus = PublicationStatus.PENDING
    reviewer: str = ""

    @property
    def awaits_human(self) -> bool:
        return self.status is PublicationStatus.PENDING

    @property
    def published(self) -> bool:
        return self.status is PublicationStatus.PUBLISHED


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
    # The identity of the catalog snapshot this verdict was decided against (Session 21).
    # Persisted, because the write-back happens at approval time from THIS record and must
    # not re-fetch the catalog to recover it — a moved catalog would make the stored identity
    # a lie. What the record does not carry, a resumed run cannot write back. See writeback.py.
    snapshot_id: str = ""

    explanation: str = ""
    # "model" or "template". A reader is entitled to know whether the prose in front of
    # them was written or fallen back to, and hiding it would flatter the semantic layer.
    explanation_source: str = "template"
    faithful: bool = True
    # WHICH tokens the guard rejected, not merely that it rejected something. `faithful:
    # false` on its own is a verdict with no evidence — the one shape this project is
    # least entitled to ship.
    faithfulness_violations: tuple[ViolationView, ...] = ()
    conflicts: tuple[ConflictView, ...] = ()
    # Every model draft that was thrown away, and why. An auditor that quietly retries
    # until something passes is hiding its own failure rate.
    rejected: tuple[str, ...] = ()

    correction: CorrectionView
    publication: PublicationView = PublicationView()

    @property
    def awaits_human(self) -> bool:
        """Is this claim still waiting on a person, for anything? See report.ClaimAudit."""
        return self.publication.awaits_human or self.correction.awaits_human


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
    # The models this step called. Empty for deterministic and IO steps — and load-bearing
    # for the LLM ones: this is how Trace.cost knows which models it could not price, and
    # therefore how a run's total goes to None instead of to a plausible sum. Lose these
    # and a resumed run invents a dollar figure the original refused to state. See the
    # module docstring.
    models: tuple[str, ...] = ()
    # A step that raised still ran, and the trace says so. A restart that forgot it would
    # show a clean run that was not one.
    error: str | None = None


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
    # Both are gaps in the audit, and both are surfaced rather than swallowed. Structured,
    # not rendered: a string cannot be parsed back, and a resumed run has to rebuild these.
    dropped: tuple[DroppedView, ...] = ()
    injection_findings: tuple[FindingView, ...] = ()

    @property
    def proposals(self) -> tuple[ClaimRecord, ...]:
        """Corrections that re-verified clean and are waiting on a human."""
        return tuple(c for c in self.claims if c.correction.awaits_human)

    @property
    def awaiting(self) -> tuple[ClaimRecord, ...]:
        """Every claim still waiting on a person — to publish its verdict, or to rule on a
        correction. This is what the checkpoint loop exits on, and it is wider than
        `proposals`: since Session 15 every verdict needs clearing, not just corrections."""
        return tuple(c for c in self.claims if c.awaits_human)

    @property
    def published(self) -> tuple[ClaimRecord, ...]:
        """Claims whose verdict a human cleared for the catalog. What write-back fires on."""
        return tuple(c for c in self.claims if c.publication.published)

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
                snapshot_id=a.snapshot_id,
                explanation=a.explanation.text,
                explanation_source=a.explanation.source,
                faithful=a.explanation.faithfulness.ok,
                faithfulness_violations=tuple(
                    ViolationView(token=v.token, kind=v.kind)
                    for v in a.explanation.faithfulness.violations
                ),
                conflicts=tuple(
                    ConflictView(kind=c.kind, detail=c.detail) for c in a.conflicts
                ),
                rejected=tuple(a.explanation.rejected),
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
                publication=PublicationView(
                    status=a.publication.status, reviewer=a.publication.reviewer
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
                models=s.models,
                error=s.error,
            )
            for n, s in enumerate(report.trace)
        ),
        dropped=tuple(
            DroppedView(reason=d.reason, payload=d.payload) for d in report.dropped
        ),
        injection_findings=tuple(
            FindingView(pattern=f.pattern, matched=f.matched)
            for f in report.injection_findings
        ),
    )
