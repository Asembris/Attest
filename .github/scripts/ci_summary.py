# Render the offline CI run as a receipts panel on the GitHub Actions run page.
#
# This is DECORATION, never a gate. The gate is `just check`'s exit code, which this script
# does not touch. So the PASS/FAIL banner is driven by the step's real outcome (CHECK_OUTCOME),
# never by what this parser managed to scrape — a summary that could paint a red run green would
# be exactly the "green light wired to nothing" this repo refuses. The counts are supplementary:
# if the pytest tail cannot be parsed, the row says so rather than inventing a number.
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


def parse_counts(log: str) -> tuple[dict[str, int], str | None]:
    lines = [ANSI.sub("", ln) for ln in log.splitlines()]
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
        log = p.read_text(encoding="utf-8", errors="replace")
    counts, duration = parse_counts(log)

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

    lint_row = "ruff clean" if passed else "see the log above"
    import_row = "collect-check clean" if passed else "see the log above"
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
