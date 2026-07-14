"""Trajectory verification: does it actually catch a pipeline that cheated?

A trajectory check that only ever passes is worse than none — it is a green light wired to
nothing, and it would let exactly the failure it exists to prevent through while looking
like proof that it cannot happen. So the tests that matter here are the ones that BREAK the
graph and demand that the verifier notices.

Two levels, and both are needed:

  - Against hand-built traces, one per rule, because a rule that no test can make fail is
    not a rule.
  - Against a SABOTAGED REAL PIPELINE — a graph with the guard node ripped out, a checker
    that secretly calls a model, a router wired to the wrong checker. These are the edits a
    hurried refactor would actually make, they leave every other test in this suite green,
    and each one produces a report that looks entirely normal. That is the whole point:
    trajectory verification is the only thing standing between "the pipeline did what it
    says" and "the pipeline says it did."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from attest import trajectory
from attest.claims import ClaimType
from attest.graph import Pipeline
from attest.llm import LLM
from attest.observe import StepKind, Trace
from attest.trajectory import (
    CHECKER,
    DECOMPOSE,
    EXPLAIN,
    GUARD,
    RECHECK,
    RESOLVE,
    REVISE,
    SANITIZE,
    AuditedClaim,
    Rule,
    verify,
)
from fakes import (
    FakeCatalog,
    FakeChat,
    claim_reply,
    dataset,
    explanation_reply,
    revision_reply,
)

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
ALICE = "urn:li:corpuser:alice.chen"
CAROL = "urn:li:corpuser:carol.davis"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

CATALOG = {
    SF: dataset(
        SF,
        last_modified=NOW - timedelta(hours=6),
        owners=(CAROL,),
        tags=("urn:li:tag:PII",),
        terms=(),
        custom_properties={},
    )
}

SAYS = f"{SF} is owned by {ALICE}."


def ownership_claim(owner: str) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": SF,
        "raw_text": f"{SF} is owned by {owner}.",
        "owner_urn": owner,
    }


ONE_OWNERSHIP_CLAIM = (
    AuditedClaim(
        index=0,
        claim_type=ClaimType.OWNERSHIP,
        has_verdict=True,
        has_explanation=True,
        correction_attempts=0,
        has_proposal=False,
    ),
)


def step(trace: Trace, name: str, kind: StepKind, claim_index: int | None = 0, **kw) -> None:
    with trace.step(name, kind, claim_index=claim_index, **kw):
        pass


def good_trace() -> Trace:
    """The path a healthy ownership claim takes. Every rule below breaks exactly one thing."""
    t = Trace()
    step(t, SANITIZE, StepKind.DETERMINISTIC, claim_index=None)
    step(t, DECOMPOSE, StepKind.LLM, claim_index=None)
    step(t, RESOLVE, StepKind.IO)
    step(t, CHECKER[ClaimType.OWNERSHIP], StepKind.DETERMINISTIC)
    step(t, EXPLAIN, StepKind.LLM)
    step(t, GUARD, StepKind.DETERMINISTIC)
    return t


def test_a_healthy_trajectory_passes_and_says_which_rules_it_exercised():
    report = verify(good_trace(), ONE_OWNERSHIP_CLAIM)

    assert report.ok, report.summary
    # A report that claimed to have exercised a rule it never reached would be
    # overclaiming — the same sin as a checker returning Supported from silence.
    assert Rule.NO_LLM_IN_THE_VERDICT_PATH in report.checked
    assert Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION not in report.checked


# --- one test per rule, each breaking exactly one thing -----------------------


def test_it_catches_a_verdict_with_no_deterministic_check_behind_it():
    t = good_trace()
    t.steps = [s for s in t.steps if s.name != CHECKER[ClaimType.OWNERSHIP]]

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(
        v.rule is Rule.NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK for v in report.violations
    ), report.summary


def test_it_catches_an_explanation_that_never_reached_the_guard():
    t = good_trace()
    t.steps = [s for s in t.steps if s.name != GUARD]

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(
        v.rule is Rule.NO_EXPLANATION_WITHOUT_THE_GUARD for v in report.violations
    ), report.summary


def test_it_catches_a_guard_that_ran_before_the_thing_it_was_guarding():
    t = Trace()
    step(t, SANITIZE, StepKind.DETERMINISTIC, claim_index=None)
    step(t, DECOMPOSE, StepKind.LLM, claim_index=None)
    step(t, RESOLVE, StepKind.IO)
    step(t, CHECKER[ClaimType.OWNERSHIP], StepKind.DETERMINISTIC)
    step(t, GUARD, StepKind.DETERMINISTIC)  # before, not after
    step(t, EXPLAIN, StepKind.LLM)

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(v.rule is Rule.NO_EXPLANATION_WITHOUT_THE_GUARD for v in report.violations)


def test_it_catches_a_checker_that_is_secretly_a_model_call():
    """A checker declared DETERMINISTIC that ran as an LLM step. The kind is a claim."""
    t = good_trace()
    for s in t.steps:
        if s.name == CHECKER[ClaimType.OWNERSHIP]:
            s.kind = StepKind.LLM

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(v.rule is Rule.NO_LLM_IN_THE_VERDICT_PATH for v in report.violations)


def test_it_catches_a_checker_that_spent_tokens_while_calling_itself_deterministic():
    """The kind is what the node SAYS it is; the token count is what it DID.

    A checker that kept its label and quietly called a model is the exact failure mode
    that would otherwise pass every test in the repo — the verdicts might even be right.
    """
    t = good_trace()
    for s in t.steps:
        if s.name == CHECKER[ClaimType.OWNERSHIP]:
            s.input_tokens, s.output_tokens = 800, 40

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    violation = next(v for v in report.violations if v.rule is Rule.NO_LLM_IN_THE_VERDICT_PATH)
    assert "840 tokens" in violation.detail


def test_it_catches_a_model_call_smuggled_between_resolving_and_checking():
    """The subtle one: the checker stays clean, and the model still shaped the verdict.

    An "evidence enrichment" or "entity disambiguation" step upstream of a checker leaves
    every checker pure and still lets a model decide what the checker gets to see.
    """
    t = Trace()
    step(t, SANITIZE, StepKind.DETERMINISTIC, claim_index=None)
    step(t, DECOMPOSE, StepKind.LLM, claim_index=None)
    step(t, RESOLVE, StepKind.IO)
    step(t, "enrich_evidence", StepKind.LLM)  # <-- innocuous-looking, fatal
    step(t, CHECKER[ClaimType.OWNERSHIP], StepKind.DETERMINISTIC)
    step(t, EXPLAIN, StepKind.LLM)
    step(t, GUARD, StepKind.DETERMINISTIC)

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    violation = next(v for v in report.violations if v.rule is Rule.NO_LLM_IN_THE_VERDICT_PATH)
    assert "enrich_evidence" in violation.detail


def test_it_catches_a_claim_routed_to_the_wrong_checker():
    """A freshness claim answered by the ownership checker returns a confident wrong answer."""
    t = good_trace()
    for s in t.steps:
        if s.name == CHECKER[ClaimType.OWNERSHIP]:
            s.name = CHECKER[ClaimType.FRESHNESS]

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(v.rule is Rule.ROUTING_MATCHED_THE_CLAIM for v in report.violations)


def test_it_catches_a_correction_loop_that_blew_through_the_cap():
    t = good_trace()
    for _ in range(3):  # cap is 2
        step(t, REVISE, StepKind.LLM)
        step(t, RECHECK, StepKind.DETERMINISTIC)

    report = verify(
        t,
        (
            AuditedClaim(
                index=0,
                claim_type=ClaimType.OWNERSHIP,
                has_verdict=True,
                has_explanation=True,
                correction_attempts=3,
                has_proposal=False,
            ),
        ),
        max_retries=2,
    )

    assert not report.ok
    assert any(v.rule is Rule.RETRY_CAP_HELD for v in report.violations)


def test_it_catches_a_correction_proposed_to_a_human_without_re_verification():
    """A loop that believed the model's revision. The report would look entirely normal."""
    t = good_trace()
    step(t, REVISE, StepKind.LLM)
    # ...and no RECHECK. The proposal goes straight to the human.

    report = verify(
        t,
        (
            AuditedClaim(
                index=0,
                claim_type=ClaimType.OWNERSHIP,
                has_verdict=True,
                has_explanation=True,
                correction_attempts=1,
                has_proposal=True,
            ),
        ),
    )

    assert not report.ok
    assert any(
        v.rule is Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION for v in report.violations
    ), report.summary


