"""Parent-side orchestration for the settlement-recovery barrier harness.

One function, `run_barrier_scenario`, drives the whole thing and returns what it OBSERVED,
so the test asserts and the sabotage spike checks the same evidence. Nothing here reads
Attest's in-memory state: the post-recovery truth comes from a `store=None` reader against
the live catalog, and from a FRESH store opened on the killed process's files.

The choreography, per scenario:

    1. Process A (`_barrier_app`): audit, then approve — which BLOCKS at the barrier inside a
       real DataHub write. The approve is posted from a daemon thread so it can hang.
    2. Wait for the barrier marker. PROVE THE KILL IS REAL: the marker means the barrier
       tripped (for an `after:*` barrier, only after the real mutation returned — so the
       remote call committed), and the approve request has NOT returned (the child did not
       pass the barrier). Then SIGKILL A.
    3. VERIFY THE LOCK INVARIANT: a fresh connection acquires an immediate write lock on both
       SQLite files. A hard kill inside a network call must leave no store lock held; if one
       IS held, that is a real finding, surfaced — never retried past.
    4. Read the artifact PRE-recovery through a `store=None` reader (the second agent).
    5. (kill_during_recovery only) Process B (`_barrier_app`): its startup recovery replays
       the intent and hits a barrier mid-write-back; kill it too, leaving the intent STILL
       unsettled — the proof that recovery is itself re-entrant.
    6. Process C (the SHIPPED app): recovers on startup. Read the artifact POST-recovery, and
       the run + intent from a fresh store.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from attest import writeback
from attest.retrieval import ClaimQuery, ClaimReader, ReadState, RetrievedClaim
from attest.store import AuditStore

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
SHIPPED_APP = "attest.api.app:app"
BARRIER_APP = "_barrier_app:app"


@dataclass
class Observations:
    """Everything the parent watched happen. The test and the sabotage both read this."""

    barrier: str
    marker_tripped: bool
    approve_returned_before_kill: bool
    child_exit_code: int | None
    store_lock_free_after_kill: bool
    checkpoint_lock_free_after_kill: bool
    # Pre-recovery, through a store=None reader (None == no artifact at all).
    pre_state: str | None
    pre_stale_tag: bool
    pre_history: int
    # Only set when kill_during_recovery: recovery replayed and hit a barrier, and the
    # intent was STILL unsettled after that process was killed.
    recovery_barrier_tripped: bool
    intent_unsettled_before_final: int | None
    # Post-recovery, through a store=None reader.
    post_state: str | None
    post_stale_tag: bool
    post_history: int
    post_verdict: str | None
    # Store side, after recovery.
    run_status: str | None
    intent_unsettled_after: int
    artifact_urn: str


# --- low-level process + file helpers ----------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _env(store_path: Path, ckpt_path: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "ATTEST_STORE_PATH": str(store_path),
        "ATTEST_CHECKPOINT_PATH": str(ckpt_path),
        # tests/ on the path so `_barrier_app` and `fakes` import as top-level modules,
        # exactly as pytest resolves them (pyproject `pythonpath = ["tests"]`).
        "PYTHONPATH": str(TESTS) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    env.update(extra)
    return env


def _spawn(entrypoint: str, port: int, env: dict[str, str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", entrypoint,
            "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_health(base: str, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"process died before health:\n{proc.stdout.read()}")
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise AssertionError(f"process never became healthy on {base}")


def _poll_marker(marker: Path, proc: subprocess.Popen, timeout: float = 60.0) -> bool:
    """True once the barrier writes its marker. A process that dies first is a hard failure."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return True
        if proc.poll() is not None:
            raise AssertionError(
                f"process exited (code {proc.returncode}) before reaching the barrier:\n"
                f"{proc.stdout.read() if proc.stdout else ''}"
            )
        time.sleep(0.2)
    return False


