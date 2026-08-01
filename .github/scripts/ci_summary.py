# Render the offline CI run as a receipts panel on the GitHub Actions run page.
#
# This is DECORATION, never a gate. The gate is `just check`'s exit code, which this script
# does not touch. So the PASS/FAIL banner is driven by the step's real outcome (CHECK_OUTCOME),
# never by what this parser managed to scrape — a summary that could paint a red run green would
# be exactly the "green light wired to nothing" this repo refuses.
#
# Every row below the banner is READ OUT OF THE LOG, never inferred from the banner. A green
# `just check` does ENTAIL a clean ruff — `check: lint collect-check test-offline` runs fail-fast,
# so nothing downstream runs unless ruff was clean — but an entailed row keeps printing "ruff clean"
# after the recipe it names is dropped from `check`, and on a RED run it cannot say WHICH of the
# three stages broke. So each row is parsed, and a row that cannot be parsed says so rather than
# inventing a result.
#
# Input:  check-output.txt  (tee'd stdout+stderr of `just check`)
#         CHECK_OUTCOME env  ('success' | 'failure' | 'cancelled')
# Output: markdown appended to $GITHUB_STEP_SUMMARY
import os
import re
import sys
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# pytest's final tally line, e.g. "== 370 passed in 12.34s ==" or
# "== 2 failed, 368 passed, 5 skipped in 13.0s ==". --collect-only never prints "passed",
# so the last such line in the log is always the offline test run's.
TALLY = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|deselected)")
DURATION = re.compile(r"\bin\s+([\d.]+)s")
# ruff's success line, verbatim. Its failure output is `path.py:12:5: F401 ...` — no space after
# the colon — so it can never be mistaken for a COLLECTED line below.
LINT_OK = re.compile(r"^All checks passed!$")
# `just collect-check` is `pytest --collect-only -q`, which prints ONE LINE PER FILE
# ("tests/test_api.py: 37") and NO total: the "collected N items" line is verbosity >= 0 only.
# MEASURED against the real command — so the total is summed from the per-file lines, which are
# what the recipe actually leaves in the log.
COLLECTED = re.compile(r"^\S+\.py:\s+(\d+)$")


def parse_lint(lines: list[str]) -> bool:
    return any(LINT_OK.match(ln.strip()) for ln in lines)


def parse_collected(lines: list[str]) -> int | None:
    per_file = [int(m.group(1)) for ln in lines if (m := COLLECTED.match(ln.strip()))]
    return sum(per_file) if per_file else None


def parse_counts(lines: list[str]) -> tuple[dict[str, int], str | None]:
    tally_lines = [
        ln
        for ln in lines
        if TALLY.search(ln) and (" passed" in ln or " failed" in ln or " error" in ln)
    ]
    if not tally_lines:
        return {}, None
    line = tally_lines[-1]
    counts: dict[str, int] = {}
    for n, kind in TALLY.findall(line):
        counts[kind.rstrip("s")] = counts.get(kind.rstrip("s"), 0) + int(n)
    dur = DURATION.search(line)
    return counts, (dur.group(1) if dur else None)


def main() -> None:
    outcome = os.environ.get("CHECK_OUTCOME", "").lower()
    log = ""
    p = Path("check-output.txt")
    if p.exists():
        # utf-8-SIG, not utf-8: a BOM would survive into the first line and make the very first
        # row ("Lint") unparseable — a measured row silently degrading to "see the log above".
        log = p.read_text(encoding="utf-8-sig", errors="replace")
    lines = [ANSI.sub("", ln) for ln in log.splitlines()]
    counts, duration = parse_counts(lines)

    passed = outcome == "success"
    if passed:
        banner = "✅ passed"
    elif outcome == "failure":
        banner = "❌ failed"
    else:
        banner = f"⚠️ {outcome or 'unknown'}"

    if counts:
        parts = [f"**{counts.get('passed', 0)}** passed"]
        if counts.get("failed"):
            parts.append(f"**{counts['failed']} failed**")
        if counts.get("error"):
            parts.append(f"**{counts['error']} error**")
        parts.append(f"{counts.get('skipped', 0)} skipped")
        tests_row = " · ".join(parts)
    else:
        tests_row = "see the log above"

    collected = parse_collected(lines)
    lint_row = "ruff clean" if parse_lint(lines) else "see the log above"
    import_row = f"{collected} tests collected" if collected else "see the log above"
    dur_row = f"{duration}s (pytest)" if duration else "—"
    py = ".".join(str(v) for v in sys.version_info[:3])
    runner = os.environ.get("RUNNER_OS", "").lower() or "runner"

    md = f"""## Attest · offline CI &nbsp; {banner}

The truly-offline tier — **no DataHub, no OpenAI key** — run exactly as `just check` does locally.

|  |  |
| :-- | :-- |
| **Result** | {banner} |
| **Tests** | {tests_row} |
| **Lint** | {lint_row} |
| **Import gate** | {import_row} |
| **Duration** | {dur_row} |
| **Runtime** | Python {py} · {runner} |

> `0 skipped` is the honest signal: the offline tier reads captured fixtures, so nothing skips
> for want of a catalog. A green here is a green about the whole tier — not half of it.
"""

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(md)
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
