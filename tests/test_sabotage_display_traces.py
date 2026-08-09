"""Every judge-facing SABOTAGE figure traces to `core-sabotaged-classification.json`.

`test_benchmark_display_traces.py` pins the frontend's healthy numbers to `core.json` /
`full.json`. It never opens the sabotage receipt — so the repo's only evidence that the
benchmark CAN fail was displayed in five places, hand-typed, backed by nothing. CLAUDE.md
§26 names that gap in as many words ("NAMED, NOT FIXED: no test pins 67.5% to the receipt")
and this closes it.

The receipt is the source of truth and is never edited by this module. **No numeric literal
from the receipt appears below** — the display forms a reader sees (`67.5`, `0.536`) are
DERIVED from the stored values, so regenerating the receipt moves the expectations with it
and a surface that drifted is what fails. `pct()` and `three()` are the only two
transformations, and they are the ones the surfaces actually use.

Regexes anchor on structure — a table cell, a `data-final` attribute, the sentence's own
framing — not on prose, so a harmless rewording does not redlight. Every extractor asserts
it matched something before it asserts what it matched: a pin that silently parses nothing
is a green light wired to nothing, which is the argument the sabotage receipt exists to
make. `test_the_display_check_can_actually_fail` proves that end of it on a mutated copy in
tmp_path, never on the working tree.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RECEIPT = REPO / "benchmark" / "results" / "core-sabotaged-classification.json"
SABOTAGE = json.loads(RECEIPT.read_text(encoding="utf-8"))
METRICS = SABOTAGE["metrics"]

README = REPO / "README.md"
BENCH_README = REPO / "benchmark" / "README.md"
SUBMISSION = REPO / "docs" / "submission.md"
PAGES = REPO / "docs" / "index.html"
BENCHMARK_TSX = REPO / "frontend" / "src" / "components" / "Benchmark.tsx"


def pct(x: float) -> str:
    """`0.675` as the page prints it: `67.5`."""
    return f"{x * 100:.1f}"


def three(x: float) -> str:
    """`0.5357142857142857` as the table prints it: `0.536`."""
    return f"{x:.3f}"


ACCURACY = METRICS["accuracy"]
MACRO_F1 = METRICS["macro_f1"]
SUPPORTED_PRECISION = METRICS["per_verdict"]["Supported"]["precision"]
CONTRADICTED_RECALL = METRICS["per_verdict"]["Contradicted"]["recall"]
IC_RECALL = METRICS["per_verdict"]["Insufficient-Coverage"]["recall"]
CORRECTNESS = METRICS["correctness_failures"]
COVERAGE = METRICS["coverage_failures"]
N_ERRORS = len(METRICS["errors"])


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _found(pattern: str, text: str, what: str) -> re.Match[str]:
    """Match, or say which surface stopped carrying the figure. The non-vacuity guard."""
    m = re.search(pattern, text, re.DOTALL)
    assert m, f"no {what} found — the surface moved, so this pin is measuring nothing"
    return m


# --- README ------------------------------------------------------------------


def _check_readme(text: str) -> None:
    """Both README sabotage surfaces: the lead sentence and the receipt-table row."""
    lead = _found(
        r"falls from 1\.000 to ([\d.]+), Supported precision to ([\d.]+), with (\d+) "
        r"correctness\s*\n?and (\d+) coverage failures",
        text,
        "README sabotage sentence",
    )
    assert lead.group(1) == three(ACCURACY)
    assert lead.group(2) == three(SUPPORTED_PRECISION)
    assert int(lead.group(3)) == CORRECTNESS
    assert int(lead.group(4)) == COVERAGE

    row = _found(
        r"\*\*Sabotage\*\*[^\n|]*\|\s*accuracy \*\*([\d.]+)\*\*, macro-F1 \*\*([\d.]+)\*\*, "
        r"Supported precision \*\*([\d.]+)\*\*, \*\*(\d+)\*\* errors named",
        text,
        "README receipt-table sabotage row",
    )
    assert row.group(1) == three(ACCURACY)
    assert row.group(2) == three(MACRO_F1)
    assert row.group(3) == three(SUPPORTED_PRECISION)
    assert int(row.group(4)) == N_ERRORS


def test_the_readme_sabotage_figures_trace_to_the_receipt() -> None:
    _check_readme(_text(README))


# --- benchmark/README --------------------------------------------------------

_SABOTAGE_ROW = re.compile(
    r"^\|\s*([^|]+?)\s*\|\s*[^|]+\|\s*\*\*([\d.%]+)\*\*\s*\|\s*$", re.MULTILINE
)

# Row label -> the receipt value it must equal, in the form that row prints.
_BENCH_ROWS = {
    "Accuracy": lambda: pct(ACCURACY) + "%",
    "Supported precision": lambda: three(SUPPORTED_PRECISION),
    "Contradicted recall": lambda: three(CONTRADICTED_RECALL),
    "Insufficient-Coverage recall": lambda: three(IC_RECALL),
    "Correctness failures": lambda: str(CORRECTNESS),
    "Coverage failures": lambda: str(COVERAGE),
}


def _check_bench_readme(text: str) -> None:
    """Both Healthy/Sabotaged tables. Every recognised row must match its receipt value."""
    rows = _SABOTAGE_ROW.findall(text)
    assert rows, "no Healthy/Sabotaged table rows parsed from benchmark/README.md"

    checked = 0
    for label, shown in rows:
        expected = _BENCH_ROWS.get(label)
        if expected is None:
            continue
        assert shown == expected(), f"benchmark/README row {label!r} drifted from the receipt"
        checked += 1

    # Both tables are present, so every label appears at least once and accuracy twice.
    assert checked >= len(_BENCH_ROWS) + 1, (
        f"only {checked} sabotage table cells matched a known row — the tables were "
        "restructured and this pin no longer covers them"
    )


def test_the_benchmark_readme_sabotage_tables_trace_to_the_receipt() -> None:
    _check_bench_readme(_text(BENCH_README))


# --- docs/submission.md ------------------------------------------------------


def _check_submission(text: str) -> None:
    m = _found(
        r"falls from accuracy 1\.000 to ([\d.]+)\*\*, Supported precision to ([\d.]+), with "
        r"(\d+)\s*\ncorrectness and (\d+) coverage failures",
        text,
        "submission sabotage sentence",
    )
    assert m.group(1) == three(ACCURACY)
    assert m.group(2) == three(SUPPORTED_PRECISION)
    assert int(m.group(3)) == CORRECTNESS
    assert int(m.group(4)) == COVERAGE


def test_the_submission_doc_sabotage_figures_trace_to_the_receipt() -> None:
    _check_submission(_text(SUBMISSION))


# --- docs/index.html (GitHub Pages) ------------------------------------------


def _check_pages(text: str) -> None:
    """The animated stat counts UP to `data-final`, and prints `data-dec` decimals."""
    m = _found(
        r'data-final="([\d.]+)" data-dec="(\d+)">([\d.]+)</span>%\s*</div>\s*'
        r'<div class="l">when a checker is deliberately sabotaged',
        text,
        "Pages sabotage stat",
    )
    assert m.group(1) == pct(ACCURACY), "the counter's target is not the receipt's accuracy"
    assert int(m.group(2)) == 1, "one decimal is what pct() renders; the markup disagrees"
    assert m.group(3) == pct(ACCURACY), "the pre-animation text differs from the target"


def test_the_pages_sabotage_stat_traces_to_the_receipt() -> None:
    _check_pages(_text(PAGES))


# --- frontend/src/components/Benchmark.tsx -----------------------------------


def _check_benchmark_tsx(text: str) -> None:
    m = _found(
        r"affirm-everything checker <span[^>]*>drops accuracy to ([\d.]+)%</span>",
        text,
        "Benchmark.tsx sabotage prose",
    )
    assert m.group(1) == pct(ACCURACY)


def test_the_frontend_sabotage_prose_traces_to_the_receipt() -> None:
    _check_benchmark_tsx(_text(BENCHMARK_TSX))


# --- the vacuity check -------------------------------------------------------

_MUTATIONS = [
    # surface, its extractor, the anchor to move, what to move it to
    (README, _check_readme, three(ACCURACY), "0.700"),
    (BENCH_README, _check_bench_readme, pct(ACCURACY) + "%", "70.0%"),
    (SUBMISSION, _check_submission, three(ACCURACY), "0.700"),
    (PAGES, _check_pages, f'data-final="{pct(ACCURACY)}"', 'data-final="70.0"'),
    (BENCHMARK_TSX, _check_benchmark_tsx, f"to {pct(ACCURACY)}%", "to 70.0%"),
]


def test_the_display_check_can_actually_fail(tmp_path: Path) -> None:
    """THE VACUITY CHECK, on copies — the working tree is never touched.

    A pin that finds nothing and a pin that finds the right thing are indistinguishable
    from a green tick. So EVERY extractor above is handed its own surface with the
    sabotaged accuracy moved off the receipt's value, and must say so — one silent
    extractor is enough to fail this. Run on tmp_path copies rather than by editing and
    restoring the real files: a restore that half-failed would leave a doctored public
    claim in the repo, which is a worse outcome than an unproven pin.
    """
    for surface, check, old, new in _MUTATIONS:
        original = surface.read_text(encoding="utf-8")
        assert old in original, f"the mutation anchor {old!r} is not in {surface.name}"

        doctored = tmp_path / surface.name
        doctored.write_text(original.replace(old, new), encoding="utf-8")

        with pytest.raises(AssertionError):
            check(doctored.read_text(encoding="utf-8"))

        # And the same extractor still passes on the untouched text, so the failure above
        # is the mutation and not a broken harness.
        check(original)
