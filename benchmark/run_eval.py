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
worse, the harness says WHICH: `extraction fidelity` reports the cases where the extracted
claim differs from the labeled one, by name and with the field-level difference. A single
aggregate accuracy number would blur a checker bug and a transcription bug into one, and
they have nothing to do with each other.

--------------------------------------------------------------------------------
Which extracted claim answers which labeled one (scorer v2)
--------------------------------------------------------------------------------

A case's prose can decompose into more than one claim, so before anything can be scored
something has to decide WHICH extracted claim the label is about. Scorer v1 decided it with
`next(a for a in report.audits if a.claim.target_urn == case.target_urn)` — the first audit
touching the URN — and that one expression carried five separable defects: a wrong-FAMILY
claim answered a question nobody asked, extraction ORDER decided which claim was scored, a
right column with a wrong type read as a faithful transcription, and everything the
selector did not pick was discarded unexamined, so a duplicate looked perfect and an
unintended extra was invisible.

v2 binds them ONE-TO-ONE on the canonical subject (benchmark/matching.py), and the
dispositions are chosen so that a decomposer failure can never wear a checker's clothes:

  exact            the labeled claim, transcribed. Scored, clean.
  partial-subject  right family, right entity, wrong assertion. Scored end-to-end — the
                   user did receive that verdict — with extraction_ok False and the field
                   diff reported. A right verdict about a wrong assertion is right by luck.
  wrong-family     scored NO_CLAIM. That claim's verdict is a true answer to a different
                   question, and reporting it as this case's answer is exactly the
                   laundering NO_CLAIM exists to refuse.
  missing          scored NO_CLAIM.

Extras and duplicates are counted and named, and they never move accuracy: they are a
different failure from a wrong verdict and they are reported in a different place.
Extraction fidelity is deliberately kept OUT of the verdict confusion matrix — a fifth
column for "the decomposer mangled it" would put a transcription bug and a checker bug in
one grid, which is the aggregation this harness exists to refuse.

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
from collections.abc import Callable, Sequence
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
from attest.report import ClaimAudit
from benchmark.cases import (
    CASES,
    DOCUMENTED,
    VERDICTS,
    Case,
    coverage,
    extraction_demands,
    extraction_risk,
)
from benchmark.matching import (
    EXPECTED_CLAIMS_PER_CASE,
    Fidelity,
    fingerprint,
    match,
)

