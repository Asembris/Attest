"""One-to-one claim matching, and the five defects it closes.

Scorer v1 bound a case's verdict with one expression:

    next((a for a in report.audits if a.claim.target_urn == case.target_urn), None)

and judged fidelity with a `_same_subject` whose schema arm compared column NAMES only.
Between them they produced five separable defects, and **every one of them was reproduced
against the real v1 code before a line of v2 was written**:

  1. a right column with a wrong native type read as a FAITHFUL extraction, so a
     transcription bug was reported as a checker CORRECTNESS failure;
  2. a claim of the wrong FAMILY about the right entity had its verdict scored as the
     case's answer — a true answer to a question the label never asked;
  3. an exact match extracted SECOND lost to a wrong claim extracted first;
  4. a claim the decomposer said twice read as a perfect extraction;
  5. an unintended extra claim was structurally invisible — nothing counted it.

Each test below therefore asserts in BOTH directions: the specific v1 behavior it is named
for gets the case wrong, and v2 gets it right. One generic "the old selector was bad" test
would prove only that something changed. These prove WHICH thing, one defect at a time —
and if a later session reverts any single piece of the matching, exactly one test goes red
and its name says which piece.

The v1 fragments are kept HERE, in the test file, and never in `benchmark/`. Product code
carries no legacy path to fall back to; these are quotations, used as sabotage.

Offline tier by construction: no catalog, no key, no model. That is the point — v1's
selector lived inside `run_full`, which could not be called offline at all (the injected
Catalog never set `.client`), so no test could reach the five defects even in principle.
"""

from __future__ import annotations

from typing import Any

import pytest
from benchmark.cases import CASES, Case
from benchmark.matching import (
    Fidelity,
    fingerprint,
    match,
    norm_type,
    subject,
    subject_distance,
)
from benchmark.run_eval import (
    NO_CLAIM,
    Catalog,
    build_claim,
    predict_for_case,
    run_full,
    score,
)
from pydantic import TypeAdapter

from _snapshots import load_snapshot
from attest.claims import Claim, Evidence, Verdict
from attest.explain import Explanation
from attest.faithfulness import Faithfulness
from attest.observe import Trace
from attest.report import (
    AuditReport,
    ClaimAudit,
    Correction,
    CorrectionOutcome,
    RunStatus,
)
from attest.trajectory import TrajectoryReport

DATASET = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders.orders_fact,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.finance.revenue_daily,PROD)"
PII = "urn:li:tag:PII"
NONPII = "urn:li:tag:NonPII"
ALICE = "urn:li:corpuser:alice.chen"

_ADAPTER: TypeAdapter[Claim] = TypeAdapter(Claim)


# --- payloads, claims, audits, cases -----------------------------------------


def freshness(hours: float, urn: str = DATASET) -> dict[str, Any]:
    return {"claim_type": "freshness", "target_urn": urn,
            "raw_text": "refreshed daily", "max_age_hours": hours}


def ownership(owner: str = ALICE, urn: str = DATASET) -> dict[str, Any]:
    return {"claim_type": "ownership", "target_urn": urn,
            "raw_text": "owned by alice", "owner_urn": owner}


def classification(
    labels: tuple[str, ...] = (PII,), present: bool = True,
    field_path: str | None = None, urn: str = DATASET,
) -> dict[str, Any]:
    return {"claim_type": "classification", "target_urn": urn, "raw_text": "contains PII",
            "labels": list(labels), "present": present, "field_path": field_path}


def schema(columns: list[dict[str, Any]], urn: str = DATASET) -> dict[str, Any]:
    return {"claim_type": "schema", "target_urn": urn,
            "raw_text": "has an order_total column", "columns": columns}


def col(name: str, native_type: str | None = None) -> dict[str, Any]:
    return {"name": name, "native_type": native_type}


def as_claim(payload: dict[str, Any]) -> Claim:
    return _ADAPTER.validate_python({k: v for k, v in payload.items() if v is not None})


def audit(index: int, payload: dict[str, Any], verdict: Verdict) -> ClaimAudit:
    return ClaimAudit(
        index=index,
        claim=as_claim(payload),
        verdict=verdict,
        reason="deterministic reason",
        evidence=(Evidence(field="f", value=None),),
        explanation=Explanation(text="t", source="model", faithfulness=Faithfulness(ok=True)),
        correction=Correction(outcome=CorrectionOutcome.NOT_ATTEMPTED),
    )


