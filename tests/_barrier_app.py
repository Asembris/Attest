"""Composition root for the settlement-recovery barrier harness. Runs in a SUBPROCESS.

    python -m uvicorn _barrier_app:app   (with PYTHONPATH=tests)

This is the whole reason the recovery test can prove a REAL process death rather than a
simulated one, and it does it WITHOUT a fault hook in product code. Every injection here is
TEST-SIDE: it wraps the DataHub client and (for the sabotage) the store the service is
handed, through the same dependency boundary Session 19 used (`app.dependency_overrides`).
`writeback.py` and `service.py` get no env var, no test branch, no `if broken:`.

Everything is driven by env, so the parent can stand up as many differently-configured
subprocesses as a scenario needs:

  ATTEST_BARRIER        "after:report" | "before:upsert" | "after:upsert" | "after:tag" |
                        "none". WHERE the DataHub client should trip a marker and BLOCK
                        forever. The block is the "crash": the parent confirms the remote
                        call committed and the request never returned, then SIGKILLs.
  ATTEST_BARRIER_MARKER path the barrier writes when it trips (atomic rename).
  ATTEST_BARRIER_WINDOW freshness window (hours) for the fake claim — varied per scenario so
                        each lands on a FRESH content-addressed artifact (the trap CLAUDE.md
                        names: a fixed claim reads a prior run's verdict).
  ATTEST_BARRIER_TARGET the seeded dataset URN the claim is about.
  ATTEST_SABOTAGE       "record_intent" no-ops the store's intent write. THE VACUITY CHECK:
                        with no durable intent, a post-upsert kill must restart into an
                        unrecoverable UNKNOWN artifact. Test-side, so product stays clean.
  ATTEST_STORE_PATH /   the shared SQLite files, so a fresh recovery process opens exactly
  ATTEST_CHECKPOINT_PATH  what the killed one left behind.

The model is the scripted fake (a crash-recovery test must not flake on a real model, and
the model is not what is under test); the CATALOG is real, so the write-back really commits
and the run-event history is real.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import UTC, datetime

from langgraph.checkpoint.sqlite import SqliteSaver

from attest.api.app import app, get_service
from attest.api.service import AuditService
from attest.config import settings
from attest.datahub import DataHubClient
from attest.graph import Pipeline
from attest.llm import LLM
from attest.store import AuditStore, SettlementIntent
from fakes import FakeChat, claim_reply, explanation_reply

# short barrier name -> the DataHub client method it fires on
BARRIER_METHODS = {
    "upsert": "upsert_custom_assertion",
    "report": "report_assertion_result",
    "tag": "add_tag",
}


class BarrierClient:
    """A DataHub client that, at ONE configured write, signals the parent and BLOCKS.

    It delegates everything to the real client (`__getattr__`) and only intercepts the three
    write-back mutations. `before` blocks with the remote call NOT yet made; `after` makes
    the real call — so the remote state really commits — and then blocks. The block never
    returns: a barrier that let the request finish would not be a crash. The parent kills the
    process once it has confirmed both that the commit landed and that the request is still
    hanging.

    Nothing here touches the store. The barrier sits INSIDE a network call, so at every
    barrier point no SQLite write lock is held — a property the harness verifies rather than
    assumes.
    """

    def __init__(self, real: object, position: str, step: str, marker: str) -> None:
        self._real = real
        self._position = position  # "before" | "after" | ""
        self._method = BARRIER_METHODS.get(step, "")
        self._marker = marker

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def _guard(self, name: str, *a, **k):
        if name == self._method and self._position == "before":
            self._trip(name)
            self._block()
        result = getattr(self._real, name)(*a, **k)
        if name == self._method and self._position == "after":
            self._trip(name)
            self._block()
        return result

    def upsert_custom_assertion(self, *a, **k):
        return self._guard("upsert_custom_assertion", *a, **k)

    def report_assertion_result(self, *a, **k):
        return self._guard("report_assertion_result", *a, **k)

    def add_tag(self, *a, **k):
        return self._guard("add_tag", *a, **k)

    def _trip(self, name: str) -> None:
        """Signal the parent, atomically, that the barrier for `name` was reached."""
        tmp = self._marker + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(name)
        os.replace(tmp, self._marker)

    @staticmethod
    def _block() -> None:
        """Hang this request thread forever. The parent SIGKILLs the whole process."""
        threading.Event().wait()


class _NoRecordIntent:
    """A store whose `record_intent` does NOT persist. THE SABOTAGE.

    Everything else delegates to the real store, so the only thing missing is the durable
    intent. With it gone, a post-upsert kill leaves the catalog holding a claim with no
    verdict and NOTHING in the store to replay — the recovery must therefore fail, and the
    artifact must stay UNKNOWN. If recovery still completes it, the intent write was not
    load-bearing and the whole harness was testing nothing. Test-side, so product code carries
    no sabotage.
    """

    def __init__(self, real: AuditStore) -> None:
        self._real = real

    def __getattr__(self, name: str):
        return getattr(self._real, name)

    def record_intent(self, run_id: str, payload: str) -> SettlementIntent:
        # A well-formed intent so `approve` proceeds identically — but nothing hits the DB,
        # so `unsettled_intents()` will never surface it and recovery has nothing to do.
        return SettlementIntent(
            intent_id="sabotaged-never-persisted",
            run_id=run_id,
            created_at=datetime.now(tz=UTC),
            payload=payload,
            settled=False,
        )


def _build_service() -> AuditService:
    barrier_spec = os.environ.get("ATTEST_BARRIER", "none")
    position, _, step = barrier_spec.partition(":")
    marker = os.environ.get("ATTEST_BARRIER_MARKER", "")
    target = os.environ.get("ATTEST_BARRIER_TARGET", "")
    window = int(os.environ.get("ATTEST_BARRIER_WINDOW", "50000"))

    real = DataHubClient()
    client = BarrierClient(real, position=position, step=step, marker=marker)

    # A single fresh, Supported freshness claim. Supported so it parks for publication with
    # no correction loop (the write's tail is what is under test, not the loop); fresh so its
    # content-addressed artifact is unique to this scenario.
    claim = {
        "claim_type": "freshness",
        "target_urn": target,
        "raw_text": f"The dataset {target} is refreshed within {window} hours.",
        "max_age_hours": window,
    }
    replies = [
        claim_reply([claim]),
        explanation_reply("The dataset was refreshed within the stated window.", "Supported", []),
    ]

    checkpoints = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    store: AuditStore = AuditStore(settings.store_path)
    if os.environ.get("ATTEST_SABOTAGE") == "record_intent":
        store = _NoRecordIntent(store)  # type: ignore[assignment]

    return AuditService(
        pipeline=Pipeline(
            llm=LLM(client=FakeChat(replies=replies)),
            client=client,
            saver=SqliteSaver(checkpoints),
        ),
        store=store,
        client=client,
    )


_SERVICE: AuditService | None = None


def _service() -> AuditService:
    """One service per process, built once — mirroring the shipped `@lru_cache get_service`.

    Load-bearing: the lifespan (recovery), the audit request, and the approve request must
    all resolve to the SAME instance, or they would each open their own store connection and
    checkpointer and the run would behave like several unrelated services stitched together.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service()
    return _SERVICE


# Serve the SHIPPED app, with the barrier service swapped in through the real DI boundary.
# The shipped lifespan therefore runs recover_settlements() on THIS service at startup —
# which is exactly what makes a process that starts on a stranded store recover it (and, when
# a barrier is armed, hit the barrier DURING that recovery).
app.dependency_overrides[get_service] = _service
