"""THE VACUITY CHECK FOR THE DEPLOYMENT SMOKE TEST. Break four boundaries; demand red.

`just smoke-sabotage` exits nonzero if the real-stack smoke stays green under sabotage. The
four faults correspond to the four claims `just smoke` now makes:

  1. A container did not come up: point Attest at a dead GMS and fail fast at the wire.
  2. The deployed server did not start: launch uvicorn with a nonexistent app module.
  3. The built UI is not served: request a deliberately absent JavaScript asset.
  4. The demo path does not answer: feed the audit an unseeded URN, producing no verdict.

Every fault is test-side configuration. Product code contains no sabotage hook, and every
failure must include its boundary-specific marker rather than merely exit nonzero.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

UNSEEDED = (
    "The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public."
    "does_not_exist_smoke,PROD) contains no PII."
)


@dataclass(frozen=True)
class Sabotage:
    name: str
    why: str
    env: dict[str, str]
    marker: str
    fast_seconds: float


SABOTAGES = (
    Sabotage(
        name="a container did not come up",
        why="point Attest at a dead GMS; the stack is not reachable",
        env={"DATAHUB_GMS_URL": "http://localhost:59999"},
        marker="not reachable",
        fast_seconds=60.0,
    ),
    Sabotage(
        name="the deployed server did not start",
        why="launch uvicorn with a nonexistent ASGI module",
        env={"ATTEST_SMOKE_APP_MODULE": "attest.api.missing_smoke_app:app"},
        marker="deployed Attest/UI did not start",
        fast_seconds=60.0,
    ),
    Sabotage(
        name="the built frontend is not served",
        why="request a JavaScript asset that the deployed app cannot serve",
        env={"ATTEST_SMOKE_ASSET_PATH": "/assets/does-not-exist-smoke.js"},
        marker="built frontend asset is not reachable",
        fast_seconds=60.0,
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
    """Run the same real-uvicorn runner as `just smoke`. True means it passed."""
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "spikes/smoke_runner.py"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={**os.environ, **extra_env},
    )
    return result.returncode == 0, result.stdout + result.stderr, time.monotonic() - start


def main() -> int:
    print("=" * 78)
    print("  SMOKE VACUITY CHECK - four boundaries of 'one command runs everything'")
    print("=" * 78)

    survived: list[str] = []
    wrong_place: list[str] = []

    for sabotage in SABOTAGES:
        print(f"\n--- SABOTAGE: {sabotage.name}")
        print(f"    why    : {sabotage.why}")
        print(f"    expect : red, with {sabotage.marker!r} in the output")
        passed, output, elapsed = run_smoke(sabotage.env)

        if passed:
            survived.append(sabotage.name)
            print("    RESULT : *** THE SMOKE TEST STAYED GREEN. SABOTAGE SURVIVED. ***")
            continue

        if sabotage.marker not in output:
            wrong_place.append(sabotage.name)
            print(f"    RESULT : went red, but NOT at {sabotage.marker!r} - wrong reason")
            failures = [
                line
                for line in output.splitlines()
                if line.strip().startswith(("E ", "FAILED", "the deployed", "the built"))
            ]
            for line in failures[:3]:
                print(f"             {line.strip()[:96]}")
            continue

        timing = ""
        if sabotage.fast_seconds:
            fast = elapsed <= sabotage.fast_seconds
            verdict = "FAST" if fast else "SLOW - a hang, not a diagnosis"
            timing = f"  (failed in {elapsed:.1f}s, {verdict})"
            if not fast:
                wrong_place.append(sabotage.name)
        print(f"    RESULT : caught - red at {sabotage.marker!r}{timing}")

    print("\n" + "=" * 78)
    if survived or wrong_place:
        if survived:
            print(f"  FAILED: {len(survived)} sabotage(s) SURVIVED: {survived}")
        if wrong_place:
            print(f"  FAILED: {len(wrong_place)} sabotage(s) red for the WRONG reason:")
            print(f"          {wrong_place}")
        print("=" * 78)
        return 1

    print(f"  PASSED: all {len(SABOTAGES)} sabotages caught at their own boundary.")
    print("  DataHub, uvicorn, built UI, and demo API are all load-bearing.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
