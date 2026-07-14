"""Durable resume: a run parked at the human checkpoint outlives the process that parked it.

Offline and free. The model is the scripted fake and the catalog is a fixture; what is under
test is whether a restarted service can finish an audit it did not start, and whether the
report it produces is the one the unrestarted run would have produced.

Three assertions carry this file, and the second is the one that makes the feature real.

**The decision lands.** Obvious, and not sufficient.

**`human_checkpoint` appears in the RESUMED trace.** Applying the decision straight to the
stored record would satisfy the first assertion perfectly, and it would be a second path to
the one thing in this system that must not have one: a correction settled without the
checkpoint node ever running, invisible from the outside, taken only after a restart. This
assertion is the only thing standing between "one durable path" and "two paths, one of them
unaudited".

**The report is the same report.** "Resume works" and "resume works and the report is
identical" are different features, and only the second is worth having in an auditor. A
restarted run that quietly reports something slightly different is indistinguishable from
one that does not — and the sharpest case is not cosmetic: without `StepRecord.models` in
the store, a rehydrated run recomputes an unpriced run's cost as `sum(...) = 0.0` where the
original honestly said `None`. That is a restarted audit FABRICATING a cost figure, which is
the exact failure Attest exists to catch, sitting in Attest's own receipts. It gets its own
test below.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from attest import trajectory
from attest.api.service import AuditService, NotResumable
from attest.graph import Pipeline
from attest.llm import LLM
from attest.observe import Trace
from attest.record import AuditRecord, from_report
from attest.report import Decision, ReviewStatus, RunStatus
from attest.store import AuditStore
from attest.trajectory import CHECKPOINT, AuditedClaim
from fakes import FakeChat, FakeDataHub, claim_reply, dataset, explanation_reply, revision_reply

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"

ALICE = "urn:li:corpuser:alice.chen"
CAROL = "urn:li:corpuser:carol.davis"
PII = "urn:li:tag:PII"

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
SAYS = f"The dataset {SF} is owned by {ALICE}."


def ownership(owner: str) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": SF,
        "raw_text": f"{SF} is owned by {owner}.",
        "owner_urn": owner,
    }


# A claim naming alice, a catalog naming carol, and a revision the catalog confirms: one
# contradiction, one correctable proposal, and therefore a run that PARKS.
SCRIPT = (
    claim_reply([ownership(ALICE)]),
    explanation_reply("the catalog lists a different owner.", "Contradicted", []),
    revision_reply(ownership(CAROL)),
)


def catalog() -> FakeDataHub:
    return FakeDataHub(
        {
            SF: dataset(
                SF,
                last_modified=NOW - timedelta(hours=6),
                owners=(CAROL,),
                tags=(PII,),
                terms=(),
                custom_properties={},
            )
        }
    )


def service(tmp_path, name: str, tokens: tuple[int, int] | None = None) -> AuditService:
    """A service on its own store and its own checkpoint file, on disk, like the real one.

    Two SQLite files, deliberately: LangGraph owns the checkpoint tables, Attest owns the
    audit history, and the whole point of this suite is that they are two DURABLE things
    that a restart puts back together.
    """
    checkpoints = sqlite3.connect(
        str(tmp_path / f"{name}-checkpoints.db"), check_same_thread=False
    )
    fake = catalog()
    return AuditService(
        pipeline=Pipeline(
            llm=LLM(client=FakeChat(replies=list(SCRIPT), tokens=tokens)),
            client=fake,
            now=NOW,
            saver=SqliteSaver(checkpoints),
        ),
        store=AuditStore(tmp_path / f"{name}.db"),
        client=fake,
    )


def restart(tmp_path, name: str, tokens: tuple[int, int] | None = None) -> AuditService:
    """What the service looks like after a redeploy: same two files, everything else new.

    A brand new Pipeline, a brand new graph, a brand new SqliteSaver over the same file, a
    brand new LLM handle, and a brand new AuditStore. Nothing survives in memory. The only
    things that cross the boundary are the two databases, which is exactly the claim.
    """
    return service(tmp_path, name, tokens=tokens)


def normalized(record: AuditRecord) -> dict[str, Any]:
    """A record with the wall clock and the run's identity taken out.

    Two runs of the same audit cannot have the same id, timestamp, or latencies — those are
    facts about WHEN it ran, not about what it found. Everything else must match, and
    "everything else" is the whole report: verdicts, evidence, explanations and their
    source, the drafts the guard rejected, the tokens, the models, the trajectory, the
    catalog receipt, the corrections and how they were reviewed.
    """
    body = record.model_dump(mode="json")
    body.pop("run_id")
    body.pop("created_at")
    body["receipts"].pop("latency_ms")
    for step in body["steps"]:
        step.pop("latency_ms")
    return body


# --- the feature -------------------------------------------------------------


def test_a_run_parked_by_a_dead_process_is_resumed_through_the_checkpoint_node(tmp_path):
    """THE TEST. Park a run, throw the entire pipeline away, and finish it from the store.

    (a) the decision lands, (b) the checkpoint NODE ran on the resumed graph, and (c) the
    report is the one an unrestarted run would have produced. Without (b) this feature
    degrades into the shortcut Session 4 refused — settling a correction by editing the
    stored record — and it would look identical from the outside.
    """
    first = service(tmp_path, "durable")
    parked = first.audit(SAYS, source_agent="bot")

    assert parked.status is RunStatus.AWAITING_REVIEW
    assert parked.claims[0].correction.review is ReviewStatus.PENDING
    assert CHECKPOINT not in [s.name for s in parked.steps], (
        "the checkpoint node must not have run: the graph is parked BEFORE it"
    )

    # The process dies. Everything in memory goes with it — the pipeline, the graph, the
    # ledger, the LLM handle, the store's connection. Two files remain.
    del first

    second = restart(tmp_path, "durable")
    settled, writebacks = second.approve(
        parked.run_id, [Decision(claim_index=0, accept=True)]
    )

    # (a) the decision landed.
    assert settled.status is RunStatus.COMPLETE
    assert settled.claims[0].correction.review is ReviewStatus.ACCEPTED
    assert writebacks[0].ok, "an accepted correction did not reach the catalog"

    # (b) IT WENT THROUGH THE CHECKPOINT NODE. Not around it.
    resumed = [s.name for s in settled.steps]
    assert CHECKPOINT in resumed, (
        "the decision was applied without the human_checkpoint node running. That is a "
        "second, unaudited path to the one thing in this system that must not have one."
    )
    assert resumed.index(CHECKPOINT) == len(parked.steps), (
        "the checkpoint did not run at the point the graph was parked at"
    )

    # (c) and the run is still trustworthy on its own terms.
    assert settled.receipts.trajectory_ok, settled.receipts.trajectory_summary
    assert settled.claims[0].verdict == "Contradicted", "an accepted correction never unsays"


def test_the_resumed_report_is_the_report_the_unrestarted_run_would_have_produced(tmp_path):
    """Byte-for-byte, modulo the clock. Not "it resumed" — "it resumed and nothing moved".

    A restarted run that reports something subtly different from an unrestarted one is the
    worst kind of bug this system could have: it is invisible, it is on the path a human
    uses to sign off a change to the catalog, and everything about the report still LOOKS
    right. So the two are compared whole.
    """
    restarted = service(tmp_path, "restarted")
    parked = restarted.audit(SAYS, source_agent="bot")
    del restarted
    after_restart = restart(tmp_path, "restarted").approve(
        parked.run_id, [Decision(claim_index=0, accept=True)]
    )[0]

    # The same audit, start to finish, in one process.
    straight_through = service(tmp_path, "straight")
    parked_too = straight_through.audit(SAYS, source_agent="bot")
    unrestarted = straight_through.approve(
        parked_too.run_id, [Decision(claim_index=0, accept=True)]
    )[0]

    assert normalized(after_restart) == normalized(unrestarted)


def test_a_rehydrated_run_does_not_invent_a_cost_the_original_refused_to_state(
    tmp_path, monkeypatch
):
    """THE SHARPEST GAP, and the reason record.py carries `models`.

    A run against a model with no price reports `usd = None` — unknown, never zero (cost.py).
    Trace.cost works that out from the model NAMES on the steps that spent tokens. Drop them
    on the way to disk and a rehydrated run finds no unpriced models, sums the per-step costs
    it does have, and reports `0.0`: a restarted audit fabricating a figure the original
    honestly declined to give.

    That is the None-is-not-zero rule breaking inside Attest's own receipts, on the one path
    a human uses to approve a change. It is exactly the failure this product exists to catch.
    """
    monkeypatch.setattr("attest.config.settings.model_default", "gpt-9-unreleased")

    first = service(tmp_path, "unpriced", tokens=(400, 90))
    parked = first.audit(SAYS)

    assert parked.receipts.total_tokens > 0, "the fake must bill, or this proves nothing"
    assert parked.receipts.usd is None, "an unpriced model must cost None, never 0"
    billed = [s for s in parked.steps if s.models]
    assert billed and all(s.models == ("gpt-9-unreleased",) for s in billed), (
        "the model names are what make the cost unknowable rather than zero"
    )

    del first
    settled = restart(tmp_path, "unpriced", tokens=(400, 90)).approve(
        parked.run_id, [Decision(claim_index=0, accept=True)]
    )[0]

    assert settled.receipts.usd is None, (
        "the resumed run invented a dollar figure the original refused to state — the "
        "rehydrated trace lost the model names, so nothing told it the cost was unknown"
    )
    assert settled.receipts.total_tokens == parked.receipts.total_tokens


def test_the_guards_own_failures_survive_the_restart(tmp_path):
    """A rejected draft, and the tokens it was rejected FOR, are part of the audit.

    The fake hallucinates an owner nobody in the evidence mentions, so the guard throws the
    draft away — twice — and the deterministic template ships instead. A resumed run must
    still say so: an auditor that forgets it had to fall back is flattering its own semantic
    layer, and it would be doing it only on the restart path, where nobody is looking.

    Note which explanation `faithful` describes: the one that SHIPPED. The template is
    faithful by construction, so the guard's own failures live in `rejected`, one entry per
    thrown-away draft, naming the tokens that killed it.
    """
    fake = catalog()
    checkpoints = sqlite3.connect(str(tmp_path / "guard-checkpoints.db"), check_same_thread=False)
    # The lie twice: explain() hands the model its own violations back and tries once more
    # before it gives up, so a script that lies only once would have the retry consuming the
    # revision reply and testing something else entirely.
    lie = explanation_reply("This dataset is owned by Sarah Jennings.", "Contradicted", [])
    lying = (claim_reply([ownership(ALICE)]), lie, lie, revision_reply(ownership(CAROL)))
    first = AuditService(
        pipeline=Pipeline(
            llm=LLM(client=FakeChat(replies=list(lying))),
            client=fake,
            now=NOW,
            saver=SqliteSaver(checkpoints),
        ),
        store=AuditStore(tmp_path / "guard.db"),
        client=fake,
    )
    parked = first.audit(SAYS)

    claim = parked.claims[0]
    assert claim.explanation_source == "template", "unfaithful prose reached the reader"
    assert len(claim.rejected) == 2, "the thrown-away drafts were not recorded"
    assert all("Sarah" in draft for draft in claim.rejected)
    assert claim.faithful, "the template that shipped must itself be faithful"
    del first

    settled = restart_with(tmp_path, "guard", lying, fake).approve(
        parked.run_id, [Decision(claim_index=0, accept=True)]
    )[0]

    resumed = settled.claims[0]
    assert resumed.rejected == claim.rejected, "the restarted run forgot what it threw away"
    assert resumed.faithfulness_violations == claim.faithfulness_violations
    assert resumed.explanation == claim.explanation
    assert resumed.explanation_source == "template"


def restart_with(tmp_path, name: str, replies, fake) -> AuditService:
    """A restart with a different script — same two files, everything else new."""
    checkpoints = sqlite3.connect(
        str(tmp_path / f"{name}-checkpoints.db"), check_same_thread=False
    )
    return AuditService(
        pipeline=Pipeline(
            llm=LLM(client=FakeChat(replies=list(replies))),
            client=fake,
            now=NOW,
            saver=SqliteSaver(checkpoints),
        ),
        store=AuditStore(tmp_path / f"{name}.db"),
        client=fake,
    )


# --- the boundary of the replay, asserted rather than promised ---------------


def test_nothing_a_reader_sees_depends_on_a_step_s_inputs_or_outputs(tmp_path):
    """THE INVARIANT BEHIND THE ONE THING REPLAY DOES NOT REBUILD.

    A step's `inputs`/`outputs` are summaries for a human reading the log (`cached: true`,
    `claims: 3`). They are not persisted, so a REPLAYED step carries them empty — which is
    only safe for as long as nothing a reader sees is computed from them. Today nothing is.
    "Today nothing is" is a fact about the code, and a fact about the code that nobody
    asserts is a fact about the code until the afternoon somebody changes it.

    And the failure would be silent in exactly the way the TLS bug was silent: the moment a
    receipt, a summary or a trajectory rule starts reading `step.outputs`, a RESUMED run
    begins reporting something different from an unrestarted one — on the path a human uses
    to approve a change to the catalog — and every other test stays green, because every
    other test runs in one process and never replays anything.

    So the invariant is asserted the way NO_LLM_IN_THE_VERDICT_PATH is: strip the summaries
    out of a real run's trace and demand that every consumer-facing surface is unmoved. If
    someone makes the report depend on them, this goes red and names why.
    """
    # TWO claims about ONE dataset, deliberately: the second `resolve` records
    # `cached: True`, so the trace carries a step summary that is TRUTHY. A one-claim run
    # would leave every interesting summary falsy, and a consumer that started reading them
    # would produce the same report either way — the test would pass a sabotage it was
    # written to catch. Choose the fixture that can actually go red.
    chat = FakeChat(
        replies=[
            claim_reply([ownership(ALICE), ownership(ALICE)]),
            explanation_reply("the catalog lists a different owner.", "Contradicted", []),
            revision_reply(ownership(CAROL)),
        ],
        tokens=(400, 90),
    )
    fake = catalog()
    report = Pipeline(llm=LLM(client=chat), client=fake, now=NOW).run(SAYS, thread_id="r")

    # Non-vacuity first. If the trace carried no summaries — or only falsy ones — stripping
    # them would prove nothing and this test would be a green light wired to nothing.
    assert any(s.inputs for s in report.trace), "no step recorded any inputs to strip"
    assert any(
        v for s in report.trace for v in s.outputs.values()
    ), "no step recorded a truthy output, so removing them could not change any answer"
    assert any(s.outputs.get("cached") for s in report.trace), "the cache summary is falsy"

    stripped = replace(
        report,
        trace=Trace(
            steps=[replace(s, inputs={}, outputs={}) for s in report.trace]
        ),
    )

    # Every consumer-facing surface there is: the persisted/served projection, the
    # trajectory verdict, the receipts line, and the printable summary.
    # created_at pinned: from_report stamps the wall clock, and two calls are two instants.
    projected = from_report(stripped, run_id="r", created_at=NOW)
    assert projected == from_report(report, run_id="r", created_at=NOW), (
        "the stored/served record changed when a step's log summaries were removed. A "
        "resumed run rebuilds those steps WITHOUT them, so it now reports something an "
        "unrestarted run does not — silently, and only after a restart."
    )
    assert stripped.summary() == report.summary()
    assert stripped.receipts() == report.receipts()
    assert stripped.cost == report.cost
    assert trajectory.verify(
        stripped.trace, _audited(stripped)
    ) == trajectory.verify(report.trace, _audited(report)), (
        "a trajectory rule started reading a step's log summaries. It will pass in the "
        "process that ran the audit and fail in the one that resumes it."
    )


def _audited(report) -> tuple[AuditedClaim, ...]:
    return tuple(
        AuditedClaim(
            index=a.index,
            claim_type=a.claim.claim_type,
            has_verdict=True,
            has_explanation=bool(a.explanation.text),
            correction_attempts=len(a.correction.attempts),
            has_proposal=a.correction.proposal is not None,
        )
        for a in report.audits
    )


# --- and what durable resume still refuses to do -----------------------------


def test_a_run_whose_pause_is_gone_is_a_409_and_not_a_shortcut(tmp_path):
    """The stored evidence is exactly what would make faking the pause feel reasonable.

    The store says this run is awaiting review; the checkpointer has no paused graph for it.
    Everything needed to *look* like a resume is right there — the verdicts, the proposal,
    the evidence — and applying the decision to the record would be four lines. It is still
    a 409, because a correction settled outside the checkpoint node is not settled.
    """
    first = service(tmp_path, "orphan")
    parked = first.audit(SAYS)

    # The checkpoints are gone; the audit history is not. This is what an operator wiping
    # ATTEST_CHECKPOINT_PATH looks like, and what an in-memory saver looks like after a
    # restart.
    orphaned = AuditService(
        pipeline=Pipeline(
            llm=LLM(client=FakeChat(replies=list(SCRIPT))),
            client=catalog(),
            now=NOW,
            saver=SqliteSaver(
                sqlite3.connect(str(tmp_path / "empty.db"), check_same_thread=False)
            ),
        ),
        store=AuditStore(tmp_path / "orphan.db"),
        client=catalog(),
    )

    with pytest.raises(NotResumable, match="no paused graph"):
        orphaned.approve(parked.run_id, [Decision(claim_index=0, accept=True)])

    # And the audit itself is intact and still readable, which is the whole reason the
    # store and the checkpointer are two different things.
    assert orphaned.get(parked.run_id).claims[0].evidence


def test_a_completed_run_deletes_its_checkpoints(tmp_path):
    """A service is a long-lived process, and a checkpoint per audit forever is a leak.

    A run with nothing to review is over the moment it returns, and a run whose proposals
    have been settled is over too. Neither is resumable, so neither keeps a paused graph.
    """
    svc = service(tmp_path, "cleanup")
    parked = svc.audit(SAYS)
    assert svc.pipeline.is_parked(parked.run_id)

    svc.approve(parked.run_id, [Decision(claim_index=0, accept=True)])

    assert not svc.pipeline.is_parked(parked.run_id), "a settled run kept its paused graph"
    # And the audit history is untouched by the cleanup: the RUN is over, not deleted.
    assert svc.get(parked.run_id).claims[0].evidence
