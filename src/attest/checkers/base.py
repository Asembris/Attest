"""Shared plumbing for checkers.

A checker is a pure function of (claim, snapshot) -> CheckResult. It performs no
I/O: the snapshot is fetched once, up front, and handed in. That keeps every checker
trivially testable against a fixture, and it means a checker can never turn a network
failure into a verdict.
"""

from __future__ import annotations

from attest.claims import BaseClaim, CheckResult, Evidence, Verdict


def result(
    claim: BaseClaim,
    verdict: Verdict,
    reason: str,
    *evidence: Evidence,
) -> CheckResult:
    if not evidence:
        raise ValueError("a verdict without evidence is not auditable")
    return CheckResult(claim=claim, verdict=verdict, evidence=evidence, reason=reason)


def worst(verdicts: list[Verdict]) -> Verdict:
    """Fold per-part verdicts into one for a claim asserting several things at once.

    A claim like "it has columns a, b, and c" is a conjunction, so one Contradicted
    part disproves the whole thing regardless of how the rest fared — contradiction
    dominates. Insufficient-Coverage beats Supported for the same reason in reverse:
    if any part is unverifiable, the conjunction as a whole has not been verified,
    and reporting Supported would overclaim.
    """
    if Verdict.CONTRADICTED in verdicts:
        return Verdict.CONTRADICTED
    if Verdict.INSUFFICIENT_COVERAGE in verdicts:
        return Verdict.INSUFFICIENT_COVERAGE
    return Verdict.SUPPORTED