def test_it_catches_claims_that_never_came_out_of_the_decomposer():
    t = good_trace()
    t.steps = [s for s in t.steps if s.name != DECOMPOSE]

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(v.rule is Rule.NO_CLAIM_WITHOUT_DECOMPOSITION for v in report.violations)


def test_it_catches_a_model_that_saw_the_agents_output_before_it_was_sanitized():
    t = Trace()
    step(t, DECOMPOSE, StepKind.LLM, claim_index=None)
    step(t, SANITIZE, StepKind.DETERMINISTIC, claim_index=None)  # too late
    step(t, RESOLVE, StepKind.IO)
    step(t, CHECKER[ClaimType.OWNERSHIP], StepKind.DETERMINISTIC)
    step(t, EXPLAIN, StepKind.LLM)
    step(t, GUARD, StepKind.DETERMINISTIC)

    report = verify(t, ONE_OWNERSHIP_CLAIM)

    assert not report.ok
    assert any(v.rule is Rule.NO_CLAIM_WITHOUT_DECOMPOSITION for v in report.violations)


# --- and now against a REAL, SABOTAGED pipeline ------------------------------
#
# The hand-built traces above prove the verifier's logic. They cannot prove it is wired to
# anything. These do: they take the real graph, make the edit a hurried refactor would
# make, and check that the run's OWN trajectory report goes red. Every other assertion in
# the suite stays green through each of these.


