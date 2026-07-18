"""What an audit run returns: verdicts, explanations, proposed corrections, and receipts.

Three things here are deliberate.

**A correction has an OUTCOME, not a success flag.** "Did self-correction work?" has more
than two honest answers, and collapsing them would hide the interesting ones. An agent
that stood by its claim because the catalog does not say what the truth is has done
something quite different from an agent that tried twice and stayed wrong, which is
different again from one whose revision was rejected for changing the subject. Each is
reported by name. A loop that only ever reported "corrected / not corrected" would make
its own failure modes invisible, which is the failure mode this project is about.

**A proposal is PENDING until a human says otherwise, and pending is the default.** The
review status of an unattended correction is not "accepted", and it is not "rejected"
either — it is *unreviewed*, and it ships that way. See graph.py for why the checkpoint is
a real graph state rather than a flag.

**The receipts are measured.** cost and latency come from observe.Trace, which reads the
clock and the API's own token counts. Nothing here is estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from attest.claims import CheckResult, Claim, Evidence, Verdict
from attest.cost import Cost
from attest.crosscheck import Conflict
from attest.datahub import CacheStats
from attest.decompose import Dropped
from attest.explain import Explanation
from attest.observe import Trace
from attest.sanitize import Finding
from attest.trajectory import TrajectoryReport


class CorrectionOutcome(StrEnum):
    """How the self-correction loop ended for one claim. Six answers, not two."""

    # The verdict was not Contradicted, so nothing was attempted. The common case.
    NOT_ATTEMPTED = "not-attempted"
    # The agent revised, and the revision RE-VERIFIED as Supported by the same
    # deterministic checker. This is the only outcome that produces a proposal.
    CORRECTED = "corrected"
    # The agent revised, the revision was re-verified, and it is still not Supported.
    # Retries remain unspent only if the cap has not been hit — see EXHAUSTED.
    NOT_CORRECTED = "not-corrected"
    # The retry cap was reached and the claim is still not Supported.
    EXHAUSTED = "exhausted"
    # The agent declined to revise: the evidence does not say what the correct value is.
    # An honest non-answer. NOT a failure, and not a correction.
    STOOD_FIRM = "stood-firm"
    # The revision was refused before it was ever verified — it retargeted the claim,
    # changed its type, or failed the claim schema. See revise.py.
    REFUSED = "refused"


class ReviewStatus(StrEnum):
    """Where a proposed correction stands with the human who has to sign it off.

    PENDING is the default and it is the whole point: Attest never settles a correction on
    its own, and an unattended run leaves its proposals unreviewed rather than applied.
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class PublicationStatus(StrEnum):
    """Whether a claim's VERDICT has been cleared to reach the catalog (Session 15).

    Publication is a separate act from accepting a correction, and keeping them separate is
    the whole of the Option A decision. Until Session 15 they were one thing: a verdict was
    written back only if the claim's CORRECTION had been accepted. Two consequences, and
    both were backwards.

    **`STOOD_FIRM` never published.** The gate was `outcome is CORRECTED`, so the only
    verdict that ever reached the catalog was a contradiction the agent had successfully
    self-corrected. The single most damning thing Attest can find — an agent claims an `ssn`
    column, the catalog says no, and the agent STANDS BY the false claim (measured: 2 of 6
    runs attempt the column swap, which is why `revise.subject()` exists) — was silently
    swallowed. A compliance auditor that publishes "the agent was wrong and fixed it" while
    hiding "the agent was wrong and refused to" is publishing the good news.

    **And Supported and Insufficient-Coverage never published at all**, which had Attest
    committing its own cardinal sin in its own output. This product exists to say that
    ABSENCE IS NOT AN ANSWER — an untagged table does not mean "PII-free", it means nobody
    looked. Yet a dataset with no Attest verdict was ambiguous between "we checked and it
    was fine" and "we never checked", and nothing in the catalog could tell them apart. The
    write-back has to obey the rule the checkers obey. Now it does: Attest records that it
    reviewed, whatever the verdict.

    PENDING is the resting state, for the same reason it is for a correction: nothing is
    published by default, and an unattended run publishes nothing.
    """

    PENDING = "pending"
    # A human cleared this verdict for the catalog. This is what the write-back fires on.
    PUBLISHED = "published"
    # A human looked and declined to publish it. A decision, not an absence.
    WITHHELD = "withheld"


@dataclass(frozen=True)
class Publication:
    """Where a claim's verdict stands with the human who has to clear it for the catalog."""

    status: PublicationStatus = PublicationStatus.PENDING
    reviewer: str = ""

    @property
    def awaits_human(self) -> bool:
        return self.status is PublicationStatus.PENDING

    @property
    def published(self) -> bool:
        return self.status is PublicationStatus.PUBLISHED