def case_for(payload: dict[str, Any], verdict: str, cid: str = "probe-01") -> Case:
    return Case(
        id=cid,
        agent_text=f"{payload['target_urn']}: {payload['raw_text']}.",
        target_urn=payload["target_urn"],
        claim=payload,
        expected_verdict=verdict,
        rationale="a synthetic case, for the matcher only",
    )


# --- scorer v1, quoted verbatim. The sabotage directions. ---------------------


def legacy_first_urn_selection(case: Case, audits) -> ClaimAudit | None:
    """v1's selector, verbatim from run_full. Defect 3: first URN hit wins."""
    return next((a for a in audits if a.claim.target_urn == case.target_urn), None)


def legacy_verdict(case: Case, audits) -> str:
    """v1 read the verdict off whatever that selector bound. Defect 2: family unchecked."""
    picked = legacy_first_urn_selection(case, audits)
    return picked.verdict.value if picked else NO_CLAIM


def legacy_schema_subject(extracted: Claim, expected: Claim) -> bool:
    """v1's schema arm of `_same_subject`, verbatim. Defect 1: native_type dropped."""
    return {c.name for c in extracted.columns} == {c.name for c in expected.columns}


def legacy_leftovers(case: Case, audits) -> list[ClaimAudit]:
    """What v1 did with everything the selector did not bind: nothing at all.

    There was no second pass over `report.audits`, so an unmatched claim was neither
    counted nor named. Defects 4 and 5 are both this function returning an empty list.
    """
    return []


# =============================================================================
# The five regressions. Each names ONE v1 behavior and proves it wrong.
# =============================================================================


def test_1_a_right_column_with_the_wrong_type_is_not_a_faithful_extraction():
    """DEFECT 1: `_same_subject` compared column names and dropped `native_type`.

    So "order_total is NUMBER(12,2)" transcribed as "order_total is VARCHAR(32)" scored
    `extraction_ok=True`, the checker correctly answered Contradicted against a label that
    says Supported, and the harness filed a DECOMPOSER bug under the heading
    benchmark/README.md calls the worst thing this product can do.
    """
    want = schema([col("order_total", "NUMBER(12,2)")])
    got = schema([col("order_total", "VARCHAR(32)")])

    # v1: the names match, so the subject "matched".
    assert legacy_schema_subject(as_claim(got), as_claim(want)) is True

    case = case_for(want, "Supported")
    p = predict_for_case(case, [audit(0, got, Verdict.CONTRADICTED)])

    assert p.fidelity == Fidelity.PARTIAL.value
    assert p.extraction_ok is False
    assert any("native_type" in d for d in p.subject_diff), p.subject_diff
    # Still scored end to end: there IS a verdict about this subject and the user got it.
    assert p.predicted == "Contradicted"

    m = score([p])
    assert m.correctness_failures == 1, "the user received a wrong outcome; it still counts"
    assert m.correctness_failures_from_extraction == 1, "and the cause is named"
    assert m.partial_subjects[0]["case"] == case.id


def test_2_a_wrong_family_claim_is_never_the_answer_to_the_question_asked():
    """DEFECT 2: v1 matched on URN alone, so any family's verdict could be scored.

    A classification verdict is a true answer to a question this schema case did not ask.
    Reporting it as this case's answer is the laundering NO_CLAIM exists to refuse.
    """
    case = case_for(schema([col("order_total")]), "Supported")
    audits = [audit(0, classification(), Verdict.INSUFFICIENT_COVERAGE)]

    # v1: bound a classification claim and scored its verdict as the schema case's answer.
    assert legacy_verdict(case, audits) == "Insufficient-Coverage"

    p = predict_for_case(case, audits)
    assert p.fidelity == Fidelity.WRONG_FAMILY.value
    assert p.predicted == NO_CLAIM
    assert p.extraction_ok is False
    # Kept for diagnosis: it is what the model actually produced about this entity.
    assert p.extracted is not None

    m = score([p])
    assert m.wrong_family == [case.id]
    # Nothing was verdicted, so it is neither kind of verdict confusion.
    assert m.correctness_failures == 0
    assert m.coverage_failures == 0


