"""Crash-recoverable settlement, proven by a REAL process death mid-write and a REAL recover.

    audit -> approve -> [three catalog writes] -> store.settle
                         ^^^ SIGKILL here, at four points, and once DURING recovery ^^^

Run with `just settle-recover`. Live tier: a real uvicorn subprocess running the shipped
service, a real DataHub, a real kill, and a fresh process that recovers from the durable
write-ahead intent alone. The model is the scripted fake — a crash-recovery test must not
flake on a real model, and the model is not what is under test. Every recovery claim is read
back through a `store=None` reader (the second agent) or from a fresh store on the killed
process's own files; never from Attest's in-memory state.

**WHY THIS EXISTS.** `test_e2e_browser.py` proves the CAUGHT-failure repair; it explicitly
does NOT prove the process-death orphan, calling it "not simulatable without faking it". This
file demolishes that: the property IS testable deterministically. The barrier sits inside the
real DataHub client, injected through the same dependency boundary Session 19 used
(`app.dependency_overrides`), so `writeback.py` and `service.py` carry no fault hook.

**THE VACUITY CHECK IS `just settle-sabotage`, and it runs FIRST.** It no-ops the durable
intent write and demands that the same post-upsert kill restarts into an unrecoverable
UNKNOWN artifact. A subprocess-kill-and-recover harness has a hundred ways to pass without
testing anything; the sabotage is what proves the intent write is load-bearing.

**FIVE barrier points**, and the fifth is the one that makes recovery itself trustworthy:

    before:upsert   nothing written    -> recovery writes everything
    after:upsert    the UNKNOWN window  -> recovery completes the verdict
    after:report    the stale-tag gap   -> recovery swaps the tag
    after:tag       the LOCAL STRAND    -> remote done, store not settled; recovery settles
    after:tag + kill DURING recovery    -> recovery is RE-ENTRANT: a second recovery finishes

Two invariants the plan pinned are asserted at every barrier: the killed child holds NO
SQLite write lock (a hard kill inside a network call leaves the store clean — verified, not
assumed), and the append-only verdict history stays length ONE (idempotent replay).
"""

from __future__ import annotations

import time

import pytest

from _barrier_driver import Observations, run_barrier_scenario

pytestmark = pytest.mark.live


# (id, barrier, kill_during_recovery, expected pre-recovery state, expected pre-recovery stale-tag)
SCENARIOS = [
    ("before-upsert", "before:upsert", False, None, False),
    ("after-upsert-is-UNKNOWN-not-incomplete", "after:upsert", False, "unknown", False),
    ("after-report-leaves-a-stale-tag", "after:report", False, "complete", True),
    ("after-tag-is-a-local-strand", "after:tag", False, "complete", False),
    ("kill-DURING-recovery-is-re-entrant", "after:tag", True, "complete", False),
]


def _assert_kill_was_real(obs: Observations) -> None:
    """The child blocked at the barrier and was killed there — not allowed to finish."""
    assert obs.marker_tripped, "the barrier never tripped: the write path was not reached"
    assert not obs.approve_returned_before_kill, (
        "the approve returned before the kill — the barrier did not block, so nothing was "
        "interrupted and this proves nothing"
    )
    assert obs.child_exit_code is not None, "the child was not actually terminated"


def _assert_lock_invariant(obs: Observations) -> None:
    """A SIGKILL inside a network call must leave no SQLite write lock held. A REAL finding
    if not — never papered over with a retry that goes green on attempt two."""
    assert obs.store_lock_free_after_kill, (
        "the killed child left a WRITE LOCK on the store: a fresh process could not acquire "
        "one immediately. Recovery would contend or deadlock. This is a real robustness "
        "finding, not something to retry past."
    )
    assert obs.checkpoint_lock_free_after_kill, (
        "the killed child left a write lock on the checkpoint database."
    )


