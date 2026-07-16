"""Two audits at once, through one service, and each receipt bills only its own tokens.

Offline and free.

Session 4 serialized audits behind a lock, and the reason was never throughput. One
`Pipeline` meant one `LLM` handle, which meant ONE shared `usage` list, and observe.py bills
a step by slicing that list from a mark taken when the step opened. Two audits through one
handle bill each other: run A's decompose step is charged with whatever run B spends while A
is waiting on the API.

Session 5 removed the sharing rather than guarding it — each run forks its own handle onto
its own ledger (llm.py `for_run`) — and then removed the lock. **That is only an improvement
if the billing is still right, and this file is where that is settled.** A concurrency fix
that silently cross-bills is worse than the queue it replaced: the queue was slow, and this
would be wrong, in the receipts, which are the product.

--------------------------------------------------------------------------------
How the interleaving is made certain rather than hoped for
--------------------------------------------------------------------------------

A test that starts two threads and hopes they overlap proves nothing on a fast machine. So
the fake model HOLDS run A's first call open until run B has finished its entire audit. Run
B's tokens are therefore spent, in full, inside the window of A's decompose step — which is
exactly the window observe.py slices. With a shared usage list, A's decompose bills three
calls instead of one, and A's total comes out at double. With a handle per run it cannot.

And if someone puts the lock back, run B can never start while A is held, so A waits forever:
the fake fails the run by name rather than deadlocking the suite.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from attest.api.service import AuditService
from attest.graph import Pipeline
from attest.llm import LLM
from attest.record import AuditRecord
from attest.report import RunStatus
from attest.store import AuditStore
from attest.trajectory import DECOMPOSE, EXPLAIN
from fakes import FakeDataHub, claim_reply, dataset, explanation_reply

HELD = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
FREE = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders.fact,PROD)"

CAROL = "urn:li:corpuser:carol.davis"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

# Per call, from the fake. Two calls per run (decompose, explain), so an honest receipt is
# 240 tokens and a cross-billed one is not.
TOKENS = (100, 20)
PER_CALL = sum(TOKENS)
CALLS_PER_RUN = 2


def ownership(urn: str) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": urn,
        "raw_text": f"{urn} is owned by {CAROL}.",
        "owner_urn": CAROL,
    }


def says(urn: str) -> str:
    return f"The dataset {urn} is owned by {CAROL}."


class GatedChat:
    """One model, two runs, and run A's first call held open across the whole of run B.

    Replies are chosen by what was ASKED rather than by call order, because two concurrent
    runs interleave and a script indexed by call count would hand run B run A's answers.
    """

    def __init__(self) -> None:
        self.b_finished = threading.Event()
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        temperature: float,
    ) -> Any:
        schema = response_format["json_schema"]["name"]
        body = " ".join(m["content"] for m in messages)
        urn = HELD if HELD in body else FREE

        with self._lock:
            self.calls.append((urn, schema))

        if urn == HELD and schema == "extracted_claims":
            # Run A stops here, INSIDE its decompose step, and does not move until run B has
            # spent every token it is going to spend. If the service still serializes audits,
            # run B never gets to start and this never comes back — so it fails, by name,
            # rather than hanging the suite.
            if not self.b_finished.wait(timeout=30):
                raise AssertionError(
                    "run B never ran while run A was in flight: the service is serializing "
                    "audits again, and the concurrency this test exists to prove is gone"
                )

        content = (
            claim_reply([ownership(urn)])
            if schema == "extracted_claims"
            else explanation_reply(f"{CAROL} is listed as an owner.", "Supported", [])
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=TOKENS[0], completion_tokens=TOKENS[1]),
        )


def build(tmp_path) -> tuple[AuditService, GatedChat]:
    chat = GatedChat()
    catalog = FakeDataHub(
        {
            urn: dataset(
                urn,
                last_modified=NOW - timedelta(hours=6),
                owners=(CAROL,),
                tags=(),
                terms=(),
                custom_properties={},
            )
            for urn in (HELD, FREE)
        }
    )
    service = AuditService(
        pipeline=Pipeline(llm=LLM(client=chat), client=catalog, now=NOW),
        store=AuditStore(tmp_path / "attest.db"),
        client=catalog,
    )
    return service, chat


def step(record: AuditRecord, name: str) -> Any:
    return next(s for s in record.steps if s.name == name)


def test_two_concurrent_audits_do_not_bill_each_other(tmp_path):
    """THE PROPERTY. Run B spends every one of its tokens inside run A's decompose step.

    A shared `usage` list would charge all of them to A — the step slices the list from a
    mark, and it cannot tell whose tokens landed in it. One handle per run means A's slice
    contains A's calls and nothing else, and it means that BY CONSTRUCTION rather than by a
    lock that the next refactor is free to delete.
    """
    service, chat = build(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(service.audit, says(HELD), "agent-a")
        b = pool.submit(service.audit, says(FREE), "agent-b")

        # Run B goes end to end while run A is held. Then A is let go.
        run_b = b.result(timeout=30)
        chat.b_finished.set()
        run_a = a.result(timeout=30)

    # Both PARK rather than complete: every verdict awaits publication (Session 15). What
    # this test is about is the RECEIPTS, and a parked run has already spent its tokens.
    assert run_a.status is RunStatus.AWAITING_REVIEW
    assert run_b.status is RunStatus.AWAITING_REVIEW
    assert len(chat.calls) == 2 * CALLS_PER_RUN, "one of the runs did not do its own work"

    # The receipts. Each run made two calls; each run is charged for two calls.
    assert run_a.receipts.total_tokens == CALLS_PER_RUN * PER_CALL, (
        f"run A was billed {run_a.receipts.total_tokens} tokens for "
        f"{CALLS_PER_RUN * PER_CALL} tokens of work — it is being charged for run B's "
        f"calls, which were made while A's decompose step was open"
    )
    assert run_b.receipts.total_tokens == CALLS_PER_RUN * PER_CALL

    # And per step, which is where the cross-billing would actually land: A's decompose is
    # the step that was open for the whole of B's run.
    assert step(run_a, DECOMPOSE).input_tokens == TOKENS[0]
    assert step(run_a, DECOMPOSE).output_tokens == TOKENS[1]
    assert step(run_a, EXPLAIN).input_tokens == TOKENS[0]
    assert step(run_b, DECOMPOSE).input_tokens == TOKENS[0]

    # The dollar figures follow from the tokens, and they are per-run too. A receipt that
    # totalled two runs together would be exactly the unfounded number Attest exists to
    # catch, printed by Attest.
    assert run_a.receipts.usd == run_b.receipts.usd
    assert run_a.receipts.usd is not None and run_a.receipts.usd > 0


def test_a_second_audit_does_not_wait_for_the_first(tmp_path):
    """And the plain thing a user notices: they are not in a queue.

    Run B completes while run A is still blocked mid-decompose. Under the Session 4 lock
    this could not happen — B would sit behind A's lock until A finished — and the fake
    would fail the run rather than let the suite hang.
    """
    service, chat = build(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(service.audit, says(HELD), "agent-a")
        b = pool.submit(service.audit, says(FREE), "agent-b")

        run_b = b.result(timeout=30)  # finishes with A still in flight, or this times out
        assert not a.done(), "run A was not actually still running: the test proves nothing"
        assert run_b.claims[0].verdict == "Supported"

        chat.b_finished.set()
        assert a.result(timeout=30).claims[0].verdict == "Supported"

    # Both are in the store, whole and separate.
    assert service.get(run_b.run_id).claims[0].target_urn == FREE
