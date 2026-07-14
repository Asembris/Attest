"""Trajectory verification: did the pipeline actually take the path it claims to take?

The sharpest cheap-shot at any "multi-agent system" is that it is one clever prompt wearing
a costume — that the graph, the nodes, and the routing are decoration around a single model
call that does all the work. This module is the answer, and the answer has to be an
*assertion* rather than a log line, or it is not an answer at all.

So: every step of every run is recorded with a KIND (see observe.py), and this module holds
the trace to the architecture Attest claims to have. It is not a checklist of node names.
Checking that the nodes you called got called is trivially true and proves nothing; a graph
that ran every node in the right order and let a model pick the verdict would sail through
it. What is checked here is the set of properties that would be VIOLATED if the deterministic
core had been quietly hollowed out:

  1. NO_LLM_IN_THE_VERDICT_PATH — the single most important assertion in this file, and the
     one that turns "the deterministic core is sacred" from a claim in a README into a
     property of the run. Between resolving an entity and returning its verdict, no model is
     called: not by a checker, not by a helper, not by anything. Because steps carry a kind
     and LLM steps carry token counts, this is checkable after the fact — a checker that
     started calling a model would blow this up even if it returned the right answers, and
     even if every other test stayed green.

  2. NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK — a verdict in the report must trace back to
     a checker that actually ran on that claim. A verdict that appeared without one was
     invented somewhere.

  3. NO_EXPLANATION_WITHOUT_THE_GUARD — nothing reaches a reader unless faithfulness.check
     ran on it, after it was written. Bypassing the guard is the cheapest way to make the
     live suite pass and the product worthless.

  4. NO_CLAIM_WITHOUT_DECOMPOSITION — every audited claim came out of the decomposer, which
     came out of the sanitizer. A claim minted mid-pipeline never passed the URN check.

  5. NO_CORRECTION_WITHOUT_RE_VERIFICATION — a proposed correction must have been re-checked
     by the deterministic core. A loop that believed the model's revision would still look
     right in the report; it would be wrong here.

  6. ROUTING_MATCHED_THE_CLAIM — a freshness claim was handled by the freshness checker.
     This is what makes conditional routing load-bearing rather than decorative: a
     misrouted claim is caught, instead of producing a confident verdict from the wrong
     checker.

  7. RETRY_CAP_HELD — no claim was revised more than MAX_RETRIES times. The cap is enforced
     by a graph edge; this proves the edge works, which a `while` loop with a counter cannot.

The expected path is declared HERE, not derived from the graph's edges. That is the point:
if it were read off the graph, it would agree with the graph by construction and assert
nothing. Rewire graph.py and these still say what a run is supposed to look like.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from attest.claims import ClaimType
from attest.observe import StepKind, Trace

# The canonical step names. Shared with graph.py so a typo cannot silently make an
# invariant vacuous ("no step named 'fathfulness_guard' violated the rule!"). The names
# are shared; the EXPECTATIONS below are not — those are this module's own.
SANITIZE = "sanitize"
DECOMPOSE = "decompose"
RESOLVE = "resolve_entity"
EXPLAIN = "explain"
GUARD = "faithfulness_guard"
REVISE = "revise"
RECHECK = "recheck"
CHECKPOINT = "human_checkpoint"
ASSEMBLE = "assemble_report"

# One checker node per claim type. Routing is by claim type, and invariant 6 holds the
# router to it.
CHECKER: dict[ClaimType, str] = {
    ClaimType.FRESHNESS: "check_freshness",
    ClaimType.OWNERSHIP: "check_ownership",
    ClaimType.CLASSIFICATION: "check_classification",
    ClaimType.SCHEMA: "check_schema",
}
CHECKERS: frozenset[str] = frozenset(CHECKER.values())

# The hard cap on self-correction attempts per claim. A demo cannot be allowed to spiral
# and an unbounded loop is a cost bug waiting to happen.
MAX_RETRIES = 2

# The steps that produce or re-produce a verdict. Nothing in here may be an LLM step, and
# no LLM step may sit between resolving an entity and one of these. This tuple IS
# invariant 1.
VERDICT_STEPS: frozenset[str] = CHECKERS | {RECHECK}


class Rule(StrEnum):
    """The named invariants. A violation cites one, so a failure says which law it broke."""

    NO_LLM_IN_THE_VERDICT_PATH = "no-llm-in-the-verdict-path"
    NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK = "no-verdict-without-a-deterministic-check"
    NO_EXPLANATION_WITHOUT_THE_GUARD = "no-explanation-without-the-guard"
    NO_CLAIM_WITHOUT_DECOMPOSITION = "no-claim-without-decomposition"
    NO_CORRECTION_WITHOUT_RE_VERIFICATION = "no-correction-without-re-verification"
    ROUTING_MATCHED_THE_CLAIM = "routing-matched-the-claim"
    RETRY_CAP_HELD = "retry-cap-held"


@dataclass(frozen=True)
class Violation:
    """One architectural invariant the run broke."""

    rule: Rule
    detail: str
    claim_index: int | None = None

    def __str__(self) -> str:
        where = "" if self.claim_index is None else f" [claim {self.claim_index}]"
        return f"{self.rule.value}{where}: {self.detail}"


@dataclass(frozen=True)
class TrajectoryReport:
    """Whether the run took the path it was supposed to, and which laws it broke if not."""

    ok: bool
    violations: tuple[Violation, ...] = ()
    # Which rules were actually exercised. A run with no corrections cannot exercise
    # NO_CORRECTION_WITHOUT_RE_VERIFICATION, and a report that quietly counted it as
    # passed would be overclaiming — the same sin as a checker returning Supported from
    # silence.
    checked: tuple[Rule, ...] = ()

    @property
    def summary(self) -> str:
        if self.ok:
            return f"trajectory ok ({len(self.checked)} rules exercised)"
        return "; ".join(str(v) for v in self.violations)


@dataclass(frozen=True)
class AuditedClaim:
    """The slice of a ClaimAudit trajectory verification needs.

    A plain structural view, so this module does not import report.py and report.py can
    import this one. It also keeps the verifier honest: it checks the trace against what
    the report SAYS happened, and cannot be handed the graph's internals to agree with.
    """

    index: int
    claim_type: ClaimType
    has_verdict: bool
    has_explanation: bool
    correction_attempts: int
    has_proposal: bool


def verify(
    trace: Trace,
    claims: tuple[AuditedClaim, ...],
    max_retries: int = MAX_RETRIES,
) -> TrajectoryReport:
    """Hold a completed run to the architecture. Returns violations; raises nothing."""
    violations: list[Violation] = []
    checked: set[Rule] = set()

    names = trace.names

    # --- 4. every claim came out of the decomposer, which came out of the sanitizer ---
    if claims:
        checked.add(Rule.NO_CLAIM_WITHOUT_DECOMPOSITION)
        for required in (SANITIZE, DECOMPOSE):
            if required not in names:
                violations.append(
                    Violation(
                        rule=Rule.NO_CLAIM_WITHOUT_DECOMPOSITION,
                        detail=(
                            f"{len(claims)} claims were audited but {required!r} never ran. "
                            f"A claim that did not come through decomposition never had its "
                            f"URN checked against the source text."
                        ),
                    )
                )
        if SANITIZE in names and DECOMPOSE in names:
            if names.index(SANITIZE) > names.index(DECOMPOSE):
                violations.append(
                    Violation(
                        rule=Rule.NO_CLAIM_WITHOUT_DECOMPOSITION,
                        detail="decomposition ran before sanitization: the model saw "
                        "un-redacted agent output",
                    )
                )

    for audited in claims:
        i = audited.index
        steps = trace.for_claim(i)
        step_names = [s.name for s in steps]

        checker_steps = [s for s in steps if s.name in CHECKERS]
        recheck_steps = [s for s in steps if s.name == RECHECK]
        revise_steps = [s for s in steps if s.name == REVISE]

        # --- 2. a verdict must trace back to a checker that ran on THIS claim ---------
        if audited.has_verdict:
            checked.add(Rule.NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK)
            if not checker_steps:
                violations.append(
                    Violation(
                        rule=Rule.NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK,
                        claim_index=i,
                        detail=(
                            "the report carries a verdict but no deterministic checker ran "
                            f"on this claim. Steps seen: {step_names or 'none'}."
                        ),
                    )
                )
            if RESOLVE not in step_names:
                violations.append(
                    Violation(
                        rule=Rule.NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK,
                        claim_index=i,
                        detail="a verdict was reached without resolving the entity: the "
                        "checker had no snapshot to read",
                    )
                )

        # --- 6. the checker that ran is the one this claim type routes to -------------
        if checker_steps:
            checked.add(Rule.ROUTING_MATCHED_THE_CLAIM)
            expected = CHECKER[audited.claim_type]
            for step in checker_steps:
                if step.name != expected:
                    violations.append(
                        Violation(
                            rule=Rule.ROUTING_MATCHED_THE_CLAIM,
                            claim_index=i,
                            detail=(
                                f"a {audited.claim_type.value} claim was handled by "
                                f"{step.name!r}, not {expected!r}. The router sent it to the "
                                f"wrong checker, which will have produced a confident verdict "
                                f"from the wrong question."
                            ),
                        )
                    )

        # --- 1. NO MODEL ANYWHERE IN THE VERDICT PATH --------------------------------
        # The heart of it. Two ways to break the rule, and both are checked: a verdict
        # step that is itself an LLM step, and an LLM step smuggled in between resolving
        # the entity and deciding on it.
        verdict_steps = [s for s in steps if s.name in VERDICT_STEPS]
        if verdict_steps:
            checked.add(Rule.NO_LLM_IN_THE_VERDICT_PATH)

            for step in verdict_steps:
                if step.kind is not StepKind.DETERMINISTIC:
                    violations.append(
                        Violation(
                            rule=Rule.NO_LLM_IN_THE_VERDICT_PATH,
                            claim_index=i,
                            detail=(
                                f"{step.name!r} decided a verdict but ran as a "
                                f"{step.kind.value} step. Verdicts come from code."
                            ),
                        )
                    )
                if step.total_tokens:
                    violations.append(
                        Violation(
                            rule=Rule.NO_LLM_IN_THE_VERDICT_PATH,
                            claim_index=i,
                            detail=(
                                f"{step.name!r} spent {step.total_tokens} tokens. A checker "
                                f"that calls a model is not a checker, whatever it returns."
                            ),
                        )
                    )

            # Nothing may sit between resolve and the FIRST verdict step but code. This is
            # what catches an "evidence enrichment" or "entity disambiguation" LLM call
            # quietly added upstream of a checker: the checker itself stays clean, and the
            # model still shaped the verdict.
            if RESOLVE in step_names:
                start = step_names.index(RESOLVE)
                end = min(
                    n for n, s in enumerate(steps) if s.name in VERDICT_STEPS
                )
                for step in steps[start:end]:
                    if step.is_llm:
                        violations.append(
                            Violation(
                                rule=Rule.NO_LLM_IN_THE_VERDICT_PATH,
                                claim_index=i,
                                detail=(
                                    f"{step.name!r} called a model between resolving the "
                                    f"entity and checking it. The verdict path must contain "
                                    f"no model call."
                                ),
                            )
                        )

        # --- 3. no explanation ships unless the guard ran on it, afterwards -----------
        if audited.has_explanation:
            checked.add(Rule.NO_EXPLANATION_WITHOUT_THE_GUARD)
            if GUARD not in step_names:
                violations.append(
                    Violation(
                        rule=Rule.NO_EXPLANATION_WITHOUT_THE_GUARD,
                        claim_index=i,
                        detail=(
                            "an explanation is in the report but the faithfulness guard "
                            "never ran on it. Unverified model prose reached the reader."
                        ),
                    )
                )
            elif EXPLAIN in step_names and step_names.index(GUARD) < step_names.index(EXPLAIN):
                violations.append(
                    Violation(
                        rule=Rule.NO_EXPLANATION_WITHOUT_THE_GUARD,
                        claim_index=i,
                        detail="the guard ran before the explanation it was supposed to "
                        "check",
                    )
                )

        # --- 7. the retry cap is a graph edge, and it held ----------------------------
        if revise_steps:
            checked.add(Rule.RETRY_CAP_HELD)
            if len(revise_steps) > max_retries:
                violations.append(
                    Violation(
                        rule=Rule.RETRY_CAP_HELD,
                        claim_index=i,
                        detail=(
                            f"the claim was revised {len(revise_steps)} times; the cap is "
                            f"{max_retries}. The loop is unbounded."
                        ),
                    )
                )

        # --- 5. a correction is never accepted on the model's say-so ------------------
        if audited.correction_attempts or audited.has_proposal:
            checked.add(Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION)

            if audited.has_proposal and not recheck_steps:
                violations.append(
                    Violation(
                        rule=Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION,
                        claim_index=i,
                        detail=(
                            "a corrected claim is being proposed to a human, but it was "
                            "never re-verified against the catalog. The loop believed the "
                            "model."
                        ),
                    )
                )
            if len(recheck_steps) > len(revise_steps):
                violations.append(
                    Violation(
                        rule=Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION,
                        claim_index=i,
                        detail=(
                            f"{len(recheck_steps)} re-checks for {len(revise_steps)} "
                            f"revisions: something was verified that was never revised"
                        ),
                    )
                )

    return TrajectoryReport(
        ok=not violations,
        violations=tuple(violations),
        checked=tuple(sorted(checked, key=lambda r: r.value)),
    )
