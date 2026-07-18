"""THE VACUITY CHECK for crash-recoverable settlement. Remove the intent; demand it stays broken.

`just settle-sabotage`. Exits NON-ZERO if recovery still completes a settlement after the
durable write-ahead intent is removed — same discipline as `just bench-sabotage`,
`just e2e-sabotage`, and `just spike-mcp`. A subprocess-kill-and-recover harness has a hundred
ways to pass without testing anything; this is what proves the intent write is load-bearing
before any green from the recovery test is trusted.

THE SABOTAGE IS TEST-SIDE, exactly like the fault injection it guards. `_barrier_app` honours
`ATTEST_SABOTAGE=record_intent` by wrapping the store so its `record_intent` does NOT persist
— product code (`writeback.py`, `service.py`) carries no sabotage switch. With the intent
gone, the same post-upsert kill leaves the catalog holding a claim with no verdict and NOTHING
in the store to replay. Recovery MUST therefore do nothing, and the artifact MUST stay
UNKNOWN. If it reaches `complete`, recovery worked without the intent, the intent is not
load-bearing, and the recovery test is a green light wired to nothing — which is what this
finds out rather than assumes.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from _barrier_driver import run_barrier_scenario  # noqa: E402
from attest.datahub import DataHubClient  # noqa: E402

TARGET = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.support_tickets,PROD)"


def main() -> int:
    print("=" * 78)
    print("  SETTLEMENT VACUITY CHECK — no durable intent, a post-upsert kill must NOT recover")
    print("=" * 78)
    print("\n  Sabotage: store.record_intent no-oped (test-side). Barrier: after:upsert.")

    window = 40000 + int(time.time()) % 40000
    # ignore_cleanup_errors: on Windows a just-killed subprocess can hold its SQLite file for
    # a beat after exit, and rmtree would raise. The dev temp dir is disposable; the result is
    # not, so a cleanup hiccup must not mask it.
    with (
        DataHubClient() as client,
        tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp,
    ):
        obs = run_barrier_scenario(
            barrier="after:upsert",
            target=TARGET,
            window=window,
            tmp_dir=Path(tmp),
            real_client=client,
            sabotage=True,
        )

    print(f"\n  kill was real   : marker={obs.marker_tripped}  "
          f"approve-hung={not obs.approve_returned_before_kill}  exit={obs.child_exit_code}")
    print("  intent written  : NO (record_intent no-oped)")
    print(f"  pre-recovery    : state={obs.pre_state}  history={obs.pre_history}")
    print(f"  AFTER recovery  : state={obs.post_state}  history={obs.post_history}  "
          f"run={obs.run_status}  unsettled={obs.intent_unsettled_after}")

    if not obs.marker_tripped or obs.approve_returned_before_kill:
        print("\n  INCONCLUSIVE: the kill was not real (the barrier did not block). Fix the")
        print("  harness before trusting either outcome.")
        return 2

    if obs.post_state == "complete":
        print("\n" + "=" * 78)
        print("  FAILED: recovery COMPLETED the artifact with NO durable intent.")
        print("  The intent write is not load-bearing and the recovery test proves nothing.")
        print("=" * 78)
        return 1

    print("\n" + "=" * 78)
    print("  PASSED: with no intent, recovery had nothing to replay — the artifact stayed")
    print(f"  {obs.post_state!r}. The durable write-ahead intent is what makes recovery work.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
