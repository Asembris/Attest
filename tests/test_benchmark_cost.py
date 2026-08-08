"""The benchmark's cost aggregate: unknown stays unknown, and a measured zero stays zero.

Scorer v2 totalled a full-pipeline run with `sum(p.usd or 0.0 for p in runs[0])`. A case
whose run used a model with no price reports `usd=None` -- honestly, all the way up from
cost.py -- and `or 0.0` turned that honest unknown into a numeric zero. The receipt then
carried a `cost_usd` that looked like a complete total while silently omitting every
unpriced call, and printed it as `$0.0000`.

That is the one lie cost.py exists to refuse, committed by the harness that measures the
system built to catch it:

    An unknown model costs None, never 0. ... a report that quietly totals it as zero
    would be a lie of exactly the kind Attest exists to catch, printed by Attest itself.
                                                          -- src/attest/cost.py

The distinction that makes this hard, and the reason half these tests exist: **a measured
zero is legitimate and load-bearing**. Core mode calls no model and spends exactly $0.00.
The scripted fake leaves Usage at zero tokens, so `Trace.cost` reports a KNOWN 0.0 and
`test_graph.py` asserts `report.cost.is_known` on that path. A fix that flagged every zero
as unknown would be just as wrong as the defect, in the other direction. So `known 0.0`
and `unknown` are pinned apart, from both sides.

Offline tier by construction: no catalog, no key, no model.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from benchmark.cases import CASES
from benchmark.run_eval import (
    Catalog,
    Prediction,
    Spend,
    build_claim,
    run_full,
    total_spend,
)

from _snapshots import load_snapshot
from attest.claims import Evidence, Verdict
from attest.explain import Explanation
from attest.faithfulness import Faithfulness
from attest.observe import StepKind, StepRecord, Trace
from attest.report import (
    AuditReport,
    ClaimAudit,
    Correction,
    CorrectionOutcome,
    RunStatus,
)
from attest.trajectory import TrajectoryReport

RUN_EVAL = Path(__file__).resolve().parents[1] / "benchmark" / "run_eval.py"

UNPRICED = "gpt-9-unreleased"
ALSO_UNPRICED = "claude-not-in-the-table"
THIRD_UNPRICED = "a-model-nobody-priced"


def priced(usd: float, case_id: str = "c") -> Prediction:
    """A case whose run was fully priced. `usd` is a real, known dollar figure."""
    return Prediction(case_id=case_id, expected="Supported", predicted="Supported", usd=usd)


def unpriced(*models: str, case_id: str = "c") -> Prediction:
    """A case whose run used a model with no price. usd is None -- unknown, not free."""
    return Prediction(
        case_id=case_id,
        expected="Supported",
        predicted="Supported",
        usd=None,
        unpriced_models=models,
    )


# --- the legacy expression, quoted as sabotage -------------------------------
#
# v1's aggregate, verbatim from run_eval.main. It lives HERE and never in benchmark/:
# the shipped code carries no legacy path to fall back to. It is a quotation, used to
# prove these tests reject the defect AND -- the direction that actually matters -- that
# they do not merely reject any zero.


def _legacy_spend(predictions: list[Prediction]) -> float:
    """v1: `sum(p.usd or 0.0 for p in runs[0])`. Unknown silently becomes zero."""
    return sum(p.usd or 0.0 for p in predictions)


def _cost_zero_fallbacks(source: str) -> list[str]:
    """Every `<something money-ish> or 0` in real code. Docstrings and comments excluded.

    Money only: `total or 0` on a DataHub result count and `timestampMillis or 0` in
    writeback are legitimate zero defaults for things that are not dollars, and a guard
    that flagged them would be noise nobody keeps.
    """
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        for value in node.values[1:]:
            zero = isinstance(value, ast.Constant) and value.value in (0, 0.0)
            if zero and not isinstance(value.value, bool):
                text = ast.unparse(node)
                if re.search(r"usd|cost|spend|price", text, re.IGNORECASE):
                    offenders.append(f"line {node.lineno}: {text}")
    return offenders


# --- the two states that must never converge ---------------------------------


def test_a_fully_priced_run_reports_its_exact_numeric_cost():
    spend = total_spend([priced(0.01), priced(0.0053102)])

    assert spend.usd == 0.01 + 0.0053102
    assert spend.known is True
    assert spend.unpriced_models == ()
    assert spend.cases_unpriced == 0


def test_a_measured_zero_stays_a_known_zero_and_is_never_reported_as_unknown():
    """The other half of the fix, and the easier one to break.

    Core mode calls no model and genuinely spends nothing; the scripted fake spends no
    tokens and `Trace.cost` calls that KNOWN zero. Reporting either as "unknown" would be
    the same category error as the defect, pointing the other way -- absence invented
    where a measurement exists.
    """
    spend = total_spend([priced(0.0), priced(0.0)])

    assert spend.usd == 0.0
    assert spend.known is True, "a measured zero is a measurement, not a missing value"
    assert spend.unpriced_models == ()
    assert spend.cases_unpriced == 0


def test_one_unpriced_case_makes_the_whole_total_unknown_rather_than_free():
    spend = total_spend([unpriced(UNPRICED)])

    assert spend.usd is None
    assert spend.known is False
    assert spend.unpriced_models == (UNPRICED,)
    assert spend.cases_unpriced == 1


def test_a_mix_of_priced_and_unpriced_cases_stays_unknown_overall():
    """A partial sum printed as a receipt is worse than none: it reads like a complete one.

    The priced cases here total $0.03. That number must not appear as the run's cost --
    it is a real figure about a strict subset, and a reader has no way to tell it from a
    total. cost.py refuses to print exactly this, and so does the harness.
    """
    spend = total_spend([priced(0.01), unpriced(UNPRICED), priced(0.02)])

    assert spend.usd is None
    assert spend.known is False
    assert spend.cases_unpriced == 1
    assert "0.03" not in json.dumps(spend.as_payload()), "a priced subtotal leaked out"


# --- determinism of the unpriced set -----------------------------------------


def test_the_same_unpriced_model_twice_is_reported_once():
    spend = total_spend([unpriced(UNPRICED, case_id="a"), unpriced(UNPRICED, case_id="b")])

    assert spend.unpriced_models == (UNPRICED,), "the model set must deduplicate"
    assert spend.cases_unpriced == 2, "...but the CASE count is not deduplicated"


def test_several_unpriced_models_come_back_sorted_and_independent_of_input_order():
    forwards = total_spend([unpriced(THIRD_UNPRICED, ALSO_UNPRICED), unpriced(UNPRICED)])
    backwards = total_spend([unpriced(UNPRICED), unpriced(ALSO_UNPRICED, THIRD_UNPRICED)])

    assert forwards.unpriced_models == tuple(sorted([UNPRICED, ALSO_UNPRICED, THIRD_UNPRICED]))
    assert forwards.unpriced_models == backwards.unpriced_models


def test_a_case_that_is_unknown_but_named_no_model_is_still_unknown():
    """Knownness keys on `usd is None`, never on whether the model names survived.

    `Trace.cost` finds unpriced models by NAME off the step, and Session 5 recorded what
    happens when those names are lost on the way to disk: the unpriced set comes back
    empty and the run computes a total where the original said "unknown". Keying the
    aggregate on the names would rebuild that bug one level up. It keys on the value.
    """
    spend = total_spend([unpriced()])

    assert spend.usd is None
    assert spend.known is False
    assert spend.unpriced_models == ()
    assert spend.cases_unpriced == 1


# --- edges -------------------------------------------------------------------


def test_a_run_with_no_cases_is_a_known_zero():
    """Preserved from v1, where `sum([])` was 0.0. Nothing ran, so nothing was spent."""
    spend = total_spend([])

    assert spend.usd == 0.0
    assert spend.known is True
    assert spend.unpriced_models == ()
    assert spend.cases_unpriced == 0


def test_the_payload_round_trips_through_json_without_collapsing_the_two_states():
    """The receipt is written with json.dumps and read back by tests, docs and the UI."""
    known = json.loads(json.dumps(total_spend([priced(0.0)]).as_payload()))
    unknown = json.loads(json.dumps(total_spend([unpriced(UNPRICED)]).as_payload()))

    assert known["cost_usd"] == 0.0 and known["cost_known"] is True
    assert unknown["cost_usd"] is None and unknown["cost_known"] is False
    assert known["cost_usd"] != unknown["cost_usd"], "known zero and unknown converged"
    assert unknown["unpriced_models"] == [UNPRICED], "the receipt must name the model"


def test_an_unknown_cost_is_never_rendered_as_a_dollar_figure():
    """The console line printed `${spend:.4f}` -- so an unknown run announced $0.0000."""
    rendered = total_spend([priced(0.01), unpriced(UNPRICED)]).display()

    assert "unknown" in rendered
    assert "$" not in rendered, f"a dollar figure was printed for an unknown cost: {rendered}"
    assert UNPRICED in rendered

    assert total_spend([priced(0.0)]).display() == "$0.0000"


# --- the sabotage, both directions -------------------------------------------


def test_the_legacy_expression_collapses_unknown_into_zero_and_the_new_one_does_not():
    """The vacuity check. Both halves are required.

    REJECTION: on an unpriced run, v1 answers 0.0 and v2 answers None. Restore the legacy
    expression in run_eval and this goes red.

    NON-VACUITY: on a genuinely-free run, v1 and v2 AGREE at 0.0. Without this half, an
    implementation that flagged every zero as unknown would pass the rejection half while
    being just as dishonest -- so this is the assertion that pins the fix to the actual
    defect rather than to "any zero looks suspicious".
    """
    mixed = [priced(0.01), unpriced(UNPRICED)]
    assert _legacy_spend(mixed) == 0.01, "v1 reported a subtotal as if it were the total"
    assert total_spend(mixed).usd is None, "v2 must refuse to total an unpriced run"

    free = [priced(0.0), priced(0.0)]
    assert _legacy_spend(free) == 0.0
    assert total_spend(free).usd == 0.0, "v2 must not invent an unknown out of a real zero"
    assert total_spend(free).known is True


# --- the pass-through, which a pure aggregator test cannot reach --------------


class _ScriptedPipeline:
    """Returns a canned report per case, with a trace that spends on a chosen model.

    `run_full` is where the model names have to survive: it reads `report.cost` and builds
    the Prediction. An aggregator tested only on hand-built Predictions agrees with itself
    by construction while `run_full` drops `unpriced_models` on the floor -- the failure
    trajectory.py names, one layer down.
    """

    def __init__(self, model: str | None):
        self.model = model
        self._by_text = {c.agent_text: c for c in CASES}

    def run(self, text: str) -> AuditReport:
        case = self._by_text[text]
        trace = Trace()
        if self.model is not None:
            trace.steps.append(
                StepRecord(
                    name="decompose",
                    kind=StepKind.LLM,
                    input_tokens=100,
                    output_tokens=50,
                    cost_usd=None,  # what cost.cost_usd returns for a model with no price
                    models=(self.model,),
                )
            )
        return AuditReport(
            source_text=text,
            audits=(
                ClaimAudit(
                    index=0,
                    claim=build_claim(case),
                    verdict=Verdict(case.expected_verdict),
                    reason="scripted",
                    evidence=(Evidence(field="f", value=None),),
                    explanation=Explanation(
                        text="t", source="model", faithfulness=Faithfulness(ok=True)
                    ),
                    correction=Correction(outcome=CorrectionOutcome.NOT_ATTEMPTED),
                ),
            ),
            status=RunStatus.COMPLETE,
            trace=trace,
            trajectory=TrajectoryReport(ok=True),
            thread_id=f"t-{case.id}",
        )

    def forget(self, thread_id: str) -> None:
        pass


def test_run_full_carries_the_unpriced_model_names_out_of_the_report():
    catalog = Catalog(snapshot_source=load_snapshot)

    predictions = run_full(catalog, pipeline=_ScriptedPipeline(UNPRICED))

    assert all(p.usd is None for p in predictions), "an unpriced run must not report a cost"
    assert all(p.unpriced_models == (UNPRICED,) for p in predictions)

    spend = total_spend(predictions)
    assert spend.usd is None
    assert spend.known is False
    assert spend.unpriced_models == (UNPRICED,), "the model name did not survive run_full"
    assert spend.cases_unpriced == len(CASES)


def test_run_full_on_a_run_that_spent_nothing_reports_a_known_zero():
    """The scripted fake's own path: no tokens, so `Trace.cost` is a KNOWN 0.0."""
    catalog = Catalog(snapshot_source=load_snapshot)

    predictions = run_full(catalog, pipeline=_ScriptedPipeline(None))
    spend = total_spend(predictions)

    assert spend.usd == 0.0
    assert spend.known is True
    assert spend.unpriced_models == ()
    assert spend.cases_unpriced == 0