@dataclass(frozen=True)
class Attempt:
    """One turn of the correction loop: what the agent said, and what the catalog said back.

    `verdict` is the re-verification result — produced by the same deterministic checker
    that produced the original verdict, against the same snapshot. The model does not get
    to grade its own correction.
    """

    n: int
    revised_claim: Claim | None
    verdict: Verdict | None
    reason: str
    rejected: str | None = None


@dataclass(frozen=True)
class Correction:
    """The self-correction loop's record for one claim."""

    outcome: CorrectionOutcome
    attempts: tuple[Attempt, ...] = ()
    # The revised claim that re-verified clean. Only ever set when outcome is CORRECTED,
    # and even then it is a PROPOSAL: nothing in Attest writes it anywhere.
    proposal: Claim | None = None
    review: ReviewStatus = ReviewStatus.PENDING

    @property
    def was_attempted(self) -> bool:
        return self.outcome is not CorrectionOutcome.NOT_ATTEMPTED

    @property
    def succeeded(self) -> bool:
        return self.outcome is CorrectionOutcome.CORRECTED

    @property
    def awaits_human(self) -> bool:
        return self.succeeded and self.review is ReviewStatus.PENDING


@dataclass(frozen=True)
class ClaimError:
    """A claim that could not be checked at all. NOT a verdict, and never scored as one.

    An unresolvable URN is the case that matters: the entity does not exist, so the
    catalog neither disagrees with the claim nor is silent about it — the question was
    malformed. Reporting that as Insufficient-Coverage would launder a bad URN into a
    legitimate-looking audit result, and the bad URN would never be seen. So these live
    in their own list, outside `audits`, and they are counted in no verdict tally.

    (CLAUDE.md listed "entity-not-found propagation" as deferred, with the pipeline to
    decide how it surfaces. This is that decision.)
    """

    index: int
    claim: Claim
    error: str


@dataclass(frozen=True)
class ClaimAudit:
    """One claim, audited end to end."""

    index: int
    claim: Claim
    verdict: Verdict
    reason: str
    evidence: tuple[Evidence, ...]
    explanation: Explanation
    correction: Correction
    publication: Publication = field(default_factory=Publication)
    # The identity of the catalog snapshot this verdict was decided against (Session 21).
    # Captured here, at audit time, from the snapshot the checker actually ran against — the
    # only honest source, because a write-back re-fetch would let the catalog move underneath
    # it. Carried into the published artifact so an inherited verdict names its ground truth.
    snapshot_id: str = ""

    @property
    def conflicts(self) -> tuple[Conflict, ...]:
        return self.explanation.conflicts

    @property
    def awaits_human(self) -> bool:
        """Is this claim still waiting on a person, for anything?

        TWO independent things can be pending, and this is where they are combined rather
        than confused: the VERDICT is waiting to be published, and — only where the loop
        produced one — a CORRECTION is waiting to be accepted. A Supported claim has no
        correction and still awaits publication; a Contradicted claim that STOOD FIRM has no
        proposal and still awaits publication. Correction is one thing that can happen to a
        Contradicted claim; it is not the gate.
        """
        return self.publication.awaits_human or self.correction.awaits_human

    @classmethod
    def from_check(
        cls,
        index: int,
        result: CheckResult,
        explanation: Explanation,
        correction: Correction,
        snapshot_id: str = "",
    ) -> ClaimAudit:
        return cls(
            index=index,
            claim=result.claim,
            verdict=result.verdict,
            reason=result.reason,
            evidence=result.evidence,
            explanation=explanation,
            correction=correction,
            snapshot_id=snapshot_id,
        )


class RunStatus(StrEnum):
    """Where the run itself stands."""

    # Ran to the end. Either there was nothing to propose, or the proposals were reviewed.
    COMPLETE = "complete"
    # Parked at the human checkpoint: there are corrections nobody has signed off. The
    # verdicts are final and readable; only the CORRECTIONS are unsettled.
    AWAITING_REVIEW = "awaiting-review"
    # The run did not keep to its own architecture — a trajectory invariant was violated
    # (trajectory.py). The report is NOT trustworthy evidence: a checker may have called a
    # model, an explanation may have skipped the guard, a correction may have been proposed
    # unverified. A flagged run is UN-APPROVABLE and nothing it proposes may reach the
    # catalog. This is a hard gate, not an alarm — see graph._report and api/service.approve.
    FLAGGED = "flagged"


