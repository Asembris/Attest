"""Item 1 (Session 12): every displayed calibration number traces to a committed receipt,
and no displayed row can be arithmetically impossible.

The six-run calibration table that used to live in `benchmark/README.md` and the frontend was
UNBACKED — no six per-run receipts existed — and two of its rows were arithmetically impossible
(97.5% or 95% agreement over 40 cases while listing ZERO disputes: 0.975 * 40 = 39 agreed, so
one case must be disputed, not none). It was retracted to the two runs that ARE committed.

This test makes the impossible row unreintroducible. It fails if any displayed agreement
percentage disagrees with its stated dispute count over its case total, or if a displayed row
(README table or UI data) stops matching a committed receipt. The check is deliberately dumb
arithmetic, because dumb arithmetic is exactly what the fake rows violated — and `row_consistent`
is unit-tested against a known-impossible row (the vacuity proof), so the guard itself cannot
silently stop guarding.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from benchmark.cases import CASES

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "benchmark" / "results"
UI_DATA = REPO / "frontend" / "src" / "data" / "benchmarkData.ts"
BENCH_README = REPO / "benchmark" / "README.md"

VALID_CASE_IDS = {c.id for c in CASES}


def row_consistent(agreement: float, disputed_count: int, n_cases: int) -> bool:
    """Does an agreement percentage agree with its dispute count over the case total?

    A single-run agreement is agreed/n_cases, and every case is either agreed or disputed — so
    agreement * n_cases must be a whole number of agreed cases, and (agreed + disputed) must
    equal n_cases. The retracted six-run rows fail the second clause: 97.5% implies 39 agreed
    over 40, so 39 + 0 disputes != 40.
    """
    implied_agreed = agreement * n_cases
    if abs(implied_agreed - round(implied_agreed)) > 1e-9:
        return False
    return round(implied_agreed) + disputed_count == n_cases


# --- the vacuity proof: the guard must reject a known-impossible row ---------------------


def test_row_consistent_rejects_impossible_rows():
    # The real committed shape: 97.5% over 40 with exactly one dispute.
    assert row_consistent(0.975, 1, 40)
    assert row_consistent(0.95, 2, 40)
    assert row_consistent(1.0, 0, 40)
    # The two rows that were arithmetically impossible in the retracted six-run table: an
    # agreement that implies a dispute, paired with zero disputes.
    assert not row_consistent(0.975, 0, 40)
    assert not row_consistent(0.95, 0, 40)
    # An agreement that is not a whole number of cases at all.
    assert not row_consistent(0.97, 1, 40)  # 0.97 * 40 = 38.8


# --- the committed receipts must themselves be consistent -------------------------------


def _receipts() -> list[tuple[Path, dict]]:
    files = sorted(RESULTS.glob("calibration*.json"))
    assert files, "no committed calibration receipts found"
    return [(p, json.loads(p.read_text(encoding="utf-8"))) for p in files]


def test_committed_calibration_receipts_are_arithmetically_consistent():
    for path, d in _receipts():
        n = d["n_cases"]
        disputed = d["disputed"]
        assert d["agreed"] + len(disputed) == n, f"{path.name}: agreed + disputed != n_cases"
        assert row_consistent(d["agreement"], len(disputed), n), f"{path.name}: agreement vs disputes"
        assert d["agreed"] == n - len(disputed), f"{path.name}: agreed != n - disputed"
        for dispute in disputed:
            assert dispute["case"] in VALID_CASE_IDS, f"{path.name}: disputes unknown {dispute['case']}"


def _receipt_signatures() -> set[tuple[float, tuple[str, ...]]]:
    """(agreement, sorted disputed case ids) for each committed receipt — the trace key."""
    return {
        (round(d["agreement"], 6), tuple(sorted(x["case"] for x in d["disputed"])))
        for _, d in _receipts()
    }


# --- the UI rows must be consistent AND trace to a receipt ------------------------------

_UI_ROW_RE = re.compile(
    r"\{\s*condition:[^,]+,\s*agreement:\s*([\d.]+),\s*agreed:\s*(\d+),"
    r"\s*nCases:\s*(\d+),\s*disputed:\s*\[([^\]]*)\]"
)
_CASE_RE = re.compile(r"'([^']+)'")


def test_ui_calibration_rows_trace_to_receipts():
    rows = _UI_ROW_RE.findall(UI_DATA.read_text(encoding="utf-8"))
    assert rows, "no calibrationRuns rows parsed from benchmarkData.ts"
    signatures = _receipt_signatures()
    for agreement_s, agreed_s, n_s, disputed_s in rows:
        agreement, agreed, n = float(agreement_s), int(agreed_s), int(n_s)
        disputed = _CASE_RE.findall(disputed_s)
        assert row_consistent(agreement, len(disputed), n), f"UI row {agreement} inconsistent"
        assert agreed == n - len(disputed), "UI row agreed != n - disputed"
        assert (round(agreement, 6), tuple(sorted(disputed))) in signatures, (
            f"UI row (agreement={agreement}, disputed={disputed}) matches no committed receipt"
        )


# --- the README table must be consistent AND trace to a receipt ------------------------

# | ... | **97.5%** (39/40) | `class-15` | ... |
_README_ROW_RE = re.compile(r"\*\*([\d.]+)%\*\*\s*\((\d+)/(\d+)\)\s*\|\s*`([^`]+)`")


def test_readme_calibration_table_traces_to_receipts():
    rows = _README_ROW_RE.findall(BENCH_README.read_text(encoding="utf-8"))
    assert rows, "no calibration table rows parsed from benchmark/README.md"
    signatures = _receipt_signatures()
    for pct_s, agreed_s, n_s, case in rows:
        agreement, agreed, n = float(pct_s) / 100, int(agreed_s), int(n_s)
        # One disputed case is listed per row.
        assert row_consistent(agreement, 1, n), f"README row {pct_s}% inconsistent"
        assert agreed == n - 1, "README row agreed != n - 1"
        assert (round(agreement, 6), (case,)) in signatures, (
            f"README row (agreement={agreement}, disputed={case}) matches no committed receipt"
        )