def _pipeline(*replies: str, **kw) -> Pipeline:
    return Pipeline(
        llm=LLM(client=FakeChat(replies=list(replies))),
        client=FakeCatalog(CATALOG),
        now=NOW,
        **kw,
    )


def test_a_real_pipeline_with_the_guard_torn_out_fails_its_own_trajectory_check():
    """Skip the guard node. The verdicts are still right. The report is still readable.

    And the run reports itself as architecturally broken, which is the only thing that
    would tell anyone.
    """
    p = _pipeline(
        claim_reply([ownership_claim(CAROL)]),
        explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
    )
    # The sabotage: the guard node becomes a no-op that records nothing. Rebuilding the
    # graph re-binds the node, so the compiled graph really does skip it.
    p._guard = lambda state: {}  # type: ignore[method-assign]
    p.graph = p._build()

    report = p.run(SAYS)

    assert not report.trajectory.ok, "the guard was skipped and nobody noticed"
    assert any(
        v.rule is Rule.NO_EXPLANATION_WITHOUT_THE_GUARD for v in report.trajectory.violations
    ), report.trajectory.summary
    # Note what did NOT change: the verdict is still correct, and the explanation still
    # reads fine. Nothing else in the suite would have caught this.
    assert report.audits[0].verdict.value == "Supported"


def test_a_real_pipeline_whose_checker_calls_a_model_fails_its_own_trajectory_check():
    """The deterministic core, hollowed out. The verdict returned is still correct."""
    p = _pipeline(
        claim_reply([ownership_claim(CAROL)]),
        explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
    )

    real_checker = p._checker_node

    def sabotaged(name: str):
        node = real_checker(name)

        def wrapped(state):
            out = node(state)
            # A model call inside the verdict path. The checker still returns the right
            # answer — that is exactly why this is dangerous and why it needs catching.
            ledger = p._ledgers[state["thread_id"]]
            ledger.trace.steps[-1].input_tokens = 500
            ledger.trace.steps[-1].output_tokens = 20
            return out

        return wrapped

    p._checker_node = sabotaged  # type: ignore[method-assign]
    p.graph = p._build()

    report = p.run(SAYS)

    assert not report.trajectory.ok
    assert any(
        v.rule is Rule.NO_LLM_IN_THE_VERDICT_PATH for v in report.trajectory.violations
    ), report.trajectory.summary
    assert report.audits[0].verdict.value == "Supported", "and the verdict was still right"