def test_no_cost_fallback_survives_anywhere_in_the_harness():
    """The emitter itself, which every other test here is blind to.

    Found by running the sabotage: reverting `main`'s one line back to
    `sum(p.usd or 0.0 for p in runs[0])` left all fourteen other tests GREEN. They pin
    `total_spend`, and nothing pinned `main` to calling it — a correct helper wired to
    nothing, which is the shape this repo refuses everywhere else. `main` cannot be driven
    offline (it builds a live Catalog and calls a real model), so the property is asserted
    STATICALLY: the defect is a specific expression, and the module may not contain it.

    Named for what it is. A static check does not prove the emitter is correct; it proves
    this exact regression cannot be re-typed. `total_spend`'s behaviour is what the rest of
    the file pins, and the two together are what the sabotage has to get past.

    An AST walk, not a grep: this file and run_eval both QUOTE the legacy expression in
    prose, and a text search flags those. The walk sees code and is blind to docstrings.
    """
    offenders = _cost_zero_fallbacks(RUN_EVAL.read_text(encoding="utf-8"))
    assert not offenders, f"an unknown cost is being defaulted to zero: {offenders}"


def test_that_guard_can_actually_see_the_defect():
    """The guard's own vacuity check. A detector that never fires proves nothing.

    Both directions: it finds the real expression, and it does NOT fire on a legitimate
    zero default for something that is not money (a token count, a total, a retry budget).
    """
    assert _cost_zero_fallbacks("spend = sum(p.usd or 0.0 for p in runs[0])")
    assert _cost_zero_fallbacks("total = cost_usd or 0")
    assert not _cost_zero_fallbacks("at = ev.get('timestampMillis') or 0")
    assert not _cost_zero_fallbacks("n = (listed.get('total') or 0)")
    assert not _cost_zero_fallbacks("'''docstring quoting p.usd or 0.0 as an example'''")


def test_the_spend_type_is_frozen_so_a_receipt_cannot_be_edited_after_it_is_measured():
    spend = total_spend([priced(0.01)])
    assert isinstance(spend, Spend)

    import dataclasses

    with_unknown = dataclasses.replace(spend, usd=None)
    assert spend.usd == 0.01, "the original measurement was mutated"
    assert with_unknown.known is False