def _probe_write_lock(db_path: Path) -> bool:
    """Can a fresh connection take an IMMEDIATE write lock? False == something holds one.

    A SIGKILL'd process should release its SQLite file lock (the OS closes the handle), and
    every barrier sits inside a network call with no store transaction open — so this must be
    True at every barrier point. If it is ever False, a hard kill left a write lock held and
    recovery could deadlock: that is a real finding, and the test asserts True rather than
    retrying past it.
    """
    if not db_path.exists():
        return True
    try:
        con = sqlite3.connect(str(db_path), timeout=1.0)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
        finally:
            con.close()
        return True
    except sqlite3.OperationalError:
        return False


def _kill(proc: subprocess.Popen) -> int | None:
    proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    return proc.returncode


def _derive_artifact_urn(store_path: Path, run_id: str) -> str:
    """The claim artifact's URN, from the claim the pipeline actually stored.

    Read-only (`mode=ro`), so it neither writes the schema nor contends for a lock while the
    child still holds the file. Derived by the same function the write-back uses — never
    re-implemented, because two implementations of one identity must never disagree.
    """
    con = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT claim_json FROM claims WHERE run_id = ? ORDER BY claim_index LIMIT 1",
            (run_id,),
        ).fetchone()
    finally:
        con.close()
    import json

    return writeback.claim_urn(json.loads(row[0]))


def _read_artifact(
    reader: ClaimReader,
    target: str,
    artifact_urn: str,
    want: ReadState | None,
    timeout: float,
) -> RetrievedClaim | None:
    """Poll the DATASET-scoped read (immediate, unlike the lagging search index) for our
    artifact. Returns as soon as it reaches `want`, or the last thing seen at the deadline."""
    deadline = time.monotonic() + timeout
    last: RetrievedClaim | None = None
    while time.monotonic() < deadline:
        page = reader.list(ClaimQuery(target_urn=target), limit=200)
        found = next(
            (c for c in page.claims if c.artifact.claim_urn == artifact_urn), None
        )
        if found is not None:
            last = found
            if want is None or found.state is want:
                return found
        time.sleep(1)
    return last


def _snapshot(claim: RetrievedClaim | None) -> tuple[str | None, bool, int]:
    if claim is None:
        return None, False, 0
    return claim.state.value, claim.stale_tag, len(claim.artifact.history)


# --- the scenario ------------------------------------------------------------