# The scorer's own version, carried in every full-pipeline receipt. Before v2 a case was
# scored against the FIRST audit touching its URN, which bound wrong-family claims, lost to
# extraction order, and discarded every unmatched claim unexamined. Numbers produced by the
# two scorers are NOT directly comparable, and a receipt that does not say which one made it
# invites exactly that comparison.
SCORER_VERSION = 2
MATCHING_POLICY = "canonical-subject-one-to-one-v1"

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
    # Full mode only. None in core mode, where nothing is extracted and fidelity is not a
    # question that exists — distinct from "extraction was perfect", which is what a default
    # of EXACT would have quietly asserted about a run that never called a model.
    fidelity: str | None = None
    subject_diff: tuple[str, ...] = ()
    # Claims the decomposer produced that no label asked for, and ones it said twice. Both
    # are extraction-fidelity failures and NEITHER moves the verdict: this case's verdict is
    # whatever the bound audit said, and burying an extra inside the accuracy number would
    # make two different defects share one figure.
    extras: int = 0
    duplicates: int = 0
    # The whole extracted subject multiset, for pass@k. See matching.fingerprint.
    extracted_subjects: tuple[str, ...] = ()
    # The models this case's run could not price, carried out of `report.cost` so the
    # aggregate can name WHICH model made a total unknown. Empty in core mode, which calls
    # no model at all — and empty is not the same as `usd is None`: see `total_spend`.
    unpriced_models: tuple[str, ...] = ()

    @property
    def correct(self) -> bool:
        return self.predicted == self.expected

    @property
    def clean_extraction(self) -> bool:
        """EXACT, and nothing left over. The only unqualified extraction success."""
        return (
            self.fidelity == Fidelity.EXACT.value
            and not self.extras
            and not self.duplicates
        )


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

    # --- extraction fidelity (full mode only) --------------------------------
    # Kept OUT of the verdict-label confusion matrix on purpose. The matrix answers "which
    # verdict did Attest reach", and a fifth column for "the decomposer mangled it" would
    # make a transcription bug and a checker bug share one grid — the aggregation this
    # harness exists to refuse. These are a separate report about a separate failure.
    fidelity: dict[str, int] = field(default_factory=dict)
    extra_claims: int = 0
    duplicate_claims: int = 0
    clean_extractions: int = 0
    # Cases whose subject was right in family and target and wrong in what it asserts, with
    # the field-level difference. A count alone does not say what moved.
    partial_subjects: list[dict[str, Any]] = field(default_factory=list)
    # Cases where something WAS extracted about the entity and it answered a different
    # question. Scored No-Claim, never with the unrelated claim's verdict.
    wrong_family: list[str] = field(default_factory=list)
    # The end-to-end failure counts above stay end-to-end: the user got the wrong outcome
    # however it happened. These name the subset the decomposer caused, so the cause stays
    # visible without the headline number being softened.
    correctness_failures_from_extraction: int = 0
    coverage_failures_from_extraction: int = 0

    @property
    def scored_extraction(self) -> bool:
        """Did this run measure extraction at all? False for the deterministic core."""
        return bool(self.fidelity)


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
    # The same two failures, restricted to cases the decomposer mis-transcribed. A SUBSET,
    # never a deduction: a user handed a wrong answer got a wrong answer, and moving those
    # out of the headline count would let a broken decomposer improve the number that
    # benchmark/README.md calls the worst thing this product can do.
    correctness_from_extraction = 0
    coverage_from_extraction = 0
    for p in predictions:
        if p.correct or p.predicted == NO_CLAIM:
            continue
        pair = {p.expected, p.predicted}
        # Supported vs Contradicted: the catalog was affirmed where it denies, or denied
        # where it affirms. Nothing else in this system is worse.
        if pair == {"Supported", "Contradicted"}:
            correctness += 1
            correctness_from_extraction += not p.extraction_ok
        else:
            coverage_ += 1
            coverage_from_extraction += not p.extraction_ok

    measured = [p for p in predictions if p.fidelity is not None]
    fidelity = {
        outcome.value: sum(1 for p in measured if p.fidelity == outcome.value)
        for outcome in Fidelity
        if any(p.fidelity == outcome.value for p in measured)
    }

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
                **({"fidelity": p.fidelity} if p.fidelity is not None else {}),
            }
            for p in predictions
            if not p.correct
        ],
        fidelity=fidelity,
        extra_claims=sum(p.extras for p in measured),
        duplicate_claims=sum(p.duplicates for p in measured),
        clean_extractions=sum(1 for p in measured if p.clean_extraction),
        partial_subjects=[
            {"case": p.case_id, "diff": list(p.subject_diff)}
            for p in measured
            if p.fidelity == Fidelity.PARTIAL.value
        ],
        wrong_family=[
            p.case_id for p in measured if p.fidelity == Fidelity.WRONG_FAMILY.value
        ],
        correctness_failures_from_extraction=correctness_from_extraction,
        coverage_failures_from_extraction=coverage_from_extraction,
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

    The snapshot source is INJECTABLE (Session 8). `just bench` leaves it None and reads the
    live catalog, as the measured numbers were taken. The offline test tier injects a
    fixture-backed loader, so the vacuity check — the one guarantee that must fire in CI
    without a server — runs against captured snapshots instead of skipping. The injection
    keeps `benchmark/` decoupled from `tests/`: the loader is passed in, never imported here.
    """

    def __init__(
        self, snapshot_source: Callable[[str], DatasetSnapshot] | None = None
    ) -> None:
        # None when the snapshots are injected. It used to be UNSET, so `run_full` died with
        # an AttributeError on any fixture-backed catalog — which is why nothing offline had
        # ever executed run_full, and why five defects lived in one line of it unexamined.
        self.client: DataHubClient | None = None
        if snapshot_source is None:
            self.client = DataHubClient()
            self.cache = SnapshotCache(self.client)
            snapshot_source = self.cache.fetch_dataset
        self._snapshot = snapshot_source
        reference = self._snapshot(DOCUMENTED).last_modified
        assert reference is not None, "the reference dataset must carry a timestamp"
        self.now: datetime = reference + timedelta(hours=1)

    def snapshot(self, urn: str) -> DatasetSnapshot:
        return self._snapshot(urn)


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


def predict_for_case(
    case: Case,
    audits: Sequence[ClaimAudit],
    *,
    usd: float | None = 0.0,
    latency_ms: float = 0.0,
    unpriced_models: tuple[str, ...] = (),
) -> Prediction:
    """Score ONE case against everything the pipeline extracted from its prose.

    Pure: no pipeline, no catalog, no network. That is deliberate — this is the logic that
    carried five defects for as long as it was a single expression buried inside `run_full`,
    where no offline test could reach it.

    The binding is benchmark.matching's, one-to-one over the canonical subject, so:

      * an exact match wins however late it was extracted, and however plausible the claim
        extracted before it looked;
      * a claim of the WRONG FAMILY about the right entity scores NO_CLAIM. Its verdict is a
        true answer to a question this case did not ask, and reporting it as this case's
        answer is the laundering NO_CLAIM exists to refuse;
      * a PARTIAL match is still scored — there IS a verdict about this subject and the user
        received it — but `extraction_ok` is False and the field-level difference is carried
        out, because a verdict that is right about the wrong assertion is right by luck;
      * every claim left over is counted, as an extra or as a duplicate. None of them moves
        the verdict; all of them are extraction-fidelity failures.
    """
    expected_claim = build_claim(case)
    extracted = [a.claim for a in audits]
    matching = match([expected_claim], extracted)
    # The benchmark contract: one labeled claim per case. Asserted rather than assumed, so
    # that a future multi-claim case is a loud failure here instead of a silent reliance on
    # the partial tie-break nobody decided to depend on.
    assert len(matching.pairs) == EXPECTED_CLAIMS_PER_CASE
    pair = matching.pairs[0]
    fidelity = Fidelity(pair.fidelity)

    audit = audits[pair.extracted_index] if pair.extracted_index is not None else None

    return Prediction(
        case_id=case.id,
        expected=case.expected_verdict,
        # NO_CLAIM for MISSING and for WRONG_FAMILY alike: in neither case did anything
        # answer the question the label asks.
        predicted=audit.verdict.value if (audit and fidelity.scorable) else NO_CLAIM,
        # Carried even when it is not scorable — it is what the model actually produced
        # about this entity, and it is the diagnosis.
        extracted=audit.claim if audit else None,
        extraction_ok=fidelity is Fidelity.EXACT,
        explanation_from_model=(audit.explanation.source == "model") if audit else None,
        guard_rejections=len(audit.explanation.rejected) if audit else 0,
        usd=usd,
        latency_ms=latency_ms,
        fidelity=fidelity.value,
        subject_diff=pair.diff,
        extras=len(matching.extras),
        duplicates=len(matching.duplicates),
        extracted_subjects=fingerprint(extracted),
        unpriced_models=unpriced_models,
    )


def run_full(catalog: Catalog, pipeline: Pipeline | None = None) -> list[Prediction]:
    """The whole pipeline, on the agent's prose. Real model, real money.

    `max_retries=0` turns the self-correction loop OFF. The benchmark measures the verdict
    Attest reaches about what the agent SAID, and a correction changes what the agent says
    — it never changes the verdict on the original claim (report.py). Leaving the loop on
    would triple the cost of the run and move no number in this report.

    The pipeline is INJECTABLE for the same reason the snapshot source is (Session 8): the
    scoring path has to be reachable by a test that has neither a catalog nor a key. `just
    bench-full` leaves it None and builds the real one, as every committed number was taken.
    """
    if pipeline is None:
        pipeline = Pipeline(client=catalog.client, now=catalog.now, max_retries=0)
    predictions = []

    for case in CASES:
        report = pipeline.run(case.agent_text)
        pipeline.forget(report.thread_id)
        predictions.append(
            predict_for_case(
                case,
                report.audits,
                usd=report.cost.usd,
                latency_ms=report.latency_ms,
                # Carried, not dropped. `usd is None` says the total is unknown; only these
                # say WHY, and `Trace.cost` finds them by name off the step — so a receipt
                # that loses them can report an unknown cost it cannot explain.
                unpriced_models=report.cost.unpriced_models,
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

        # The WHOLE extracted subject multiset, not just the claim the scorer bound. Keyed
        # on the bound claim alone, a run that additionally hallucinated a second claim
        # looked identical to one that did not — so extraction variance in the extras was
        # invisible to the one check whose job is to notice a verdict moving for no reason.
        claims = {p.extracted_subjects for p in attempts}
        entry = {
            "case": case_id,
            "verdicts": sorted(verdicts),
            "distinct_extraction_sets": len(claims),
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

    if m.correctness_failures_from_extraction or m.coverage_failures_from_extraction:
        # Named, never deducted. The counts above stay end-to-end because the user received
        # a wrong answer either way; this says how many of them the decomposer caused.
        print(
            f"    of which the decomposer caused: "
            f"{m.correctness_failures_from_extraction} correctness, "
            f"{m.coverage_failures_from_extraction} coverage"
        )

    if m.mis_extracted_but_right:
        # These are the dangerous ones. The verdict is correct and the claim underneath it
        # is not the claim the sentence made, so the report is right by luck. Counting them
        # as successes and never printing them is how a benchmark flatters a broken
        # decomposer.
        print("\n  RIGHT VERDICT, WRONG CLAIM — correct by luck, and worth fixing:")
        for case_id in m.mis_extracted_but_right:
            print(f"    {case_id}")


def print_extraction_fidelity(m: Metrics) -> None:
    """What the decomposer produced, held against what the labels asked for.

    Reported apart from the confusion matrix on purpose. The matrix answers "which verdict
    did Attest reach"; this answers "was it a verdict about the right claim". Folding a
    mangled transcription into the matrix as a fifth column would put a decomposer bug and a
    checker bug in one grid and make them look like one number, which is the aggregation
    this harness exists to refuse.
    """
    if not m.scored_extraction:
        return

    print(f"\n{'=' * 78}\nEXTRACTION FIDELITY — was the verdict about the RIGHT claim?\n{'=' * 78}")
    print(f"  clean extractions (exact, nothing left over)   {m.clean_extractions}/{m.n}")
    for outcome, count in m.fidelity.items():
        print(f"    {outcome:20s} {count:3d}")
    print(f"  unmatched extra claims    {m.extra_claims}")
    print(f"  duplicate claims          {m.duplicate_claims}")

    if m.partial_subjects:
        print("\n  PARTIAL SUBJECTS — right family, right entity, wrong assertion:")
        for entry in m.partial_subjects:
            print(f"    {entry['case']:12s} {'; '.join(entry['diff']) or '(no field diff)'}")
    if m.wrong_family:
        print("\n  WRONG FAMILY — something was extracted about the entity, but it answers")
        print("  a different question. Scored No-Claim, never with that claim's verdict:")
        for case_id in m.wrong_family:
            print(f"    {case_id}")
    if not m.partial_subjects and not m.wrong_family and not m.extra_claims:
        print("\n  Every case matched its label exactly, with nothing left over.")


def print_extraction_risk(predictions: list[Prediction]) -> dict[str, Any]:
    """Where the LLM risk actually lives, and where it does not.

    The honest framing, because 100% end-to-end invites the flattering read. **Not one of
    the 40 cases bypasses the model in this mode.** All 40 go through decomposition, so the
    score is a statement about the model transcribing 40 sentences AND the checkers deciding
    40 claims — two things, measured separately.

    And the deterministic core does NOT rescue a mis-transcription. It cannot: handed the
    wrong claim it will faithfully decide the wrong claim. What it guarantees is that the
    VERDICT is not invented — and what happens to a bad claim is that it becomes a visible
    gap rather than a confident wrong answer. That is not a theory: when the decomposer
    floored "every 30 minutes" to `max_age_hours=0`, the claim schema rejected it and the
    case scored No-Claim. A gap in the audit, surfaced. Not a verdict.
    """
    risk = extraction_risk()
    subtle = {c.id for c in CASES if extraction_demands(c)}

    print()
    print("=" * 78)
    print("WHERE THE MODEL RISK LIVES (all 40 cases pass through it; none bypass it)")
    print("=" * 78)
    print(f"  cases with a non-trivial demand on the decomposer   {len(subtle)}/{len(CASES)}")
    plain = len(CASES) - len(subtle)
    print(f"  cases needing only the claim type and the URN       {plain}/{len(CASES)}")
    print("\n  what the decomposer has to get right, by kind:")
    for demand, ids in sorted(risk.items(), key=lambda kv: -len(kv[1])):
        hit = [p for p in predictions if p.case_id in set(ids) and not p.extraction_ok]
        note = "" if not hit else f"   <-- MIS-EXTRACTED: {', '.join(p.case_id for p in hit)}"
        print(f"    {demand:20s} {len(ids):2d} cases{note}")

    print(
        "\n  The checkers do NOT rescue a mis-transcription -- handed the wrong claim they"
        "\n  faithfully decide the wrong claim. What they guarantee is that the VERDICT is"
        "\n  never invented. A claim the decomposer mangles becomes a visible GAP (No-Claim,"
        "\n  or a named extraction failure), not a confident wrong answer."
    )
    return {
        "cases_with_subtle_extraction_demand": len(subtle),
        "cases_type_and_urn_only": len(CASES) - len(subtle),
        "by_demand": {k: len(v) for k, v in risk.items()},
        "cases_bypassing_the_model": 0,
    }


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


@dataclass(frozen=True)
class Spend:
    """What a full-pipeline run cost, or an explicit unknown. Never a silent zero.

    Why this is not `attest.cost.Cost`, which carries these exact semantics: `Cost` is a
    TOKEN-and-dollars receipt, and a Prediction carries no token counts. Building one here
    would fix `input_tokens`/`output_tokens` at zero for a run that really spent hundreds
    of thousands — so `Cost.display()` would announce "0 tokens" about a paid run, and
    `total_tokens` would be a fabricated zero sitting in the type whose whole job is to
    refuse fabricated zeroes. It also has nowhere to put `cases_unpriced`. So the SEMANTICS
    are `Cost`'s, deliberately and to the letter (`usd is None` means unknown; the unpriced
    set is sorted and deduplicated; unknown poisons the total), and the container is local
    rather than bending a product type to a shape it does not fit. `attest/cost.py` is not
    edited to force the reuse.

    There is deliberately NO priced subtotal. The priced cases of a partly-unpriced run do
    total to a real number, and that is exactly why it must not be emitted: a reader has no
    way to tell it from a complete one. cost.py states the rule -- "A partial total would be
    worse than no total: it reads like a complete one." `cases_unpriced` is a COUNT, which
    diagnoses the gap without being mistakable for dollars.
    """

    usd: float | None
    unpriced_models: tuple[str, ...] = ()
    cases_unpriced: int = 0

    @property
    def known(self) -> bool:
        return self.usd is not None

    def display(self) -> str:
        """For the console. An unknown cost never renders as a dollar figure."""
        if self.usd is None:
            named = ", ".join(self.unpriced_models) or "not recorded"
            plural = "" if self.cases_unpriced == 1 else "s"
            return (
                f"cost unknown ({self.cases_unpriced} unpriced case{plural}; "
                f"models: {named})"
            )
        return f"${self.usd:.4f}"

    def as_payload(self) -> dict[str, Any]:
        """The receipt block. `cost_usd` keeps its v1 key, type and value when priced."""
        return {
            "cost_usd": self.usd,
            "cost_known": self.known,
            "unpriced_models": list(self.unpriced_models),
            "cases_unpriced": self.cases_unpriced,
        }


def total_spend(predictions: list[Prediction]) -> Spend:
    """Total a run's cost, or report that it cannot be totalled.

    v1 was `sum(p.usd or 0.0 for p in runs[0])`. A case whose run used a model with no price
    reports `usd=None` -- honestly, all the way up from cost.py -- and `or 0.0` turned that
    into a numeric zero, so the receipt carried a complete-looking total that silently
    omitted every unpriced call and printed it as `$0.0000`. That is the lie cost.py exists
    to refuse, told by the harness that measures the system built to catch it.

    Two rules do the work, and they pull against each other on purpose:

      * unknown is keyed on `p.usd is None`, NOT on whether any model names survived. Keying
        it on the names would rebuild Session 5's bug one level up, where an unpriced set
        lost on the way to disk let a run compute a total the original refused to state.
      * a MEASURED zero stays a known zero. Core mode calls no model and spends exactly
        nothing; the scripted fake spends no tokens and `Trace.cost` calls that known. A fix
        that flagged every zero as unknown would be the same category error pointing the
        other way.

    The sum contains no `or 0.0` in any form: the narrowing comes from an early return, so
    the defect cannot creep back in as a type-checker convenience.
    """
    unpriced_models = tuple(sorted({m for p in predictions for m in p.unpriced_models}))
    cases_unpriced = sum(1 for p in predictions if p.usd is None)

    if cases_unpriced:
        return Spend(
            usd=None, unpriced_models=unpriced_models, cases_unpriced=cases_unpriced
        )

    # Every case is priced — the early return above is what proves it, and that proof is
    # what lets this add straight up with nothing to fall back on. If a None ever reached
    # here it would raise, which is the correct failure: loud beats a silent zero.
    usd = 0.0
    for p in predictions:
        usd += p.usd

    return Spend(usd=usd, unpriced_models=unpriced_models, cases_unpriced=0)


def provenance() -> dict[str, Any]:
    """Which scorer produced this receipt, and from which tree.

    A methodology change that is not written into the artifact invites the one comparison
    it invalidates. Scorer v1 bound the FIRST audit touching a case's URN; v2 binds on the
    canonical subject, one-to-one. Their numbers are not directly comparable, and a reader
    holding two receipts has no other way to know that.

    `dirty` is reported rather than hidden: a receipt generated from an uncommitted tree is
    not reproducible from `commit` alone, and saying so costs nothing while discovering it
    later costs the receipt's credibility.
    """
    import subprocess

    commit: str | None = None
    dirty: bool | None = None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        pass  # Not a git checkout, or no git. The receipt says so by carrying null.

    return {
        "scorer_version": SCORER_VERSION,
        "matching_policy": MATCHING_POLICY,
        "scorer_commit": commit,
        "scorer_tree_dirty": dirty,
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
        # Full mode only, both of them. The extraction block is meaningless for a run that
        # never called a model, and `core.json` / `core-sabotaged-*.json` are pinned
        # byte-identical by the suite — adding a key there would move a committed receipt
        # for a run whose numbers did not change at all.
        payload |= provenance()
        payload["extraction_fidelity"] = {
            "clean_extractions": metrics.clean_extractions,
            "by_outcome": metrics.fidelity,
            "extra_claims": metrics.extra_claims,
            "duplicate_claims": metrics.duplicate_claims,
            "partial_subjects": metrics.partial_subjects,
            "wrong_family": metrics.wrong_family,
            "correctness_failures_from_extraction": (
                metrics.correctness_failures_from_extraction
            ),
            "coverage_failures_from_extraction": metrics.coverage_failures_from_extraction,
        }
        print_extraction_fidelity(metrics)
        payload["extraction_risk"] = print_extraction_risk(runs[0])
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

        # `cost_usd` keeps its v1 key, type and value on the priced path, so a fully priced
        # receipt is unmoved and old ones stay comparable. `cost_known` is what an unpriced
        # run needs and v1 could not say. Receipts written before this block carry a numeric
        # `cost_usd` and NO `cost_known`: read that as "not asserted", never as true — v1
        # emitted a number in the unpriced case too, so the old field cannot distinguish
        # them. Historical receipts are left exactly as they were measured.
        spend = total_spend(runs[0])
        payload |= spend.as_payload()
        payload["latency_ms"] = sum(p.latency_ms for p in runs[0])
        print(f"\n  measured spend for this run: {spend.display()} over {len(CASES)} claims")

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
                    f"{u['distinct_extraction_sets']} distinct extraction sets"
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