@dataclass(frozen=True)
class AuditReport:
    """The output of one audit run, and its receipts."""

    source_text: str
    audits: tuple[ClaimAudit, ...]
    status: RunStatus
    trace: Trace
    trajectory: TrajectoryReport
    # Claims that could not be checked. Kept out of `audits` so they cannot be counted
    # as verdicts — an error is not an Insufficient-Coverage.
    errors: tuple[ClaimError, ...] = ()
    # Claims the decomposer produced but Attest refused to carry — a hallucinated URN,
    # a claim that failed validation. A gap in the audit, surfaced as one.
    dropped: tuple[Dropped, ...] = ()
    # Instruction-like spans redacted from the agent's output. An agent that tried to
    # talk its way out of the audit is a finding in its own right.
    injection_findings: tuple[Finding, ...] = ()
    # A run-level failure (decomposition died, DataHub unreachable). Verdicts, if any,
    # still stand; this says what did not happen.
    error: str | None = None
    # Thread id, for resuming a run parked at the checkpoint.
    thread_id: str = ""
    # What the run asked the catalog for, and how much of it was one snapshot answering
    # many claims. Every verdict in this report was decided against the entities named
    # here, read once each. See datahub/cache.py.
    catalog: CacheStats = CacheStats()

    # --- receipts ------------------------------------------------------------

    @property
    def cost(self) -> Cost:
        return self.trace.cost

    @property
    def latency_ms(self) -> float:
        return self.trace.latency_ms

    @property
    def proposals(self) -> tuple[ClaimAudit, ...]:
        """Corrections that re-verified clean and are waiting on a human."""
        return tuple(a for a in self.audits if a.correction.awaits_human)

    def by_verdict(self, verdict: Verdict) -> tuple[ClaimAudit, ...]:
        return tuple(a for a in self.audits if a.verdict is verdict)

    @property
    def is_suspicious(self) -> bool:
        return bool(self.injection_findings)

    def receipts(self) -> str:
        """The line the README quotes. Measured, never estimated — see observe.py."""
        seconds = self.latency_ms / 1000
        return (
            f"audited {len(self.audits)} claims in {seconds:.1f}s "
            f"({self.cost.display()})"
        )

    def summary(self) -> dict[str, Any]:
        """A flat, printable digest of the run. What the CLI shows and the README quotes."""
        return {
            "claims": len(self.audits),
            "uncheckable_claims": len(self.errors),
            "verdicts": {
                v.value: len(self.by_verdict(v)) for v in Verdict if self.by_verdict(v)
            },
            "corrections": {
                o.value: sum(1 for a in self.audits if a.correction.outcome is o)
                for o in CorrectionOutcome
                if any(a.correction.outcome is o for a in self.audits)
            },
            "proposals_awaiting_review": len(self.proposals),
            "dropped_claims": len(self.dropped),
            "injection_findings": len(self.injection_findings),
            "explanations_from_template": sum(
                1 for a in self.audits if a.explanation.is_fallback
            ),
            "conflicts": sum(len(a.conflicts) for a in self.audits),
            "status": self.status.value,
            "trajectory_ok": self.trajectory.ok,
            "steps": len(self.trace),
            "latency_ms": round(self.latency_ms, 1),
            "tokens": self.cost.total_tokens,
            "usd": self.cost.usd,
            # The catalog receipt: entity lookups the claims asked for, versus GraphQL
            # round trips actually made. Equal means the cache saved nothing.
            "catalog_lookups": self.catalog.lookups,
            "catalog_fetches": self.catalog.fetches,
            "catalog_entities": self.catalog.entities,
        }


@dataclass(frozen=True)
class Decision:
    """A human's call on one claim. The only way anything leaves PENDING.

    **TWO SEPARATE ACTS, AND THEY MAY NOT SHARE A BOOLEAN.** Until Session 15 this carried
    one `accept` flag, which meant "accept this correction" and — as a side effect nobody
    chose — also "publish this claim's verdict". Those are different decisions about
    different things:

        publish            should this VERDICT reach the catalog?   Every claim has one.
        accept_correction  is this PROPOSED REVISION right?         Only a CORRECTED claim
                           has one to answer.

    Overloading them is the exact mistake `CorrectionOutcome`'s six names exist to prevent:
    a flag that means two things hides the case where they disagree. A human can perfectly
    well publish a Contradicted verdict while rejecting the correction the agent proposed
    for it — "you were wrong, and your suggested fix is also wrong" — and the old shape
    could not say that.

    Both are OPTIONAL, and that is Session 14's rule, not a convenience: only what you name
    is settled. A decision that names neither leaves the claim exactly as PENDING as it was
    found, and the run stays parked and re-decidable.
    """

    claim_index: int
    # None means "no opinion": this decision does not settle publication, and the verdict
    # stays PENDING. It is not "no".
    publish: bool | None = None
    # None means "no opinion", or that there is no proposal to rule on.
    accept_correction: bool | None = None
    reviewer: str = ""
    note: str = ""

    @property
    def settles_anything(self) -> bool:
        return self.publish is not None or self.accept_correction is not None