def run_barrier_scenario(
    *,
    barrier: str,
    target: str,
    window: int,
    tmp_dir: Path,
    real_client,
    sabotage: bool = False,
    kill_during_recovery: bool = False,
    expected_pre_state: str | None = None,
) -> Observations:
    store_path = tmp_dir / "attest.db"
    ckpt_path = tmp_dir / "attest-checkpoints.db"
    marker_a = tmp_dir / "marker_a"
    reader = ClaimReader(real_client, store=None)

    env_a = _env(
        store_path, ckpt_path,
        ATTEST_BARRIER=barrier,
        ATTEST_BARRIER_MARKER=str(marker_a),
        ATTEST_BARRIER_WINDOW=str(window),
        ATTEST_BARRIER_TARGET=target,
        **({"ATTEST_SABOTAGE": "record_intent"} if sabotage else {}),
    )
    port_a = free_port()
    proc_a = _spawn(BARRIER_APP, port_a, env_a)
    base_a = f"http://127.0.0.1:{port_a}"
    approve_returned = threading.Event()
    try:
        _wait_health(base_a, proc_a)

        agent_output = f"The dataset {target} is refreshed within {window} hours."
        run_id = httpx.post(
            f"{base_a}/audit",
            json={"agent_output": agent_output, "source_agent": "barrier"},
            timeout=60,
        ).json()["run_id"]
        artifact_urn = _derive_artifact_urn(store_path, run_id)

        def _approve() -> None:
            try:
                httpx.post(
                    f"{base_a}/audit/{run_id}/approve",
                    json={"decisions": [{"claim_index": 0, "publish": True}]},
                    timeout=120,
                )
            except Exception:
                pass  # the process is killed under us; the hung request dies with it
            finally:
                approve_returned.set()

        threading.Thread(target=_approve, daemon=True).start()

        marker_tripped = _poll_marker(marker_a, proc_a)
        # PROVE THE KILL IS REAL: the barrier tripped and the request has not returned.
        approve_done_at_kill = approve_returned.is_set()
        child_exit = _kill(proc_a)
    finally:
        if proc_a.poll() is None:
            _kill(proc_a)

    # THE LOCK INVARIANT, checked at THIS barrier point.
    store_lock_free = _probe_write_lock(store_path)
    ckpt_lock_free = _probe_write_lock(ckpt_path)

    # PRE-recovery, through the second-agent reader. This also confirms the remote commit:
    # an `after:upsert` artifact is present-but-verdictless (UNKNOWN), `after:report` carries
    # the verdict (COMPLETE) with a stale tag, etc. `before:upsert` has no artifact at all.
    #
    # Poll for the EXPECTED state rather than first-sight: a run event is readable a measured
    # ~2s after it is written, so a first-sight read of an `after:report` artifact could catch
    # it verdictless and mis-call it UNKNOWN. When nothing is expected (before:upsert) a short
    # look is enough to confirm absence.
    if expected_pre_state is None:
        pre = _read_artifact(reader, target, artifact_urn, want=None, timeout=5)
    else:
        pre = _read_artifact(
            reader, target, artifact_urn, want=ReadState(expected_pre_state), timeout=15
        )
    pre_state, pre_stale, pre_hist = _snapshot(pre)

    recovery_barrier_tripped = False
    intent_unsettled_before_final: int | None = None
    if kill_during_recovery:
        # A second process whose STARTUP recovery replays the intent and blocks mid-write-back
        # (after re-reporting the verdict, before the atomic store.settle). Killing it must
        # leave the intent STILL unsettled — recovery re-entering is the whole point.
        marker_b = tmp_dir / "marker_b"
        env_b = _env(
            store_path, ckpt_path,
            ATTEST_BARRIER="after:report",
            ATTEST_BARRIER_MARKER=str(marker_b),
            ATTEST_BARRIER_WINDOW=str(window),
            ATTEST_BARRIER_TARGET=target,
        )
        port_b = free_port()
        proc_b = _spawn(BARRIER_APP, port_b, env_b)
        try:
            recovery_barrier_tripped = _poll_marker(marker_b, proc_b, timeout=60)
        finally:
            _kill(proc_b)
        fresh = AuditStore(store_path)
        intent_unsettled_before_final = len(fresh.unsettled_intents())
        fresh.close()

    # PROCESS C: the shipped app recovers on startup.
    port_c = free_port()
    proc_c = _spawn(SHIPPED_APP, port_c, _env(store_path, ckpt_path))
    base_c = f"http://127.0.0.1:{port_c}"
    try:
        _wait_health(base_c, proc_c)
        post = _read_artifact(reader, target, artifact_urn, want=ReadState.COMPLETE, timeout=30)
        post_state, post_stale, post_hist = _snapshot(post)
        post_verdict = post.artifact.verdict if post else None

        fresh = AuditStore(store_path)
        record = fresh.load(run_id)
        run_status = record.status.value if record else None
        intent_unsettled_after = len(fresh.unsettled_intents())
        fresh.close()
    finally:
        _kill(proc_c)

    return Observations(
        barrier=barrier,
        marker_tripped=marker_tripped,
        approve_returned_before_kill=approve_done_at_kill,
        child_exit_code=child_exit,
        store_lock_free_after_kill=store_lock_free,
        checkpoint_lock_free_after_kill=ckpt_lock_free,
        pre_state=pre_state,
        pre_stale_tag=pre_stale,
        pre_history=pre_hist,
        recovery_barrier_tripped=recovery_barrier_tripped,
        intent_unsettled_before_final=intent_unsettled_before_final,
        post_state=post_state,
        post_stale_tag=post_stale,
        post_history=post_hist,
        post_verdict=post_verdict,
        run_status=run_status,
        intent_unsettled_after=intent_unsettled_after,
        artifact_urn=artifact_urn,
    )
