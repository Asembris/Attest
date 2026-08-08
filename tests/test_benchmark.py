"""The benchmark, held to its own rules — and the metrics, held to theirs.

Two different things are under test here and they fail for different reasons.

**The DATASET** (benchmark/cases.py) must stay well-formed: unique ids, all 12 cells live,
every URN quoted verbatim in its prose, every label carrying a one-line rationale. And it
must stay TRUE: every label is re-derived against the live catalog, so a case whose label
drifts away from what the catalog actually says fails by name. A benchmark whose ground
truth has quietly gone stale is worse than no benchmark, because every number computed
against it still looks fine.

**The METRICS** (benchmark/run_eval.py) must be able to REPORT a failure. This is the
Session 5 lesson applied to the one place it matters most: a benchmark that cannot fail is a
green light wired to nothing. So the scoring is fed a deliberately wrong set of predictions
and asserted to notice — precision must collapse, the confusion matrix must fill in
off-diagonal, and a Supported/Contradicted swap must be counted as a CORRECTNESS failure
rather than lumped in with a coverage one. If these tests pass against a scorer that always
returns 1.0, the harness is decorative.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from benchmark.cases import CASES, DOCUMENTED, VERDICTS, coverage, validate
from benchmark.run_eval import NO_CLAIM, Prediction, build_claim, score

from _snapshots import load_snapshot
from attest.checkers import check

# --- the dataset -------------------------------------------------------------


def test_the_dataset_is_well_formed():
    """Unique ids, all 12 cells live, every URN quoted, every label justified in one line."""
    validate()
    assert len(CASES) >= 30, "a benchmark this small is an anecdote"
    assert all(n > 0 for n in coverage().values())


def test_the_benchmark_carries_the_hard_cases_it_exists_for():
    """The cases a plausible-but-wrong system gets wrong. Named, so they cannot be cut.

    A benchmark made of easy cases reports a high number and means nothing. These five are
    the ones the whole design turns on, and each is the reason a specific rule exists.
    """
    tagged = {tag for case in CASES for tag in case.tags}
    for required in (
        "the-trap",  # recipient_email_hash: reads as PII, tagged NonPII
        "column-propagates-up",  # audit_log: a PII column settles a table claim
        "customer-identifier-is-not-pii",  # a term outside the PII node implies nothing
        "the-central-case",  # an untagged table cannot SUPPORT "PII-free"
        "unrevisable",  # the ssn claim that can only be stood by
        "boundary",  # freshness is arithmetic, not a label on a dataset
    ):
        assert required in tagged, f"the benchmark lost its {required!r} case"

    # And the same dataset must appear with OPPOSITE freshness labels, or the checker could
    # be passing by having memorized which table is "the stale one".
    opposite = [c for c in CASES if "same-dataset-opposite-verdicts" in c.tags]
    assert len({c.expected_verdict for c in opposite}) == 2


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_every_label_is_what_the_live_catalog_actually_says(case, snapshot, now):
    """Ground truth, re-derived. The labels are hand-written; this is what keeps them true.

    If a checker changes, or the seed changes, or a label was simply wrong, this fails by
    name and says which. Note what it does NOT do: it cannot prove the POLICY is right — the
    checker and the label would agree on a wrong rule. That is what the cross-family labeler
    (benchmark/labeler.py) is for, and even that has limits it states out loud.
    """
    result = check(build_claim(case), snapshot(case.target_urn), now=now)
    assert result.verdict.value == case.expected_verdict, (
        f"{case.id}: the label says {case.expected_verdict} and the catalog says "
        f"{result.verdict.value}.\n  rationale: {case.rationale}\n  checker: {result.reason}"
    )


def test_breaking_a_checker_collapses_the_benchmark(snapshot, now, monkeypatch):
    """THE VACUITY CHECK, END TO END, AND WIRED INTO `just check`.

    `just bench-sabotage` exists and exits non-zero if the numbers do not move — but a
    guarantee that only fires when someone remembers to run it is the same shape of problem
    as the `just live` cadence rule, and it will rot. So the sabotage runs in the SUITE,
    against the live catalog, on every `just check` and in CI.

    Replace the classification checker with one that affirms everything — the exact failure
    this product exists to prevent, confident affirmation of an unverified claim — and the
    benchmark must NOTICE. If this ever passes with the numbers unmoved, every 100% in
    benchmark/README.md is a green light wired to nothing.
    """
    from benchmark import run_eval

    from attest import checkers, graph
    from attest.claims import ClaimType

    # Fixture-backed, so the vacuity check runs in the OFFLINE tier and gates CI — this is
    # the specific catalog-dependence Session 8 removed. Without the injected loader, Catalog
    # builds a live client and this test silently skips exactly where it must not: in a
    # CI-without-DataHub run, leaving the benchmark's own can-it-fail guarantee unproven.
    catalog = run_eval.Catalog(snapshot_source=load_snapshot)
    baseline = score(run_eval.run_core(catalog))
    assert baseline.accuracy == 1.0, "the benchmark must be green BEFORE it is broken"

    # Record the healthy dispatch BEFORE breaking it, so monkeypatch restores it at
    # teardown. sabotage() rebinds module-level tables on purpose — that is what lets it
    # reach both the core and the graph — and leaving them broken would mean every test
    # after this one ran against a checker that affirms everything. Which is a very funny
    # way to make a suite pass.
    node = graph.CHECKER[ClaimType.CLASSIFICATION]
    monkeypatch.setattr(checkers, "check", checkers.check)
    monkeypatch.setitem(graph._CHECK, node, graph._CHECK[node])

    run_eval.sabotage("classification")
    broken = score(run_eval.run_core(catalog))

    assert broken.accuracy < 0.75, (
        f"a checker that affirms EVERYTHING scored {broken.accuracy:.1%}. The benchmark "
        f"cannot fail, so none of its numbers mean anything."
    )
    assert broken.per_verdict["Supported"]["precision"] < 0.6
    assert broken.per_verdict["Contradicted"]["recall"] < 0.6
    assert broken.correctness_failures > 0, "affirming a contradicted claim is a CORRECTNESS bug"
    assert broken.coverage_failures > 0, "affirming a silent catalog is a COVERAGE bug"


# --- preservation: the deterministic core did not move --------------------------


def test_the_case_set_is_exactly_what_is_committed_in_cases_json():
    """The dataset is a citable artifact. A scorer change may not touch a single case.

    `cases.json` is version 1 and is cited on its own; the 12-cell counts are quoted in
    benchmark/README.md. Regenerating the generator's output here and comparing it to the
    committed file catches an edit to either half, in the session that made it.
    """
    import json

    from benchmark.cases import CASES_JSON, as_dict

    committed = json.loads(CASES_JSON.read_text(encoding="utf-8"))
    regenerated = json.loads(json.dumps(as_dict()))  # tuples -> lists, as the file has them

    assert committed == regenerated, "benchmark/cases.json is stale or a case was edited"
    assert committed["n_cases"] == 40
    assert len(CASES) == 40
    assert committed["coverage"] == coverage()
    assert all(n > 0 for n in coverage().values()), "a dark cell is a silent misclassifier"


def test_the_deterministic_core_still_scores_exactly_what_its_receipt_says(snapshot):
    """`run_core` must be untouched by the matching rewrite, and this proves it by receipt.

    The core feeds each case's structured claim straight to the checkers: no decomposer, no
    extraction, nothing for a matcher to do. So scorer v2 must reproduce `results/core.json`
    field for field — and if it ever does not, the change reached a path it had no business
    reaching, whatever else the suite says.
    """
    import json

    from benchmark import run_eval

    committed = json.loads(
        (run_eval.RESULTS_DIR / "core.json").read_text(encoding="utf-8")
    )["metrics"]
    m = score(run_eval.run_core(run_eval.Catalog(snapshot_source=load_snapshot)))

    assert m.accuracy == committed["accuracy"]
    assert m.macro_f1 == committed["macro_f1"]
    assert m.per_verdict == committed["per_verdict"]
    assert m.matrix == committed["confusion_matrix"]
    assert m.labels == committed["labels"]
    assert m.correctness_failures == committed["correctness_failures"]
    assert m.coverage_failures == committed["coverage_failures"]
    assert m.extraction_failures == committed["extraction_failures"]
    assert m.mis_extracted_but_right == committed["mis_extracted_but_right"]
    assert m.errors == committed["errors"]


def test_the_sabotage_receipt_matches_what_the_sabotaged_core_scores(snapshot, monkeypatch):
    """The vacuity check's RECEIPT, held to the run that produced it — field for field.

    `core.json` has had this pin since the scorer rewrite; the sabotage receipt never did,
    and it drifted in exactly the way an unpinned artifact does. It was written earlier in
    its own commit than the emitter line that adds `mis_extracted_but_right`, and because
    nothing compared it to a live run, a receipt missing a field its own emitter produces
    sat in the repo for twenty-odd sessions — inviting the reader to infer a scorer
    difference between the two core receipts where there is none.

    It also makes the sabotage un-fakeable from the healthy checker: a receipt generated
    without the sabotage scores 1.0, and every assertion below demands 0.675.

    Fixture-backed and monkeypatch-restored for the same reasons as the vacuity check
    above: it must run offline in CI, and it must not leave an affirm-everything checker
    behind for every test after it.
    """
    import json

    from benchmark import run_eval

    from attest import checkers, graph
    from attest.claims import ClaimType

    payload = json.loads(
        (run_eval.RESULTS_DIR / "core-sabotaged-classification.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["mode"] == "core"
    assert payload["sabotaged"] == "classification"
    assert payload["k"] == 1
    # k=1, so there is nothing for pass@k to compare. An unasked question reports nothing.
    assert "consistency" not in payload

    node = graph.CHECKER[ClaimType.CLASSIFICATION]
    monkeypatch.setattr(checkers, "check", checkers.check)
    monkeypatch.setitem(graph._CHECK, node, graph._CHECK[node])

    run_eval.sabotage("classification")
    m = score(run_eval.run_core(run_eval.Catalog(snapshot_source=load_snapshot)))

    committed = payload["metrics"]
    assert m.accuracy == committed["accuracy"]
    assert m.macro_f1 == committed["macro_f1"]
    assert m.per_verdict == committed["per_verdict"]
    assert m.matrix == committed["confusion_matrix"]
    assert m.labels == committed["labels"]
    assert m.correctness_failures == committed["correctness_failures"]
    assert m.coverage_failures == committed["coverage_failures"]
    assert m.extraction_failures == committed["extraction_failures"]
    assert m.mis_extracted_but_right == committed["mis_extracted_but_right"]
    assert m.errors == committed["errors"]


def test_the_core_receipt_carries_no_full_mode_fields():
    """The extraction block and the scorer provenance are FULL MODE ONLY, on purpose.

    A core run measures no extraction, so an extraction block there would report zeros about
    a question that was never asked. Keeping them out is also what leaves `core.json` and
    `core-sabotaged-classification.json` byte-identical across the v1 -> v2 change: their
    numbers did not move, so their files must not either.
    """
    import json

    from benchmark import run_eval

    for name in ("core.json", "core-sabotaged-classification.json"):
        payload = json.loads((run_eval.RESULTS_DIR / name).read_text(encoding="utf-8"))
        assert "extraction_fidelity" not in payload, name
        assert "scorer_version" not in payload, name
        assert payload["mode"] == "core", name


def test_the_benchmark_now_is_the_catalogs_now_and_not_the_wall_clock(snapshot):
    """The freshness labels are relative to the seed, so a wall-clock `now` would rot them.

    Under a real clock the fresh datasets age, and in a fortnight the benchmark reports a
    regression against completely correct code — "the checker broke" when the truth is "the
    catalog is old". That is the confusion between data state and code state that Attest
    exists to prevent, and it has no business in Attest's own benchmark.
    """
    from benchmark.run_eval import Catalog

    catalog = Catalog(snapshot_source=load_snapshot)
    reference = snapshot(DOCUMENTED).last_modified
    assert catalog.now == reference + timedelta(hours=1)


# --- the metrics -------------------------------------------------------------


def predictions(pairs: list[tuple[str, str]]) -> list[Prediction]:
    return [
        Prediction(case_id=f"c{i}", expected=e, predicted=p)
        for i, (e, p) in enumerate(pairs)
    ]


def test_a_perfect_run_scores_perfectly():
    m = score(predictions([(v, v) for v in VERDICTS for _ in range(3)]))
    assert m.accuracy == 1.0
    assert m.macro_f1 == 1.0
    assert m.correctness_failures == 0 and m.coverage_failures == 0


def test_the_scorer_notices_a_system_that_affirms_everything():
    """THE VACUITY CHECK, at the unit level. Feed the metric a liar; it must say so.

    A scorer that returned 1.0 here would pass every other test in this file, and every
    number in benchmark/README.md would be a green light wired to nothing.
    """
    always_supported = [("Supported", "Supported")] * 5 + [
        ("Contradicted", "Supported")
    ] * 5 + [("Insufficient-Coverage", "Supported")] * 5

    m = score(predictions(always_supported))

    assert m.accuracy == pytest.approx(1 / 3)
    assert m.per_verdict["Supported"]["precision"] == pytest.approx(1 / 3)
    assert m.per_verdict["Supported"]["recall"] == 1.0, "it did affirm every Supported case"
    assert m.per_verdict["Contradicted"]["recall"] == 0.0
    assert m.per_verdict["Insufficient-Coverage"]["recall"] == 0.0
    assert m.macro_f1 < 0.35


def test_a_correctness_failure_is_not_counted_as_a_coverage_failure():
    """Supported <-> Contradicted is a different bug from anything <-> Insufficient.

    One means Attest affirmed what the catalog denies — the worst thing it can do. The other
    means it confused disagreement with silence. Collapsing them into one "errors" number is
    exactly the aggregation this harness exists to refuse.
    """
    m = score(
        predictions(
            [
                ("Supported", "Contradicted"),  # correctness
                ("Contradicted", "Supported"),  # correctness
                ("Contradicted", "Insufficient-Coverage"),  # coverage
                ("Insufficient-Coverage", "Supported"),  # coverage
            ]
        )
    )
    assert m.correctness_failures == 2
    assert m.coverage_failures == 2


def test_a_claim_that_was_never_extracted_is_not_scored_as_a_verdict():
    """No-Claim is a decomposition failure, not the catalog being silent.

    Scoring it Insufficient-Coverage would launder a broken extraction into a
    legitimate-looking "the catalog does not say" — the same laundering report.ClaimError
    exists to prevent, committed by the benchmark instead of by the pipeline.
    """
    m = score(predictions([("Contradicted", NO_CLAIM), ("Supported", "Supported")]))

    assert m.accuracy == 0.5
    assert NO_CLAIM not in m.per_verdict, "No-Claim is not a verdict and has no recall"
    # It is a miss, and it is neither kind of verdict confusion: nothing was verdicted.
    assert m.correctness_failures == 0
    assert m.coverage_failures == 0
    assert m.errors[0]["predicted"] == NO_CLAIM


def test_the_confusion_matrix_puts_the_errors_where_they_happened():
    m = score(predictions([("Supported", "Contradicted"), ("Supported", "Supported")]))
    s, c = m.labels.index("Supported"), m.labels.index("Contradicted")
    assert m.matrix[s][c] == 1
    assert m.matrix[s][s] == 1
