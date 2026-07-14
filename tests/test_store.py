"""The audit history: a run goes in whole and comes back whole, and decisions are events.

Offline and free. What is under test is Attest's own store, not DataHub's.

The round trip is the load-bearing test. A persistence layer that keeps the verdicts and
loses the evidence would pass a naive "did it save" test and would have destroyed the only
thing that makes a verdict worth anything — Attest's whole argument is that you can point
at a verdict and see the catalog field it came from, and that argument does not survive a
lossy write.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from attest.claims import Verdict
from attest.datahub import DatasetSnapshot
from attest.graph import Pipeline
from attest.llm import LLM
from attest.record import (
    AuditRecord,
    ConflictView,
    DroppedView,
    FindingView,
    ViolationView,
    from_report,
)
from attest.report import CorrectionOutcome, Decision, ReviewStatus, RunStatus
from attest.store import AuditStore, StoreError
from fakes import FakeCatalog, FakeChat, claim_reply, dataset, explanation_reply, revision_reply

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
GHOST = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.nope.missing,PROD)"

ALICE = "urn:li:corpuser:alice.chen"
CAROL = "urn:li:corpuser:carol.davis"
PII = "urn:li:tag:PII"

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> AuditStore:
    """A real SQLite file, not :memory: — the on-disk path is the one that ships."""
    with AuditStore(tmp_path / "attest.db") as s:
        yield s


def catalog() -> dict[str, DatasetSnapshot]:
    return {
        SF: dataset(
            SF,
            last_modified=NOW - timedelta(hours=6),
            owners=(CAROL,),
            tags=(PII,),
            terms=(),
            custom_properties={},
        )
    }


def ownership(owner: str, urn: str = SF) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": urn,
        "raw_text": f"{urn} is owned by {owner}.",
        "owner_urn": owner,
    }


def audited(*replies: str, max_retries: int = 2) -> AuditRecord:
    """Run the real pipeline against the fake model and project the report."""
    p = Pipeline(
        llm=LLM(client=FakeChat(replies=list(replies))),
        client=FakeCatalog(catalog()),
        now=NOW,
        max_retries=max_retries,
    )
    report = p.run(f"{SF} is owned by someone.", thread_id="run-1")
    return from_report(report, run_id="run-1", source_agent="analyst-bot")


# --- the round trip ----------------------------------------------------------


def test_a_full_audit_round_trips_through_the_database(store):
    """Everything in, everything out. Evidence included, and that is the point."""
    record = audited(
        claim_reply([ownership(ALICE), ownership(CAROL)]),
        explanation_reply("the catalog lists an owner.", "Contradicted", []),
        revision_reply(ownership(CAROL)),
    )
    store.save(record)
    loaded = store.load("run-1")

    assert loaded == record, "the run did not survive the round trip intact"

    # And spelled out, because `==` on a big object hides which half went missing.
    assert len(loaded.claims) == 2
    assert loaded.source_agent == "analyst-bot"
    assert loaded.status is RunStatus.AWAITING_REVIEW
    assert all(c.evidence for c in loaded.claims), "a verdict came back with no evidence"
    assert loaded.claims[0].correction.outcome is CorrectionOutcome.CORRECTED
    assert loaded.claims[0].correction.proposal["owner_urn"] == CAROL
    assert loaded.claims[0].correction.attempts[0].verdict == "Supported"
    assert loaded.receipts.trajectory_ok
    assert loaded.receipts.rules_checked
    assert loaded.steps, "the step trace is the trajectory's evidence and must persist"


def test_an_uncheckable_claim_survives_as_an_error_and_not_as_a_verdict(store):
    """A bad URN stays out of the verdict tally in the database too.

    Storing a ClaimError as an Insufficient-Coverage row would launder a hallucinated URN
    into a legitimate-looking audit result — and this time it would be laundered somewhere
    permanent, where a dashboard would later count it.
    """
    p = Pipeline(
        llm=LLM(client=FakeChat(replies=[claim_reply([ownership(ALICE, GHOST)])])),
        client=FakeCatalog(catalog()),
        now=NOW,
    )
    report = p.run(f"{GHOST} is owned by {ALICE}.", thread_id="run-e")
    store.save(from_report(report, run_id="run-e"))

    loaded = store.load("run-e")
    assert loaded.claims == ()
    assert len(loaded.errors) == 1
    assert loaded.errors[0].target_urn == GHOST
    assert loaded.verdict_counts() == {}


def test_saving_a_run_twice_replaces_it_rather_than_doubling_it(store):
    """A run is saved when it is created and again when it is reviewed."""
    record = audited(
        claim_reply([ownership(ALICE)]),
        explanation_reply("the catalog lists a different owner.", "Contradicted", []),
        revision_reply(ownership(CAROL)),
    )
    store.save(record)
    store.save(record)

    loaded = store.load("run-1")
    assert len(loaded.claims) == 1
    assert len(loaded.claims[0].evidence) == len(record.claims[0].evidence)
    assert len(loaded.steps) == len(record.steps)


def test_an_unknown_run_is_none_not_an_empty_one(store):
    assert store.load("no-such-run") is None


def test_a_pre_session_5_database_is_refused_at_open_and_not_at_the_first_write(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op, so an old database opens CLEANLY and then
    dies on the first INSERT — inside a running service, on the one path that matters.

    It cannot be migrated either: three columns changed from a rendered string to the
    structure that produced it, and reconstructing the structure from the string would mean
    Attest fabricating the contents of its own audit trail. So it is refused here, by name,
    with what to do about it.
    """
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE claims (run_id TEXT, claim_index INTEGER, conflicts TEXT);"
        "INSERT INTO claims VALUES ('run-1', 0, '[\"uncited-field: cited nonsense\"]');"
    )
    old.commit()
    old.close()

    with pytest.raises(StoreError, match="pre-Session-5"):
        AuditStore(path)