def _assert_recovered_clean(obs: Observations) -> None:
    """Read through a store=None reader and a fresh store: the settlement is durable now."""
    assert obs.post_state == "complete", (
        f"after recovery the artifact reads {obs.post_state}, not complete — the fresh "
        f"process did not finish the settlement from the intent"
    )
    assert obs.post_verdict == "Supported", obs.post_verdict
    assert obs.post_history == 1, (
        f"the verdict history is {obs.post_history}, not 1 — recovery re-reported at a new "
        f"timestamp and forged a second audit instead of collapsing onto the same event"
    )
    assert not obs.post_stale_tag, "recovery left the verdict tag stale"
    assert obs.run_status == "complete", (
        f"the run is {obs.run_status} in the store, not complete — the local settlement did "
        f"not durably commit"
    )
    assert obs.intent_unsettled_after == 0, (
        f"{obs.intent_unsettled_after} intent(s) still unsettled after recovery"
    )


@pytest.mark.parametrize(
    "label,barrier,kill_during_recovery,pre_state,pre_stale",
    SCENARIOS,
    ids=[s[0] for s in SCENARIOS],
)
def test_a_process_death_mid_settlement_is_recovered_from_the_intent(
    client, tmp_path, capsys, label, barrier, kill_during_recovery, pre_state, pre_stale
):
    from tests.conftest import OWNED_BY_CAROL

    # A FRESH window per scenario AND per run: the artifact is content-addressed, so a fixed
    # claim would read a prior run's verdict (CLAUDE.md's content-addressing trap). >=40000h
    # (~4.5 years) is Supported for a dataset seeded days ago, whatever the seed's age.
    window = 40000 + (int(time.time()) + hash(label)) % 40000

    obs = run_barrier_scenario(
        barrier=barrier,
        target=OWNED_BY_CAROL,
        window=window,
        tmp_dir=tmp_path,
        real_client=client,
        kill_during_recovery=kill_during_recovery,
        expected_pre_state=pre_state,
    )

    _assert_kill_was_real(obs)
    _assert_lock_invariant(obs)

    # PRE-recovery, as the second agent sees it — absence-is-not-empty preserved:
    # after:upsert is UNKNOWN (present, verdictless), never INCOMPLETE; after:report carries
    # the verdict but a STALE tag; after:tag is a full remote strand.
    assert obs.pre_state == pre_state, (
        f"[{label}] pre-recovery state is {obs.pre_state}, expected {pre_state}"
    )
    assert obs.pre_stale_tag == pre_stale, (
        f"[{label}] pre-recovery stale_tag is {obs.pre_stale_tag}, expected {pre_stale}"
    )

    if kill_during_recovery:
        # A second recovery process replayed the intent and was killed mid-write-back. The
        # intent must have stayed unsettled through that — recovery is re-entrant, and only
        # the final process's atomic settle marks it done.
        assert obs.recovery_barrier_tripped, (
            "recovery never re-ran the write-back — the mid-recovery kill proved nothing"
        )
        assert obs.intent_unsettled_before_final == 1, (
            f"after the mid-recovery kill the intent was {obs.intent_unsettled_before_final} "
            "unsettled, expected 1 — a partial recovery must NOT mark the intent settled"
        )

    _assert_recovered_clean(obs)

    with capsys.disabled():
        print(f"\n  [{label}]  barrier={barrier}")
        print(f"    kill real     : marker={obs.marker_tripped}  approve-hung="
              f"{not obs.approve_returned_before_kill}  exit={obs.child_exit_code}")
        print(f"    lock invariant: store_free={obs.store_lock_free_after_kill}  "
              f"ckpt_free={obs.checkpoint_lock_free_after_kill}")
        print(f"    pre-recovery  : state={obs.pre_state}  stale_tag={obs.pre_stale_tag}  "
              f"history={obs.pre_history}")
        if kill_during_recovery:
            print(f"    mid-recovery  : replayed={obs.recovery_barrier_tripped}  "
                  f"intent-still-unsettled={obs.intent_unsettled_before_final}")
        print(f"    post-recovery : state={obs.post_state}  verdict={obs.post_verdict}  "
              f"history={obs.post_history}  run={obs.run_status}  "
              f"unsettled={obs.intent_unsettled_after}")
