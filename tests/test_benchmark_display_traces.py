"""Item 2 (Session 12): every benchmark number the UI displays traces to a committed receipt.

The UI headline advertised core pass@5 while `benchmark/results/core.json` was a k=1 receipt
with no pass@k in it — a displayed number backed by nothing. core.json was regenerated at k=5
(the deterministic core is stable, so pass@5 is 100% by construction); this test makes the
mismatch unreintroducible by pinning the frontend's headline, per-verdict table and confusion
matrix to `core.json` / `full.json`. If the UI says pass@5 while the receipt says k=1 again, or
any metric drifts from its receipt, this fails in the offline CI tier.

Parsing the TS with regexes rather than importing it keeps the check where the risk is — the
literal numbers a reader sees — without a JS runtime in the Python suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_DATA = REPO / "frontend" / "src" / "data" / "benchmarkData.ts"
CORE = json.loads((REPO / "benchmark" / "results" / "core.json").read_text(encoding="utf-8"))
FULL = json.loads((REPO / "benchmark" / "results" / "full.json").read_text(encoding="utf-8"))

TS = UI_DATA.read_text(encoding="utf-8")
TOL = 1e-9


def _block(name: str) -> str:
    """The text between `export const <name> = {` / `= [` and its closing `};` / `];`."""
    m = re.search(rf"export const {name}[^=]*=\s*[\[{{](.*?)[\]}}];", TS, re.DOTALL)
    assert m, f"could not locate the {name} block in benchmarkData.ts"
    return m.group(1)


def _num(block: str, field: str) -> float:
    m = re.search(rf"{field}:\s*([\d.]+)", block)
    assert m, f"field {field} not found"
    return float(m.group(1))


def test_headline_traces_to_receipts():
    h = _block("headline")

    assert abs(_num(h, "accuracy") - CORE["metrics"]["accuracy"]) < TOL
    assert abs(_num(h, "macroF1") - CORE["metrics"]["macro_f1"]) < TOL

    # The load-bearing one: the displayed pass@k must be the receipt's k, not a wish.
    core_k = re.search(r"passAtKCore:\s*\{\s*k:\s*(\d+),\s*value:\s*([\d.]+)", h)
    full_k = re.search(r"passAtKFull:\s*\{\s*k:\s*(\d+),\s*value:\s*([\d.]+)", h)
    assert core_k and full_k
    assert int(core_k.group(1)) == CORE["k"] == CORE["consistency"]["k"]
    assert abs(float(core_k.group(2)) - CORE["consistency"]["pass_at_k"]) < TOL
    assert int(full_k.group(1)) == FULL["k"] == FULL["consistency"]["k"]
    assert abs(float(full_k.group(2)) - FULL["consistency"]["pass_at_k"]) < TOL

    assert abs(_num(h, "costUsd") - FULL["cost_usd"]) < TOL
    sem = FULL["semantic_layer"]
    assert int(_num(h, "explanations")) == sem["explanations"]
    assert int(_num(h, "modelAuthored")) == sem["model_authored"]
    assert int(_num(h, "guardRejected")) == sem["guard_rejected_drafts"]


_PV_ROW = re.compile(
    r"verdict:\s*'([^']+)',\s*precision:\s*([\d.]+),\s*recall:\s*([\d.]+),"
    r"\s*f1:\s*([\d.]+),\s*support:\s*(\d+)"
)


def test_per_verdict_traces_to_core_receipt():
    rows = _PV_ROW.findall(_block("perVerdict"))
    assert rows, "no perVerdict rows parsed"
    for verdict, prec, rec, f1, support in rows:
        want = CORE["metrics"]["per_verdict"][verdict]
        assert abs(float(prec) - want["precision"]) < TOL
        assert abs(float(rec) - want["recall"]) < TOL
        assert abs(float(f1) - want["f1"]) < TOL
        assert int(support) == want["support"]


_CONF_ROW = re.compile(r"actual:\s*'([^']+)',\s*predicted:\s*\[([\d,\s]+)\]")


def test_confusion_matrix_traces_to_core_receipt():
    rows = _CONF_ROW.findall(_block("confusion"))
    assert rows, "no confusion rows parsed"
    labels = CORE["metrics"]["labels"]
    matrix = CORE["metrics"]["confusion_matrix"]
    for actual, predicted_s in rows:
        i = labels.index(actual)
        predicted = [int(x) for x in predicted_s.split(",")]
        # The UI shows the 3x3 verdict block; the receipt carries a 4th No-Claim column.
        assert predicted == matrix[i][: len(predicted)], f"confusion row {actual} drifted"