def test_3_an_exact_match_wins_however_late_it_was_extracted():
    """DEFECT 3: `next(...)` took the first URN hit, so extraction ORDER decided the score."""
    want = schema([col("order_total", "NUMBER(12,2)")])
    case = case_for(want, "Supported")
    audits = [
        audit(0, schema([col("ssn")]), Verdict.CONTRADICTED),
        audit(1, want, Verdict.SUPPORTED),
    ]

    # v1: bound index 0 and scored Contradicted, with the exact match sitting right there.
    assert legacy_first_urn_selection(case, audits).index == 0

    p = predict_for_case(case, audits)
    assert p.fidelity == Fidelity.EXACT.value
    assert p.predicted == "Supported"
    # The wrong claim is not forgiven for having lost — it is still an unasked-for claim.
    assert p.extras == 1
    assert p.clean_extraction is False


def test_4_a_claim_the_decomposer_said_twice_is_counted_not_discarded():
    """DEFECT 4: everything after the first URN hit was dropped, so a repeat read as clean."""
    want = schema([col("order_total", "NUMBER(12,2)")])
    case = case_for(want, "Supported")
    audits = [audit(0, want, Verdict.SUPPORTED), audit(1, want, Verdict.SUPPORTED)]

    # v1: one claim bound, the identical second one never examined.
    assert legacy_leftovers(case, audits) == []

    p = predict_for_case(case, audits)
    assert p.duplicates == 1
    assert p.extras == 0, "a repeat of a bound subject is a duplicate, not a generic extra"
    assert p.fidelity == Fidelity.EXACT.value
    assert p.clean_extraction is False, "exact, but not clean — something was left over"

    m = score([p])
    assert m.duplicate_claims == 1
    assert m.accuracy == 1.0, "a duplicate is a fidelity failure and must not move accuracy"


def test_5_an_unintended_extra_claim_is_counted_even_about_another_entity():
    """DEFECT 5: an extra claim was invisible — no field recorded it, about any URN.

    Each benchmark case is audited on its own prose, so a claim about a different dataset
    belongs to nothing: it is unintended output, and it is counted as such.
    """
    want = schema([col("order_total", "NUMBER(12,2)")])
    case = case_for(want, "Supported")
    audits = [
        audit(0, want, Verdict.SUPPORTED),
        audit(1, classification(urn=OTHER), Verdict.SUPPORTED),
    ]

    assert legacy_leftovers(case, audits) == []

    p = predict_for_case(case, audits)
    assert p.extras == 1
    assert p.duplicates == 0
    assert p.predicted == "Supported"

    m = score([p])
    assert m.extra_claims == 1
    assert m.accuracy == 1.0, "an extra is a fidelity failure and must not move accuracy"


# =============================================================================
# Normalization: formatting only, never semantics.
# =============================================================================


def test_native_type_normalizes_formatting_and_nothing_else():
    """The guard against over-strictness — without which this fix trades one lie for another.

    `just bench-full`'s cases spell the same vocabulary both ways (`VARCHAR(255)` in
    schema-01, `varchar(255)` in schema-04). Case and whitespace are how a sentence was
    typed; precision is what it SAID.
    """
    assert norm_type("VARCHAR(255)") == norm_type("varchar(255)")
    assert norm_type("NUMBER(12,2)") == norm_type("number(12, 2)")
    assert norm_type("  NUMBER(12,2) ") == norm_type("NUMBER(12,2)")

    assert norm_type("NUMBER(12,2)") != norm_type("NUMBER")
    assert norm_type("VARCHAR(255)") != norm_type("VARCHAR(36)")
    assert norm_type("NUMBER") != norm_type("VARCHAR")


def test_an_absent_type_is_not_a_declared_type():
    """"has a payload column" and "has a payload column of type VARCHAR" differ.

    `None` collapsing into any declared type would let the decomposer invent a type
    assertion the sentence never made, and score it as a faithful transcription.
    """
    assert norm_type(None) is None
    assert norm_type(None) != norm_type("")
    assert subject(as_claim(schema([col("payload")]))) != subject(
        as_claim(schema([col("payload", "VARCHAR(255)")]))
    )


def test_the_case_of_a_type_does_not_make_a_case_partial():
    want = schema([col("email", "VARCHAR(255)")])
    got = schema([col("email", "varchar(255)")])
    case = case_for(want, "Supported")
    p = predict_for_case(case, [audit(0, got, Verdict.SUPPORTED)])
    assert p.fidelity == Fidelity.EXACT.value
    assert p.clean_extraction is True


# =============================================================================
# The canonical subject
# =============================================================================


