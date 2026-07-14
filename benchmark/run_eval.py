"""The evaluation harness: how well does Attest actually do, in numbers a judge can check.

    python -m benchmark.run_eval              # the deterministic core. Free, exact.
    python -m benchmark.run_eval --full       # the whole pipeline, real model. Costs ~2c.
    python -m benchmark.run_eval --full -k 3  # ...and pass@k consistency across 3 runs.
    python -m benchmark.run_eval --sabotage classification   # the vacuity check.

--------------------------------------------------------------------------------
Two modes, because "Attest got it wrong" is two different bugs
--------------------------------------------------------------------------------

**core** feeds each case's structured claim straight to the deterministic checkers. No
model is involved at any point. This is the accuracy of the thing that actually decides
verdicts, and it is free, exact, and reproducible.

**full** feeds the agent's PROSE to the whole pipeline: sanitize -> decompose -> check ->
explain -> guard. This is the accuracy a user experiences, and it can be worse — because
the decomposer may transcribe the sentence into a claim the human never meant. When it is
worse, the harness says WHICH: `extraction fidelity` counts the cases where the extracted
claim differs from the labeled one, and those cases are listed by name. A single aggregate
accuracy number would blur a checker bug and a transcription bug into one, and they have
nothing to do with each other.

--------------------------------------------------------------------------------
Per-verdict metrics, not aggregate accuracy
--------------------------------------------------------------------------------

Aggregate accuracy hides which verdict is weak, and the three verdicts are not
interchangeable. So: precision, recall and F1 PER VERDICT, plus a confusion matrix, plus a
reading of the confusion matrix that says what kind of failure each cell is —

  Supported <-> Contradicted            a CORRECTNESS failure. Attest affirmed something
                                        the catalog denies, or denied something it affirms.
                                        The worst thing this product can do.

  Contradicted <-> Insufficient-Coverage   a COVERAGE failure. Attest confused "the catalog
  Supported    <-> Insufficient-Coverage   disagrees" with "the catalog is silent". This is
                                        crying wolf on an under-documented table, or the
                                        reverse: certifying silence as a clean bill.

Different problems, different fixes, and the matrix is printed so you can see which is
happening rather than being told a single number.

--------------------------------------------------------------------------------
pass@k, and what a failure of it would MEAN
--------------------------------------------------------------------------------

A deterministic checker returns the same verdict every time, by construction. So pass@k on
the core is 100% or there is a bug, and it is a cheap, loud check that no model has leaked
into the verdict path — the property trajectory.py asserts structurally, measured here
empirically. The two should never disagree.

On the FULL pipeline pass@k can dip below 100% without any such leak, because decomposition
is a model call and a model at temperature=0 is *nearly*, not perfectly, deterministic. So
a full-mode instability is diagnosed rather than merely counted: if the extracted CLAIM was
identical across runs and the VERDICT moved, that is a model in the verdict path and the
harness says so in capitals. If the extracted claim moved, it is decomposition variance —
a real finding, but a different one.

--------------------------------------------------------------------------------
The vacuity check (--sabotage)
--------------------------------------------------------------------------------

A benchmark that cannot fail is a green light wired to nothing. Before trusting any number
here, break the thing it measures and confirm the number moves: `--sabotage classification`
replaces the classification checker with one that always returns Supported. Precision on
Contradicted and Insufficient-Coverage must collapse. If it does not, the metric is not
measuring what it claims to.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

from attest import checkers, graph
from attest.claims import Claim, ClaimType, Evidence, Verdict
from attest.datahub import DataHubClient, DatasetSnapshot, SnapshotCache
from attest.graph import Pipeline
from benchmark.cases import CASES, DOCUMENTED, VERDICTS, Case, coverage

RESULTS_DIR = Path(__file__).parent / "results"

# A 4th predicted label, and it is NOT a verdict. The pipeline produced no claim about the
# case's URN at all — a decomposition failure. Scoring it as Insufficient-Coverage would
# launder a broken extraction into a legitimate-looking "the catalog is silent", which is
# exactly the laundering report.ClaimError exists to prevent.
NO_CLAIM = "No-Claim"
LABELS = [*VERDICTS, NO_CLAIM]

_CLAIM_ADAPTER: TypeAdapter[Claim] = TypeAdapter(Claim)


def build_claim(case: Case) -> Claim:
    payload = {k: v for k, v in case.claim.items() if v is not None}
    return _CLAIM_ADAPTER.validate_python(payload)


@dataclass
class Prediction:
    """What Attest said about one case, on one run."""

    case_id: str
    expected: str
    predicted: str
    # Full mode only: what the decomposer actually extracted, and whether it matched.
    extracted: Claim | None = None
    extraction_ok: bool = True
    explanation_from_model: bool | None = None
    guard_rejections: int = 0
    usd: float | None = 0.0
    latency_ms: float = 0.0

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected


@dataclass
class Metrics:
    """Per-verdict precision/recall/F1, the confusion matrix, and what it means."""

    n: int
    accuracy: float
    per_verdict: dict[str, dict[str, float]]
    macro_f1: float
    matrix: list[list[int]]
    labels: list[str]
    correctness_failures: int
    coverage_failures: int
    extraction_failures: int
    errors: list[dict[str, str]] = field(default_factory=list)
    # Cases where the decomposer produced a DIFFERENT claim from the labeled one and the
    # verdict came out right anyway. Right by luck. Counted as correct — because the user
    # did get the right answer — and named, because a benchmark that silently banks these
    # is flattering a broken decomposer.
    mis_extracted_but_right: list[str] = field(default_factory=list)


def score(predictions: list[Prediction]) -> Metrics:
    """Precision/recall/F1 per verdict, and a confusion matrix read for what it means.

    sklearn, not a hand-rolled count: the arithmetic of a metric is exactly the place a
    quiet off-by-one produces a number that looks plausible and is wrong, and a benchmark's
    own arithmetic is the last thing that should be bespoke.
    """
    y_true = [p.expected for p in predictions]
    y_pred = [p.predicted for p in predictions]

    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=LABELS, zero_division=0
    )
    matrix = confusion_matrix(y_true, y_pred, labels=LABELS)

    per_verdict = {
        label: {
            "precision": float(precision[i]),
            "recall": float(recall[i]),
            "f1": float(f1[i]),
            "support": int(support[i]),
        }
        # NO_CLAIM has no support (no case is labeled it), so it is reported only as a
        # column of the matrix, never as a class with a recall.
        for i, label in enumerate(LABELS)
        if support[i] > 0
    }
    macro = statistics.fmean(v["f1"] for v in per_verdict.values())

    correctness = 0
    coverage_ = 0
    for p in predictions:
        if p.correct or p.predicted == NO_CLAIM:
            continue
        pair = {p.expected, p.predicted}
        # Supported vs Contradicted: the catalog was affirmed where it denies, or denied
        # where it affirms. Nothing else in this system is worse.
        if pair == {"Supported", "Contradicted"}:
            correctness += 1
        else:
            coverage_ += 1

    return Metrics(
        n=len(predictions),
        accuracy=sum(p.correct for p in predictions) / len(predictions),
        per_verdict=per_verdict,
        macro_f1=macro,
        matrix=matrix.tolist(),
        labels=LABELS,
        correctness_failures=correctness,
        coverage_failures=coverage_,
        extraction_failures=sum(1 for p in predictions if not p.extraction_ok),
        mis_extracted_but_right=[
            p.case_id for p in predictions if p.correct and not p.extraction_ok
        ],
        errors=[
            {
                "case": p.case_id,
                "expected": p.expected,
                "predicted": p.predicted,
                "extraction_ok": str(p.extraction_ok),
            }
            for p in predictions
            if not p.correct
        ],
    )


# --- the two modes -----------------------------------------------------------


class Catalog:
    """One read of the catalog for the whole evaluation, and one reference `now`.

    Every case is scored against the SAME snapshot of the catalog, for the same reason a
    single audit is (datahub/cache.py): a benchmark whose ground truth moves halfway through
    is not a benchmark. And `now` is reconstructed from the catalog rather than read off the
    wall clock — the seed writes relative timestamps, so a wall-clock `now` would make the
    freshness labels rot with age and report "the checker regressed" when the truth is "the
    catalog is old". Same argument as tests/conftest.py.
    """

    def __init__(self) -> None:
        self.client = DataHubClient()
        self.cache = SnapshotCache(self.client)
        reference = self.cache.fetch_dataset(DOCUMENTED).last_modified
        assert reference is not None, "the reference dataset must carry a timestamp"
        self.now: datetime = reference + timedelta(hours=1)

    def snapshot(self, urn: str) -> DatasetSnapshot:
        return self.cache.fetch_dataset(urn)


def run_core(catalog: Catalog) -> list[Prediction]:
    """The deterministic core, alone. No model is constructed, let alone called."""
    predictions = []
    for case in CASES:
        started = time.perf_counter()
        # Through the module attribute, not a name bound at import: --sabotage replaces
        # `checkers.check`, and a `from ... import check` would have kept a private
        # reference to the healthy one — a vacuity check that cannot reach what it breaks.
        result = checkers.check(
            build_claim(case), catalog.snapshot(case.target_urn), now=catalog.now
        )
        predictions.append(
            Prediction(
                case_id=case.id,
                expected=case.expected_verdict,
                predicted=result.verdict.value,
                latency_ms=(time.perf_counter() - started) * 1000,
                usd=0.0,
            )
        )
    return predictions


def _same_subject(extracted: Claim, expected: Claim) -> bool:
    """Did the decomposer transcribe the sentence into the claim the label is about?

    Not equality: `raw_text` is the model's quotation of the agent and will differ in
    whitespace and length without meaning anything. What must match is what the claim IS
    — its type, its target, and the thing it asserts.
    """
    if extracted.claim_type is not expected.claim_type:
        return False
    if extracted.target_urn != expected.target_urn:
        return False

    if isinstance(expected, type(extracted)) is False:
        return False

    match expected.claim_type:
        case ClaimType.FRESHNESS:
            return extracted.max_age_hours == expected.max_age_hours  # type: ignore[union-attr]
        case ClaimType.OWNERSHIP:
            return extracted.owner_urn == expected.owner_urn  # type: ignore[union-attr]
        case ClaimType.CLASSIFICATION:
            return (
                set(extracted.labels) == set(expected.labels)  # type: ignore[union-attr]
                and extracted.present == expected.present  # type: ignore[union-attr]
                and extracted.field_path == expected.field_path  # type: ignore[union-attr]
            )
        case ClaimType.SCHEMA:
            return {c.name for c in extracted.columns} == {  # type: ignore[union-attr]
                c.name for c in expected.columns  # type: ignore[union-attr]
            }
    return False


def run_full(catalog: Catalog) -> list[Prediction]:
    """The whole pipeline, on the agent's prose. Real model, real money.

    `max_retries=0` turns the self-correction loop OFF. The benchmark measures the verdict
    Attest reaches about what the agent SAID, and a correction changes what the agent says
    — it never changes the verdict on the original claim (report.py). Leaving the loop on
    would triple the cost of the run and move no number in this report.
    """
    pipeline = Pipeline(client=catalog.client, now=catalog.now, max_retries=0)
    predictions = []

    for case in CASES:
        expected_claim = build_claim(case)
        report = pipeline.run(case.agent_text)
        pipeline.forget(report.thread_id)

        audit = next(
            (a for a in report.audits if a.claim.target_urn == case.target_urn), None
        )
        if audit is None:
            # Nothing was extracted about this URN. Not a verdict — see NO_CLAIM.
            predictions.append(
                Prediction(
                    case_id=case.id,
                    expected=case.expected_verdict,
                    predicted=NO_CLAIM,
                    extraction_ok=False,
                    usd=report.cost.usd,
                    latency_ms=report.latency_ms,
                )
            )
            continue

        predictions.append(
            Prediction(
                case_id=case.id,
                expected=case.expected_verdict,
                predicted=audit.verdict.value,
                extracted=audit.claim,
                extraction_ok=_same_subject(audit.claim, expected_claim),
                explanation_from_model=audit.explanation.source == "model",
                guard_rejections=len(audit.explanation.rejected),
                usd=report.cost.usd,
                latency_ms=report.latency_ms,
            )
        )
    return predictions


# --- pass@k ------------------------------------------------------------------


@dataclass
class Consistency:
    """Did the same claim get the same verdict every time it was asked?"""

    k: int
    stable: int
    unstable: list[dict[str, Any]]
    verdict_path_leaks: list[dict[str, Any]]

    @property
    def rate(self) -> float:
        total = self.stable + len(self.unstable)
        return self.stable / total if total else 1.0


def pass_at_k(runs: list[list[Prediction]]) -> Consistency:
    """k runs of the same benchmark. A deterministic verdict does not move.

    The DIAGNOSIS is the point, not the count. An unstable verdict whose extracted claim
    was IDENTICAL across runs means the same question got two answers — which can only be a
    model in the verdict path, the one thing this system says cannot happen, and the one
    thing trajectory.py exists to make impossible. That is reported as a leak, in capitals,
    and it is a bug rather than a metric.

    An unstable verdict whose extracted claim MOVED is decomposition variance: the model
    transcribed the same sentence into two different claims, and the two got different (and
    possibly both correct) verdicts. A real finding, a different one, and not a leak.
    """
    stable = 0
    unstable: list[dict[str, Any]] = []
    leaks: list[dict[str, Any]] = []

    by_case: dict[str, list[Prediction]] = {}
    for run in runs:
        for p in run:
            by_case.setdefault(p.case_id, []).append(p)

    for case_id, attempts in by_case.items():
        verdicts = {p.predicted for p in attempts}
        if len(verdicts) == 1:
            stable += 1
            continue

        claims = {
            p.extracted.model_dump_json(exclude={"raw_text"}) if p.extracted else "none"
            for p in attempts
        }
        entry = {
            "case": case_id,
            "verdicts": sorted(verdicts),
            "distinct_claims_extracted": len(claims),
        }
        unstable.append(entry)
        if len(claims) == 1:
            leaks.append(entry)

    return Consistency(k=len(runs), stable=stable, unstable=unstable, verdict_path_leaks=leaks)


# --- the vacuity check -------------------------------------------------------


def sabotage(claim_type: str) -> None:
    """Break the thing a metric measures, and confirm the metric notices.

    A benchmark that cannot fail is a green light wired to nothing. This replaces one
    checker with one that returns Supported for everything — exactly the shape of the
    failure this product exists to prevent: confident affirmation of an unverified claim.
    Precision on Contradicted and Insufficient-Coverage must collapse.

    BOTH paths are broken, because they are different paths. `core` mode calls
    `checkers.check`; the pipeline dispatches through `graph._CHECK`, keyed by NODE (the
    router's own table). Sabotaging one and not the other would leave the other mode
    quietly reporting a healthy number and would make this check itself vacuous.
    """
    from attest.checkers.base import result

    healthy = checkers.check

    def always_supported(claim: Claim, snapshot: DatasetSnapshot, now: Any = None):
        if claim.claim_type.value == claim_type:
            return result(
                claim,
                Verdict.SUPPORTED,
                "SABOTAGED: this checker affirms everything.",
                Evidence(field="sabotage", value="always-supported"),
            )
        return healthy(claim, snapshot, now=now)

    checkers.check = always_supported  # type: ignore[assignment]

    node = graph.CHECKER[ClaimType(claim_type)]
    graph._CHECK[node] = always_supported


# --- reporting ---------------------------------------------------------------


def print_metrics(title: str, m: Metrics) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(f"  cases      {m.n}")
    print(f"  accuracy   {m.accuracy:.1%}")
    print(f"  macro F1   {m.macro_f1:.3f}")

    print("\n  per verdict (aggregate accuracy hides which one is weak)")
    print(f"    {'verdict':24s} {'prec':>6s} {'rec':>6s} {'F1':>6s} {'n':>4s}")
    for label, v in m.per_verdict.items():
        print(
            f"    {label:24s} {v['precision']:6.3f} {v['recall']:6.3f} "
            f"{v['f1']:6.3f} {int(v['support']):4d}"
        )

    print("\n  confusion matrix (rows = truth, cols = predicted)")
    header = "".join(f"{lab[:12]:>14s}" for lab in m.labels)
    print(f"    {'':24s}{header}")
    for i, label in enumerate(m.labels):
        if not any(m.matrix[i]):
            continue
        row = "".join(f"{n:>14d}" for n in m.matrix[i])
        print(f"    {label:24s}{row}")

    print(
        f"\n  correctness failures (Supported <-> Contradicted): {m.correctness_failures}"
    )
    print(f"  coverage failures    (anything <-> Insufficient): {m.coverage_failures}")
    if m.extraction_failures:
        print(f"  extraction failures  (decomposer, not checker): {m.extraction_failures}")

    if m.errors:
        print("\n  every case Attest got wrong, by name:")
        for e in m.errors:
            note = "" if e["extraction_ok"] == "True" else "  [extraction]"
            print(f"    {e['case']:12s} expected {e['expected']:22s} got {e['predicted']}{note}")

    if m.mis_extracted_but_right:
        # These are the dangerous ones. The verdict is correct and the claim underneath it
        # is not the claim the sentence made, so the report is right by luck. Counting them
        # as successes and never printing them is how a benchmark flatters a broken
        # decomposer.
        print("\n  RIGHT VERDICT, WRONG CLAIM — correct by luck, and worth fixing:")
        for case_id in m.mis_extracted_but_right:
            print(f"    {case_id}")


def semantic_report(predictions: list[Prediction]) -> dict[str, Any]:
    """The semantic layer's own numbers, across the whole benchmark rather than 13 cases."""
    explained = [p for p in predictions if p.explanation_from_model is not None]
    if not explained:
        return {}
    model_authored = sum(1 for p in explained if p.explanation_from_model)
    rejected_drafts = sum(p.guard_rejections for p in explained)
    return {
        "explanations": len(explained),
        "model_authored": model_authored,
        "model_authored_rate": model_authored / len(explained),
        "fell_back_to_template": len(explained) - model_authored,
        "guard_rejected_drafts": rejected_drafts,
        "guard_rejection_rate": rejected_drafts / len(explained),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true", help="run the whole pipeline (costs money)")
    ap.add_argument("-k", type=int, default=1, help="runs per case, for pass@k consistency")
    ap.add_argument("--sabotage", choices=["freshness", "ownership", "classification", "schema"])
    ap.add_argument("--out", type=Path, default=None, help="write the report as JSON")
    args = ap.parse_args()

    if args.sabotage:
        sabotage(args.sabotage)
        print(f"\n*** SABOTAGED: the {args.sabotage} checker now affirms everything. ***")
        print("*** Every number below must get WORSE. If it does not, the metric is ***")
        print("*** a green light wired to nothing.                                   ***")

    catalog = Catalog()
    mode = "full" if args.full else "core"
    print(f"\nAttest golden benchmark: {len(CASES)} cases, mode={mode}, k={args.k}")
    print(f"reference now (from the catalog, never the wall clock): {catalog.now.isoformat()}")

    runner = run_full if args.full else run_core
    runs = [runner(catalog) for _ in range(args.k)]

    metrics = score(runs[0])
    title = (
        "THE DETERMINISTIC CORE — no model is involved at any point"
        if mode == "core"
        else "THE WHOLE PIPELINE — prose in, verdict out, real model"
    )
    print_metrics(title, metrics)

    payload: dict[str, Any] = {
        "mode": mode,
        "k": args.k,
        "sabotaged": args.sabotage,
        "n_cases": len(CASES),
        "coverage": coverage(),
        "metrics": {
            "accuracy": metrics.accuracy,
            "macro_f1": metrics.macro_f1,
            "per_verdict": metrics.per_verdict,
            "confusion_matrix": metrics.matrix,
            "labels": metrics.labels,
            "correctness_failures": metrics.correctness_failures,
            "coverage_failures": metrics.coverage_failures,
            "extraction_failures": metrics.extraction_failures,
            "mis_extracted_but_right": metrics.mis_extracted_but_right,
            "errors": metrics.errors,
        },
    }

    if mode == "full":
        semantics = semantic_report(runs[0])
        payload["semantic_layer"] = semantics
        print(f"\n{'=' * 78}")
        print(f"THE SEMANTIC LAYER (across all {len(CASES)} cases, not 13)")
        print("=" * 78)
        print(
            f"  model-authored explanations   {semantics['model_authored']}/"
            f"{semantics['explanations']}  ({semantics['model_authored_rate']:.1%})"
        )
        print(f"  fell back to the template     {semantics['fell_back_to_template']}")
        print(
            f"  drafts the guard threw away   {semantics['guard_rejected_drafts']} "
            f"({semantics['guard_rejection_rate']:.2f} per explanation)"
        )
        print(
            "\n  Both numbers matter and they pull opposite ways: a guard that rejected"
            "\n  everything would score 0% model-authored and catch every hallucination."
        )

        spend = sum(p.usd or 0.0 for p in runs[0])
        payload["cost_usd"] = spend
        payload["latency_ms"] = sum(p.latency_ms for p in runs[0])
        print(f"\n  measured spend for this run: ${spend:.4f} over {len(CASES)} claims")

    if args.k > 1:
        consistency = pass_at_k(runs)
        payload["consistency"] = {
            "k": consistency.k,
            "pass_at_k": consistency.rate,
            "unstable": consistency.unstable,
            "verdict_path_leaks": consistency.verdict_path_leaks,
        }
        print(f"\n{'=' * 78}\nCONSISTENCY — pass@{args.k}\n{'=' * 78}")
        print(f"  stable verdicts   {consistency.stable}/{len(CASES)}  ({consistency.rate:.1%})")

        if consistency.verdict_path_leaks:
            print("\n  *** A MODEL IS IN THE VERDICT PATH. ***")
            print("  The SAME extracted claim produced DIFFERENT verdicts across runs. A")
            print("  deterministic checker cannot do that. This is a bug, not a metric, and")
            print("  trajectory.py's NO_LLM_IN_THE_VERDICT_PATH should have caught it:")
            for leak in consistency.verdict_path_leaks:
                print(f"    {leak['case']}: {leak['verdicts']}")
        elif consistency.unstable:
            print("\n  Unstable, but NOT a verdict-path leak: the decomposer extracted a")
            print("  different claim from the same sentence, and the two claims honestly")
            print("  deserve different verdicts. Decomposition variance, not a model deciding.")
            for u in consistency.unstable:
                print(
                    f"    {u['case']}: verdicts {u['verdicts']}, "
                    f"{u['distinct_claims_extracted']} distinct claims extracted"
                )
        else:
            print("  Every verdict identical across every run, as a deterministic core must be.")

    out = args.out or (
        RESULTS_DIR / f"{mode}{'-sabotaged-' + args.sabotage if args.sabotage else ''}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\n")
    print(f"\nwrote {out}")

    # A sabotaged run is SUPPOSED to fail. Exiting 0 on it would make the vacuity check
    # indistinguishable from a passing benchmark in CI.
    if args.sabotage:
        collapsed = metrics.accuracy < 0.9
        print(
            "\nVACUITY CHECK: "
            + (
                "PASSED — breaking the checker moved the numbers."
                if collapsed
                else "FAILED — the metric did not notice a checker that affirms everything."
            )
        )
        return 0 if collapsed else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
