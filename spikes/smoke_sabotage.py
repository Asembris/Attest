"""THE VACUITY CHECK FOR THE DEPLOYMENT SMOKE TEST. Break it two ways; demand it goes red.

`just smoke-sabotage`. Exits NON-ZERO if the smoke test stays green under sabotage — same
discipline as `just bench-sabotage`, `just spike-mcp`, and `just e2e-sabotage`. A smoke test
that cannot fail is a green light wired to nothing, and this one's whole justification is
making the README's "one command runs everything" claim falsifiable.

THE TWO SABOTAGES ARE THE TWO FAILURE MODES THE CLAIM HAS:

    1. A CONTAINER DID NOT COME UP. Point Attest at a dead GMS. The smoke test must go red at
       the reachability gate — AND FAST, in seconds, not on a 90s downstream timeout. "The
       stack is not up" has to read as exactly that, immediately (the Session 19 lesson: a
       diagnostic that only fires after a long wait is a mystery hang, not a diagnosis).

    2. THE DEMO PATH DOES NOT ANSWER. Feed the audit an UNSEEDED URN. Every claim ClaimErrors,
       no verdict is produced, and the smoke test must go red at the "demo answers" assertion.

Neither touches product code: both are ENV overrides the smoke test already reads
(DATAHUB_GMS_URL, ATTEST_SMOKE_SAMPLE), so there is nothing to restore and no way to leave a
sabotaged tree behind. Each sabotage must not only go red but go red IN THE RIGHT PLACE — the
marker string pins where — or the smoke test is passing and failing for the wrong reasons.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# A well-formed dataset URN that is NOT in the seed. Decompose will quote it (it is in the
# text), resolve will fail, and the claim becomes a ClaimError — so `claims` is empty.
UNSEEDED = (
    "The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public."
    "does_not_exist_smoke,PROD) contains no PII."
)


@dataclass(frozen=True)
class Sabotage:
    name: str
    why: str
    env: dict[str, str]
    marker: str          # must appear in the failure output — pins WHERE it went red
    fast_seconds: float  # 0 = don't check timing; >0 = must fail within this many seconds


SABOTAGES = (
    Sabotage(
        name="a container did not come up",
        why="point Attest at a dead GMS; the stack is not reachable",
        env={"DATAHUB_GMS_URL": "http://localhost:59999"},
        marker="not reachable",
        fast_seconds=60.0,  # the reachability gate caps at 3s; a naive timeout would be far longer
    ),
    Sabotage(
        name="the demo path does not answer",
        why="feed the audit an unseeded URN; no verdict can be produced",
        env={"ATTEST_SMOKE_SAMPLE": UNSEEDED},
        marker="demo path does not answer",
        fast_seconds=0.0,
    ),
)


def run_smoke(extra_env: dict[str, str]) -> tuple[bool, str, float]:
    """Run the smoke test as a subprocess with an env override. True == it PASSED."""
    start = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_smoke.py", "-m", "live", "-q",
         "--no-header", "-x"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={**os.environ, **extra_env},
    )
    return r.returncode == 0, r.stdout + r.stderr, time.monotonic() - start


def main() -> int:
    print("=" * 78)
    print("  SMOKE VACUITY CHECK — the two failure modes of 'one command runs everything'")
    print("=" * 78)

    survived: list[str] = []
    wrong_place: list[str] = []

    for s in SABOTAGES:
        print(f"\n--- SABOTAGE: {s.name}")
        print(f"    why    : {s.why}")
        print(f"    expect : red, with {s.marker!r} in the output")
        passed, out, elapsed = run_smoke(s.env)

        if passed:
            survived.append(s.name)
            print("    RESULT : *** THE SMOKE TEST STAYED GREEN. SABOTAGE SURVIVED. ***")
            continue

        if s.marker not in out:
            wrong_place.append(s.name)
            print(f"    RESULT : went red, but NOT at {s.marker!r} — wrong reason")
            for ln in [l for l in out.splitlines() if l.strip().startswith(("E ", "FAILED"))][:3]:
                print(f"             {ln.strip()[:96]}")
            continue

        timing = ""
        if s.fast_seconds:
            ok = elapsed <= s.fast_seconds
            timing = f"  (failed in {elapsed:.1f}s, {'FAST' if ok else 'SLOW — a hang, not a diagnosis'})"
            if not ok:
                wrong_place.append(s.name)
        print(f"    RESULT : caught — red at {s.marker!r}{timing}")

    print("\n" + "=" * 78)
    if survived or wrong_place:
        if survived:
            print(f"  FAILED: {len(survived)} sabotage(s) SURVIVED: {survived}")
            print("  The smoke test cannot detect a failure it exists to detect.")
        if wrong_place:
            print(f"  FAILED: {len(wrong_place)} sabotage(s) went red for the WRONG reason: {wrong_place}")
            print("  Red at the wrong place (or too slowly) is not the diagnosis it claims.")
        print("=" * 78)
        return 1

    print(f"  PASSED: all {len(SABOTAGES)} sabotages caught, each red in the right place.")
    print("  The smoke test can fail for both real reasons — a dead stack and a dead demo path.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