def test_order_within_a_claim_does_not_change_its_subject():
    """The order a model lists columns or labels in is not part of what it asserted."""
    assert subject(as_claim(schema([col("a", "INT"), col("b")]))) == subject(
        as_claim(schema([col("b"), col("a", "int")]))
    )
    assert subject(as_claim(classification(labels=(PII, NONPII)))) == subject(
        as_claim(classification(labels=(NONPII, PII)))
    )


def test_a_subject_is_a_multiset_so_a_repeated_column_survives():
    """Sets would collapse a doubled column — hiding, one level down, the exact duplicate
    this module exists to count one level up."""
    once = subject(as_claim(schema([col("a", "INT")])))
    twice = subject(as_claim(schema([col("a", "INT"), col("a", "INT")])))
    assert once != twice


def test_every_family_field_is_part_of_the_subject():
    base = subject(as_claim(classification()))
    assert base != subject(as_claim(classification(present=False)))
    assert base != subject(as_claim(classification(field_path="email")))
    assert base != subject(as_claim(classification(labels=(NONPII,))))
    assert subject(as_claim(freshness(24))) != subject(as_claim(freshness(0.5)))
    assert subject(as_claim(ownership(ALICE))) != subject(
        as_claim(ownership("urn:li:corpuser:bob.martinez"))
    )


def test_a_typeless_column_does_not_crash_the_sort():
    """`sorted()` over (name, type) pairs raises comparing None to str. A benchmark that
    dies on a typeless column is not an option, and schema-03 is exactly that claim."""
    assert subject(as_claim(schema([col("b"), col("a", "INT"), col("c")])))


# =============================================================================
# The matching itself
# =============================================================================


def test_no_extracted_claim_can_satisfy_two_labels():
    """One-to-one. Two identical labels and one extracted claim: one binds, one is MISSING."""
    want = as_claim(schema([col("order_total", "NUMBER(12,2)")]))
    m = match([want, want], [want])
    assert [p.fidelity for p in m.pairs] == [Fidelity.EXACT, Fidelity.MISSING]
    assert m.pairs[0].extracted_index == 0
    assert m.pairs[1].extracted_index is None
    assert m.extras == () and m.duplicates == ()


def test_exact_matches_are_bound_before_any_partial_one_can_consume_them():
    """The tier ordering is what makes the greedy assignment optimal.

    Expected A matches extracted[0] exactly; expected B would also accept it as a partial.
    Pass 1 must complete for BOTH before pass 2 runs, or B steals A's exact match.
    """
    a = as_claim(freshness(24))
    b = as_claim(freshness(48))
    m = match([b, a], [a])
    assert m.pairs[1].fidelity is Fidelity.EXACT, "the exact match must not be consumed"
    assert m.pairs[1].extracted_index == 0
    assert m.pairs[0].fidelity is Fidelity.MISSING


def test_a_partial_candidate_is_chosen_by_subject_distance_then_position():
    """Deterministic tie-break, and never set or dict iteration order."""
    want = as_claim(schema([col("a", "INT"), col("b", "INT")]))
    far = as_claim(schema([col("x", "TEXT"), col("y", "TEXT")]))
    near = as_claim(schema([col("a", "INT"), col("b", "TEXT")]))
    assert subject_distance(want, near) < subject_distance(want, far)

    m = match([want], [far, near])
    assert m.pairs[0].extracted_index == 1, "the nearer subject wins, not the earlier one"
    assert m.pairs[0].fidelity is Fidelity.PARTIAL
    assert m.extras == (0,)


def test_matching_is_stable_across_equivalent_orderings():
    want_a = as_claim(freshness(24))
    want_b = as_claim(ownership(ALICE))
    first = match([want_a, want_b], [want_b, want_a])
    second = match([want_a, want_b], [want_b, want_a])
    assert first == second
    assert [p.extracted_index for p in first.pairs] == [1, 0]
    assert first.extras == () and first.duplicates == ()


def test_extras_and_duplicates_are_disjoint_and_account_for_everything_unmatched():
    want = as_claim(schema([col("order_total", "NUMBER(12,2)")]))
    other = as_claim(classification(urn=OTHER))
    m = match([want], [want, want, other])
    assert m.duplicates == (1,)
    assert m.extras == (2,)
    assert m.unmatched == (1, 2)