def test_an_unpriced_run_stores_a_null_cost_not_a_zero(store):
    """The cost.py rule, at the persistence layer. NULL is unknown; 0.0 is free."""
    record = audited(
        claim_reply([ownership(CAROL)]),
        explanation_reply("the catalog lists an owner.", "Supported", []),
    )
    unpriced = record.model_copy(
        update={"receipts": record.receipts.model_copy(update={"usd": None})}
    )
    store.save(unpriced)

    assert store.load("run-1").receipts.usd is None


def test_what_a_run_did_WRONG_survives_the_round_trip_too(store):
    """The failure columns, with something actually in them.

    A resumed run is rebuilt from this store (replay.py), so a column that is empty in every
    test is a column nobody has proved works — and these four are exactly the ones that are
    empty on a clean run and load-bearing on a bad one. A step that RAISED, a model that had
    no price, an explanation the guard rejected, and the tokens it was rejected for: all of
    them describe what went wrong, and all of them are on the path a human uses to sign off
    a change to the catalog.
    """
    record = audited(
        claim_reply([ownership(CAROL)]),
        explanation_reply("the catalog lists an owner.", "Supported", []),
    )
    claim = record.claims[0]
    broken = record.model_copy(
        update={
            "claims": (
                claim.model_copy(
                    update={
                        "faithful": False,
                        "faithfulness_violations": (
                            ViolationView(token="Sarah", kind="name"),
                        ),
                        "rejected": ("attempt 1: name 'Sarah' does not appear",),
                        "conflicts": (
                            ConflictView(kind="uncited-field", detail="cited 'nonsense'"),
                        ),
                    }
                ),
            ),
            "steps": tuple(
                s.model_copy(
                    update={
                        "models": ("gpt-9-unreleased",),
                        "error": "DataHubError: the catalog is down",
                    }
                )
                if s.name == "decompose"
                else s
                for s in record.steps
            ),
            "dropped": (DroppedView(reason="urn-not-in-source", payload={"target_urn": "x"}),),
            "injection_findings": (
                FindingView(pattern="verdict-forcing", matched="mark this as Supported"),
            ),
        }
    )
    store.save(broken)
    loaded = store.load("run-1")

    assert loaded == broken, "the record of what went wrong did not survive the round trip"

    # Spelled out, because `==` on a big object hides which half went missing — and each of
    # these is a field a rehydrated run would otherwise report differently from the original.
    decompose = next(s for s in loaded.steps if s.name == "decompose")
    assert decompose.models == ("gpt-9-unreleased",), "an unpriced model came back priceable"
    assert decompose.error, "a step that raised came back clean"
    assert loaded.claims[0].rejected == broken.claims[0].rejected
    assert loaded.claims[0].faithfulness_violations[0].token == "Sarah"
    assert loaded.claims[0].conflicts[0].kind == "uncited-field"
    assert loaded.dropped[0].payload == {"target_urn": "x"}
    assert loaded.injection_findings[0].pattern == "verdict-forcing"