def test_a_real_pipeline_that_proposes_an_unverified_correction_fails_its_own_check():
    """Believe the model's revision; skip the re-check. The proposal looks perfectly good."""
    p = _pipeline(
        claim_reply([ownership_claim(ALICE)]),
        explanation_reply("the catalog lists a different owner.", "Contradicted", []),
        revision_reply(ownership_claim(CAROL)),
    )

    real_recheck = p._recheck

    def sabotaged(state):
        out = real_recheck(state)
        # The sabotage: erase the evidence that re-verification ever happened, exactly as
        # a loop that trusted the model would have no such evidence to erase.
        ledger = p._ledgers[state["thread_id"]]
        ledger.trace.steps = [s for s in ledger.trace.steps if s.name != RECHECK]
        return out

    p._recheck = sabotaged  # type: ignore[method-assign]
    p.graph = p._build()

    report = p.run(SAYS)

    assert report.proposals, "there is still a correction on offer to a human"
    assert not report.trajectory.ok
    assert any(
        v.rule is Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION
        for v in report.trajectory.violations
    ), report.trajectory.summary


def test_a_misroute_crashes_today_rather_than_returning_a_confident_wrong_answer():
    """First line of defence: each checker asserts the claim type it was handed.

    Worth pinning, because it is what makes ROUTING_MATCHED_THE_CLAIM a BACKSTOP rather
    than the only thing standing between a misrouted claim and a confident wrong verdict.
    A misroute today is loud. The next test is about keeping it catchable if it ever
    stops being.
    """
    p = _pipeline(claim_reply([ownership_claim(CAROL)]))
    p._route_by_claim_type = lambda state: ClaimType.FRESHNESS.value  # type: ignore[method-assign]
    p.graph = p._build()

    with pytest.raises(AssertionError):
        p.run(SAYS)


def test_a_real_pipeline_with_a_miswired_router_fails_its_own_trajectory_check(monkeypatch):
    """And if a checker is ever made TOLERANT, the misroute goes silent — and this catches it.

    The scenario is not hypothetical: the moment someone writes a checker that accepts more
    than one claim type (a generic "labels" checker over tags and terms, say), the type
    assert above stops firing and a routing bug starts returning verdicts instead of
    raising. They would be confident, well-evidenced, and answers to the wrong question.

    So this test removes the assert — simulating exactly that future — and demands that
    the run still reports itself as broken. The verdict it produces is a perfectly
    reasonable-looking Supported.
    """
    from attest import graph as graph_module
    from attest.checkers import check_ownership

    tolerant = dict(graph_module._CHECK)
    tolerant[CHECKER[ClaimType.FRESHNESS]] = lambda claim, snap, now: check_ownership(claim, snap)
    monkeypatch.setattr(graph_module, "_CHECK", tolerant)

    p = _pipeline(
        claim_reply([ownership_claim(CAROL)]),
        explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
    )
    p._route_by_claim_type = lambda state: ClaimType.FRESHNESS.value  # type: ignore[method-assign]
    p.graph = p._build()

    report = p.run(SAYS)

    # No crash. A clean-looking verdict, from the wrong checker.
    assert report.audits[0].verdict.value == "Supported"
    assert not report.trajectory.ok, "a silent misroute produced a confident verdict"
    assert any(
        v.rule is Rule.ROUTING_MATCHED_THE_CLAIM for v in report.trajectory.violations
    ), report.trajectory.summary


def test_the_rules_are_all_reachable():
    """Guards the guard: every named rule must be broken by some test above.

    Without this, adding a Rule nobody can trigger makes the verifier look stronger while
    asserting nothing — the same failure the 12-cell coverage matrix exists to prevent.
    """
    assert set(Rule) == {
        Rule.NO_LLM_IN_THE_VERDICT_PATH,
        Rule.NO_VERDICT_WITHOUT_A_DETERMINISTIC_CHECK,
        Rule.NO_EXPLANATION_WITHOUT_THE_GUARD,
        Rule.NO_CLAIM_WITHOUT_DECOMPOSITION,
        Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION,
        Rule.ROUTING_MATCHED_THE_CLAIM,
        Rule.RETRY_CAP_HELD,
    }, (
        "a Rule was added or removed. Every rule needs a test that BREAKS it — a rule no "
        "test can make fail is decoration."
    )


def test_max_retries_is_two():
    """The cap is a documented number, not an accident. Changing it should be deliberate."""
    assert trajectory.MAX_RETRIES == 2