def test_a_fingerprint_is_a_sorted_multiset_of_subjects():
    a = as_claim(freshness(24))
    b = as_claim(ownership(ALICE))
    assert fingerprint([a, b]) == fingerprint([b, a]), "order is not instability"
    assert fingerprint([a, a]) != fingerprint([a]), "saying it twice is a different run"


def test_nothing_extracted_at_all_is_a_gap_not_a_verdict():
    case = case_for(schema([col("order_total")]), "Supported")
    p = predict_for_case(case, [])
    assert p.fidelity == Fidelity.MISSING.value
    assert p.predicted == NO_CLAIM
    assert p.extracted is None
    assert p.extraction_ok is False


# =============================================================================
# The full-pipeline scorer, driven offline
# =============================================================================


class ScriptedPipeline:
    """A pipeline that returns a canned report per case. No model, no catalog, no network.

    This is what `run_full`'s injection is FOR. v1's five defects lived inside `run_full`,
    which could not be called offline at all — `Catalog(snapshot_source=...)` never set
    `.client`, so building the real Pipeline raised AttributeError. A path no test can
    reach is a path nobody has checked.
    """

    def __init__(self, extra_for: str | None = None, duplicate_for: str | None = None):
        self.extra_for = extra_for
        self.duplicate_for = duplicate_for
        self.forgotten: list[str] = []
        self._by_text = {c.agent_text: c for c in CASES}

    def run(self, text: str) -> AuditReport:
        case = self._by_text[text]
        claim = build_claim(case)
        audits = [
            ClaimAudit(
                index=0, claim=claim, verdict=Verdict(case.expected_verdict),
                reason="scripted", evidence=(Evidence(field="f", value=None),),
                explanation=Explanation(
                    text="t", source="model", faithfulness=Faithfulness(ok=True)
                ),
                correction=Correction(outcome=CorrectionOutcome.NOT_ATTEMPTED),
            )
        ]
        if case.id == self.duplicate_for:
            audits.append(audits[0])
        if case.id == self.extra_for:
            audits.append(audit(9, classification(urn=OTHER), Verdict.SUPPORTED))
        return AuditReport(
            source_text=text, audits=tuple(audits), status=RunStatus.COMPLETE,
            trace=Trace(), trajectory=TrajectoryReport(ok=True), thread_id=f"t-{case.id}",
        )

    def forget(self, thread_id: str) -> None:
        self.forgotten.append(thread_id)


def test_the_full_scorer_runs_offline_and_reports_fidelity_apart_from_accuracy():
    catalog = Catalog(snapshot_source=load_snapshot)
    pipeline = ScriptedPipeline(extra_for="own-01", duplicate_for="fresh-01")

    predictions = run_full(catalog, pipeline=pipeline)
    m = score(predictions)

    assert len(predictions) == len(CASES)
    assert len(pipeline.forgotten) == len(CASES), "every run must be forgotten, parked or not"

    # A perfect transcription of every sentence: accuracy is untouched by the leftovers.
    assert m.accuracy == 1.0
    assert m.fidelity == {Fidelity.EXACT.value: len(CASES)}
    assert m.extraction_failures == 0

    # ...and the leftovers are reported, by their own names, in their own place.
    assert m.extra_claims == 1
    assert m.duplicate_claims == 1
    assert m.clean_extractions == len(CASES) - 2
    assert m.correctness_failures == 0 and m.coverage_failures == 0


def test_the_core_mode_carries_no_fidelity_because_it_extracts_nothing():
    """Fidelity is None in core mode, not EXACT.

    A default of EXACT would have a run that never called a model quietly asserting a
    perfect extraction — a claim about work that did not happen, which is the shape of
    thing this whole repository exists to refuse.
    """
    from benchmark.run_eval import run_core

    m = score(run_core(Catalog(snapshot_source=load_snapshot)))
    assert m.scored_extraction is False
    assert m.fidelity == {}
    assert m.clean_extractions == 0
    assert m.extra_claims == 0 and m.duplicate_claims == 0


@pytest.mark.parametrize("case", CASES[:4], ids=[c.id for c in CASES[:4]])
def test_a_real_labeled_claim_matches_itself_exactly(case):
    """The identity property. If a label cannot match its own claim, nothing else means
    anything — and it would show up as a benchmark-wide extraction collapse."""
    p = predict_for_case(
        case, [audit(0, case.claim, Verdict(case.expected_verdict))]
    )
    assert p.fidelity == Fidelity.EXACT.value
    assert p.clean_extraction is True
    assert p.correct