# --- decisions are events ----------------------------------------------------


def test_a_decision_is_appended_and_never_overwrites_the_last_one(store):
    """Re-deciding a claim keeps BOTH rows. Who signed off on what is not editable.

    Overwriting would reproduce, inside Attest's own store, the exact property that
    disqualified DataHub from holding the history in the first place.
    """
    store.save(audited(
        claim_reply([ownership(ALICE)]),
        explanation_reply("the catalog lists a different owner.", "Contradicted", []),
        revision_reply(ownership(CAROL)),
    ))

    store.record_decision("run-1", Decision(0, accept=True, reviewer="dana"), writeback="written")
    store.record_decision("run-1", Decision(0, accept=False, reviewer="sam", note="on reflection"))

    log = store.approvals("run-1")
    assert len(log) == 2, "a decision overwrote an earlier one"
    assert [a.reviewer for a in log] == ["dana", "sam"]
    assert [a.accept for a in log] == [True, False]
    assert log[0].writeback == "written"
    assert log[1].note == "on reflection"


def test_the_history_answers_the_question_it_exists_to_answer(store):
    """"Every contradicted ownership claim this week" — a query, not a scan.

    This is what a last-write-wins structured property on a dataset cannot do, and the
    entire reason Attest keeps a store of its own.
    """
    store.save(audited(
        claim_reply([ownership(ALICE), ownership(CAROL)]),
        explanation_reply("the catalog lists an owner.", "Contradicted", []),
        revision_reply(ownership(CAROL)),
    ))

    hits = store.find_claims(
        verdict=Verdict.CONTRADICTED.value,
        claim_type="ownership",
        since=NOW - timedelta(days=7),
    )
    assert len(hits) == 1
    assert hits[0].target_urn == SF
    assert hits[0].source_agent == "analyst-bot"
    assert hits[0].reason

    # And the filters actually filter, rather than returning everything and hoping.
    assert store.find_claims(verdict=Verdict.SUPPORTED.value, claim_type="freshness") == ()
    assert len(store.find_claims(target_urn=SF)) == 2
    assert store.find_claims(since=NOW + timedelta(days=1)) == ()


def test_a_reviewed_run_stores_the_review_status_it_actually_reached(store):
    """PENDING, ACCEPTED, REJECTED — the resting state is persisted as what it is."""
    p = Pipeline(
        llm=LLM(client=FakeChat(replies=[
            claim_reply([ownership(ALICE)]),
            explanation_reply("the catalog lists a different owner.", "Contradicted", []),
            revision_reply(ownership(CAROL)),
        ])),
        client=FakeCatalog(catalog()),
        now=NOW,
    )
    parked = p.run(f"{SF} is owned by {ALICE}.", thread_id="run-r")
    store.save(from_report(parked, run_id="run-r"))
    assert store.load("run-r").claims[0].correction.review is ReviewStatus.PENDING

    settled = p.resume("run-r", [Decision(claim_index=0, accept=True)])
    store.save(from_report(settled, run_id="run-r"))

    loaded = store.load("run-r")
    assert loaded.claims[0].correction.review is ReviewStatus.ACCEPTED
    assert loaded.status is RunStatus.COMPLETE
    # Accepting a correction does not unsay the original claim.
    assert loaded.claims[0].verdict == Verdict.CONTRADICTED.value
