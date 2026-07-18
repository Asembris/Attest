"""The semantic layer against a REAL model. Costs money; not part of `just test`.

Run with `just live`. Skipped without an OPENAI_API_KEY.

The offline suite proves the guard catches hallucinations, using a fake model that
hallucinates on demand. It cannot prove the other half: that the guard does not reject
*truthful* explanations so often that the semantic layer is decorative. Only a real model
can answer that, and the first run of this file answered it loudly — 2 of 9 explanations
fell back, and every rejection was a bug in Attest rather than a lie by the model:

  - the explain prompt told the model to say the catalog was "SILENT", and the guard then
    rejected SILENT as an unevidenced capitalised token
  - the model expanded PII to "Personally Identifiable Information", three capitalised
    words that appear nowhere in the evidence
  - the model cited half of a composite field path (`...globalTags` out of
    `...globalTags + .glossaryTerms`) and the cross-check called it a fabrication

All three are fixed. What is asserted here is the invariant, not the rate: whatever the
model returns, what ships is faithful and the verdict is untouched. The fallback rate is
also checked, loosely, because a guard that rejects everything is as useless as one that
rejects nothing.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    ALICE,
    CAROL,
    COLUMN_ONLY_PII,
    DANA,
    DOCUMENTED,
    NO_SCHEMA,
    NO_TIMESTAMP,
    OWNED_BY_CAROL,
    STALE,
    TAG_ONLY_PII,
    UNREVIEWED,
)

from attest import writeback
from attest.api.app import app, get_service
from attest.api.service import AuditService
from attest.checkers import check
from attest.claims import (
    Claim,
    ClassificationClaim,
    ColumnAssertion,
    FreshnessClaim,
    OwnershipClaim,
    SchemaClaim,
    Verdict,
)
from attest.config import settings
from attest.cost import monitoring_projection, project_fetches
from attest.decompose import decompose
from attest.explain import explain
from attest.faithfulness import check as check_faithfulness
from attest.graph import Pipeline
from attest.llm import LLM
from attest.polarity import check as check_polarity
from attest.report import CorrectionOutcome, Decision, ReviewStatus, RunStatus
from attest.retrieval import ClaimQuery, ClaimReader, ReadState
from attest.store import AuditStore
from attest.trajectory import RECHECK, REVISE, Rule
from attest.writeback import AUDIT_RUN, VERDICT

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.openai_api_key, reason="no OPENAI_API_KEY"),
]

PII = "urn:li:tag:PII"


def _claims() -> list[tuple[Claim, Verdict]]:
    """The whole 12-cell matrix — four claim types x three verdicts — with a real model.

    Every cell, because the interesting failures are lopsided: an Insufficient-Coverage
    explanation is by far the hardest one to phrase without introducing a fact (there is
    nothing in the evidence to talk about), and it was the cell that broke first.
    """
    return [
        # --- freshness
        (FreshnessClaim(target_urn=DOCUMENTED, raw_text="customer_profile is refreshed daily.",
                        max_age_hours=24), Verdict.SUPPORTED),
        (FreshnessClaim(target_urn=STALE, raw_text="revenue_daily is refreshed daily.",
                        max_age_hours=24), Verdict.CONTRADICTED),
        (FreshnessClaim(target_urn=NO_TIMESTAMP, raw_text="pipeline_scratch ran in the last day.",
                        max_age_hours=24), Verdict.INSUFFICIENT_COVERAGE),
        # --- ownership
        (OwnershipClaim(target_urn=DOCUMENTED, raw_text="customer_profile is owned by alice.chen.",
                        owner_urn=ALICE), Verdict.SUPPORTED),
        (OwnershipClaim(target_urn=OWNED_BY_CAROL, raw_text="support_tickets is owned by dana.wu.",
                        owner_urn=DANA), Verdict.CONTRADICTED),
        (OwnershipClaim(target_urn=UNREVIEWED, raw_text="raw_events is owned by alice.chen.",
                        owner_urn=ALICE), Verdict.INSUFFICIENT_COVERAGE),
        # --- classification
        (ClassificationClaim(target_urn=DOCUMENTED, raw_text="customer_profile contains PII.",
                             labels=(PII,), present=True), Verdict.SUPPORTED),
        (ClassificationClaim(target_urn=TAG_ONLY_PII, raw_text="hr_headcount contains no PII.",
                             labels=(PII,), present=False), Verdict.CONTRADICTED),
        (ClassificationClaim(target_urn=COLUMN_ONLY_PII, raw_text="audit_log is PII-free.",
                             labels=(PII,), present=False), Verdict.CONTRADICTED),
        (ClassificationClaim(target_urn=UNREVIEWED, raw_text="raw_events contains PII.",
                             labels=(PII,), present=True), Verdict.INSUFFICIENT_COVERAGE),
        # --- schema
        (SchemaClaim(target_urn=DOCUMENTED,
                     raw_text="customer_profile has an email column of type VARCHAR(255).",
                     columns=(ColumnAssertion(name="email", native_type="VARCHAR(255)"),)),
         Verdict.SUPPORTED),
        (SchemaClaim(target_urn=DOCUMENTED, raw_text="customer_profile has an ssn column.",
                     columns=(ColumnAssertion(name="ssn"),)), Verdict.CONTRADICTED),
        (SchemaClaim(target_urn=NO_SCHEMA, raw_text="external_report has a revenue_amount column.",
                     columns=(ColumnAssertion(name="revenue_amount"),)),
         Verdict.INSUFFICIENT_COVERAGE),
    ]


def test_explanations_are_faithful_and_the_verdict_is_never_moved(snapshot, now):
    """The invariant, live: nothing unfaithful ships, and the model changes no verdict."""
    llm = LLM()
    cases = _claims()
    fell_back: list[str] = []

    for claim, expected in cases:
        result = check(claim, snapshot(claim.target_urn), now=now)
        written = explain(result, llm=llm)

        # Whatever came back — model prose or the template — it is entailed by evidence.
        assert written.faithfulness.ok, (
            f"shipped an unfaithful explanation for {claim.raw_text}: "
            f"{written.faithfulness.summary}"
        )
        assert check_faithfulness(written.text, result).ok

        # And it never asserts a direction the verdict did not reach (Session 13): a live
        # explanation that says "supports" beside a Contradicted verdict is the fluent lie
        # this guard exists to stop, and what ships must be free of it.
        assert check_polarity(written.text, result).ok, (
            f"shipped an explanation whose direction contradicts its verdict for "
            f"{claim.raw_text}: {check_polarity(written.text, result).summary}"
        )

        # The deterministic verdict is untouched by anything the model said.
        assert result.verdict is expected, f"the checker moved on {claim.raw_text}"

        if written.is_fallback:
            fell_back.append(f"{claim.raw_text} -> {'; '.join(written.rejected)}")

    # THE FLOOR, and it is deliberately high: at most ONE of the twelve may fall back.
    #
    # A permissive floor is worse than none. The first version of this assertion read
    # `authored >= len(claims) // 2` — a 40% floor — which would have gone green while
    # most explanations silently degraded to templates. The layer would have been
    # decorative and the test would have said everything was fine.
    #
    # This is a floor, not an equality: a single fallback is tolerable (a model has a bad
    # day; the template is still true). Two is a regression, and the fix is to find what
    # the guard rejected and WIDEN THE EVIDENCE — never the guard.
    assert len(fell_back) <= 1, (
        f"{len(fell_back)}/{len(cases)} explanations fell back to the template. The guard "
        f"is rejecting truthful prose. Widen the EVIDENCE, not the guard:\n"
        + "\n".join(fell_back)
    )


def test_a_real_model_extracts_real_claims(snapshot):
    """Decomposition against prose a real agent would actually write."""
    agent_output = (
        f"I checked {DOCUMENTED}. It is owned by {ALICE}, it is refreshed daily, and it "
        f"has an email column of type VARCHAR(255). It contains no PII."
    )

    result = decompose(agent_output, llm=LLM())

    assert result.dropped == (), f"dropped a valid claim: {[str(d) for d in result.dropped]}"
    assert len(result.claims) >= 3
    kinds = {c.claim_type.value for c in result.claims}
    assert {"ownership", "freshness"} <= kinds
    # Every claim points at the URN the agent actually wrote — never an invented one.
    assert all(c.target_urn == DOCUMENTED for c in result.claims)


# --- the whole pipeline, live ------------------------------------------------


def test_the_full_pipeline_end_to_end(client, now, capsys):
    """One real audit run: real model, real catalog, real graph, real receipts.

    The agent's output below is engineered to exercise the pipeline rather than to flatter
    it — a true claim, a false one the catalog can positively correct, and one the catalog
    is silent about. That mix is the point: it is the only way one run reaches a Supported,
    a Contradicted, the self-correction loop, the human checkpoint, and an
    Insufficient-Coverage that must NOT be dragged into the loop.

    What is asserted is the architecture, not the model's prose:
      - the deterministic core decided every verdict (trajectory verification, live)
      - nothing unfaithful shipped
      - the correction was PROPOSED, and it is still pending when the run returns

    The measured receipts are printed, because the README quotes them and a receipt nobody
    ever printed is an estimate wearing a receipt's clothes.
    """
    agent_output = (
        f"I reviewed the warehouse. The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}. "
        f"The dataset {UNREVIEWED} is owned by {ALICE}."
    )

    pipeline = Pipeline(llm=LLM(), client=client, now=now)
    report = pipeline.run(agent_output)

    # --- the pipeline took the path it says it took --------------------------
    assert report.trajectory.ok, (
        f"the live run violated its own architecture: {report.trajectory.summary}"
    )
    assert Rule.NO_LLM_IN_THE_VERDICT_PATH in report.trajectory.checked
    assert Rule.NO_CORRECTION_WITHOUT_RE_VERIFICATION in report.trajectory.checked

    # A real model extracted three ownership claims about three real URNs.
    assert len(report.audits) == 3, [a.claim.raw_text for a in report.audits]
    assert report.errors == (), [e.error for e in report.errors]

    verdicts = {a.claim.target_urn: a.verdict for a in report.audits}
    assert verdicts[DOCUMENTED] is Verdict.SUPPORTED
    assert verdicts[OWNED_BY_CAROL] is Verdict.CONTRADICTED
    assert verdicts[UNREVIEWED] is Verdict.INSUFFICIENT_COVERAGE

    # --- nothing unfaithful shipped, whatever the model wrote ----------------
    for audit in report.audits:
        assert audit.explanation.faithfulness.ok, (
            f"shipped unfaithful prose: {audit.explanation.faithfulness.summary}"
        )

    # --- the self-correction loop ran, and only where it should have ---------
    contradicted = next(a for a in report.audits if a.verdict is Verdict.CONTRADICTED)
    silent = next(
        a for a in report.audits if a.verdict is Verdict.INSUFFICIENT_COVERAGE
    )

    # The catalog is silent, not disagreeing. There is nothing to correct, and dragging it
    # into the loop would be Attest crying wolf on an under-documented entity.
    assert silent.correction.outcome is CorrectionOutcome.NOT_ATTEMPTED

    # A real model, shown the catalog's owners, corrects itself to one of them — and the
    # correction is re-verified by CODE before anyone is asked to look at it.
    assert contradicted.correction.outcome is CorrectionOutcome.CORRECTED, (
        f"the loop did not correct a correctable claim: "
        f"{[str(a) for a in contradicted.correction.attempts]}"
    )
    assert contradicted.correction.proposal.owner_urn == CAROL
    assert contradicted.correction.attempts[0].verdict is Verdict.SUPPORTED

    # --- and it was PROPOSED, never published --------------------------------
    assert report.status is RunStatus.AWAITING_REVIEW
    assert contradicted.correction.review is ReviewStatus.PENDING
    assert len(report.proposals) == 1

    # EVERY claim is decided, not just the correctable one: since Session 15 a run settles
    # only when every VERDICT has been ruled on, and the correction is a separate act on top.
    final = pipeline.resume(
        report.thread_id,
        [
            Decision(
                claim_index=a.index,
                publish=True,
                accept_correction=True if a.index == contradicted.index else None,
                reviewer="live-test",
            )
            for a in report.audits
        ],
    )
    assert final.status is RunStatus.COMPLETE
    assert final.audits[contradicted.index].correction.review is ReviewStatus.ACCEPTED
    # Accepting a correction does not unsay the original claim.
    assert final.audits[contradicted.index].verdict is Verdict.CONTRADICTED

    # --- the receipts, measured ----------------------------------------------
    cost = final.cost
    assert cost.is_known, f"unpriced model in the run: {cost.unpriced_models}"
    assert cost.total_tokens > 0, "a live run that spent no tokens did not happen"
    assert final.latency_ms > 0

    with capsys.disabled():
        print(f"\n\n  RECEIPTS: {final.receipts()}")
        print(f"  PROJECTED: {monitoring_projection()}")
        print(f"  {json.dumps(final.summary(), indent=2, default=str)}")
        print("\n  TRAJECTORY: " + final.trajectory.summary)
        print("  rules exercised: " + ", ".join(r.value for r in final.trajectory.checked))
        print("\n  STEPS:")
        for step in final.trace:
            print(f"    {step}")
        print()


def test_an_unrevisable_claim_is_never_corrected_into_a_different_true_one(client):
    """An UNREVISABLE claim yields no correction — and a real model tries to cheat anyway.

    The case: a schema claim about a column that does not exist. It is Contradicted, and it
    is the one Contradicted verdict in the seed that CANNOT be corrected. A SchemaClaim has
    no `present=False`, so "this table does not have an ssn column" is inexpressible, and
    withdrawing a claim is not revising it. The only honest moves are to stand firm, or to
    fail.

    WHAT A REAL MODEL ACTUALLY DOES, measured over six runs of gpt-4o-mini at temperature=0:

        stood-firm  4/6   set unchanged=true. The honest answer.
        refused     2/6   tried to swap `ssn` for the table's ENTIRE real column list
                          (customer_id, email, full_name, is_active, signup_ts).

    Both are correct outcomes and neither produces a proposal. But look at what the second
    one is: without revise.subject(), those two runs each re-verify SUPPORTED, become a
    CORRECTED proposal, and put a human in front of a green correction for a claim that was
    simply false. A false "has an ssn column" laundered into an unrelated true one, a third
    of the time. The subject rule is not a hypothetical guard against a hypothetical model.

    So this test asserts the property that holds in ALL SIX runs — Attest invents no
    correction for an unrevisable claim — rather than the specific outcome, which is the
    model's to choose. Asserting `stood-firm` alone made this test flake 1-in-3, and a
    flaky assertion on a load-bearing invariant is worse than no assertion: it trains people
    to re-run it. `stood-firm`'s reachability is pinned OFFLINE, deterministically, in
    tests/test_graph.py; what is measured here is that reality gets a vote at all.
    """
    agent_output = f"The dataset {DOCUMENTED} has an ssn column."

    pipeline = Pipeline(llm=LLM(), client=client)
    report = pipeline.run(agent_output)

    audit = next(a for a in report.audits if a.claim.claim_type.value == "schema")
    assert audit.verdict is Verdict.CONTRADICTED

    # THE INVARIANT: an unrevisable claim is not corrected. Nothing is proposed, nothing is
    # re-verified, and the agent stays wrong. Both honest outcomes satisfy it; a swapped
    # column would not, and would have satisfied every other assertion in this file.
    assert audit.correction.outcome in (
        CorrectionOutcome.STOOD_FIRM,
        CorrectionOutcome.REFUSED,
    ), f"an unrevisable claim produced {audit.correction.outcome.value}"
    assert audit.correction.proposal is None, (
        "Attest proposed a correction for a claim that cannot be corrected — almost "
        "certainly by swapping in a column that does exist"
    )
    assert report.trace.named(REVISE), "the agent was never given the chance to revise"
    assert report.trace.named(RECHECK) == [], "nothing was revised, so nothing may be verified"
    # It PARKS, and since Session 15 that is the point rather than an inconvenience. There
    # is no proposal to rule on — the claim is unrevisable — but the VERDICT is a finding,
    # and the most damning one this system produces: the agent was shown the catalog and
    # stood by a false claim. Under the old gate (publication rode on an accepted
    # correction) this exact outcome could never reach the catalog at all.
    assert report.status is RunStatus.AWAITING_REVIEW, (
        "a STOOD_FIRM finding must still reach a human to be published"
    )
    assert audit.awaits_human and not audit.correction.awaits_human
    assert report.trajectory.ok


# --- the service, live -------------------------------------------------------


def test_the_service_end_to_end_against_the_real_catalog(client, now, tmp_path, capsys):
    """One real audit through the real API: submit, retrieve, approve, and write back.

    Real model, real catalog, real database, real HTTP. What is proved here that the
    offline API suite cannot prove:

      - the snapshot cache's saving is REAL against a real DataHub, not against a fake that
        counts its own calls: four claims over two datasets make two GraphQL round trips
      - an approved verdict actually lands on a real dataset as a real structured property,
        readable back out of DataHub — which is the whole claim being made by writing it as
        a structured property rather than as a text blob

    The agent's output below is built to exercise the service rather than flatter it: three
    claims about ONE dataset (so the cache has something to do) and one false claim about
    another that the catalog can positively correct (so there is a proposal to approve).

    NOTE: this test WRITES to the local catalog — `attest.*` structured properties on
    support_tickets. That is the feature. Nothing reads them back except this test, and
    `just seed` restores the catalog from scratch.
    """
    agent_output = (
        f"I reviewed the warehouse. The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"It is refreshed daily. It has an email column of type VARCHAR(255). "
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}."
    )

    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=client, now=now),
        store=AuditStore(tmp_path / "attest.db"),
        client=client,
    )
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as http:
            assert http.get("/health").json()["datahub"] == "up"

            # --- POST /audit -------------------------------------------------
            created = http.post(
                "/audit", json={"agent_output": agent_output, "source_agent": "warehouse-bot"}
            )
            assert created.status_code == 201, created.text
            audit = created.json()
            run_id = audit["run_id"]

            assert audit["receipts"]["trajectory_ok"], audit["receipts"]["trajectory_summary"]
            verdicts = {c["target_urn"]: c["verdict"] for c in audit["claims"]}
            assert verdicts[OWNED_BY_CAROL] == "Contradicted"

            # THE CACHE, MEASURED AGAINST A REAL SERVER. Several claims about one dataset
            # cost one fetch, and every one of them was decided against that one snapshot.
            receipts = audit["receipts"]
            assert receipts["catalog_lookups"] > receipts["catalog_fetches"], (
                "the run made as many catalog round trips as it had claims — the cache did "
                "nothing, or was bypassed"
            )
            assert receipts["catalog_fetches"] == receipts["catalog_entities"] == 2

            # --- nothing has been written, and nothing will be until a human says so
            assert audit["status"] == "awaiting-review"
            proposals = [c for c in audit["claims"] if c["correction"]["review"] == "pending"
                         and c["correction"]["outcome"] == "corrected"]
            assert len(proposals) == 1, "a real model did not correct a correctable claim"
            proposal = proposals[0]
            assert proposal["correction"]["proposal"]["owner_urn"] == CAROL

            # --- GET /audit/{id} ---------------------------------------------
            stored = http.get(f"/audit/{run_id}").json()
            assert stored["run_id"] == run_id
            assert all(c["evidence"] for c in stored["claims"])
            assert stored["steps"], "the trajectory's evidence did not survive the store"

            # --- POST /audit/{id}/approve ------------------------------------
            #
            # EVERY claim is decided, not just the correctable one. Since Session 15 the
            # verdict is what gets published and the correction is a separate act, so a run
            # settles only when every verdict has been ruled on. The correctable claim gets
            # both decisions; the rest get a bare publish.
            approved = http.post(
                f"/audit/{run_id}/approve",
                json={"decisions": [
                    {
                        "claim_index": c["index"],
                        "publish": True,
                        **(
                            {"accept_correction": True}
                            if c["index"] == proposal["index"]
                            else {}
                        ),
                        "reviewer": "live-test",
                    }
                    for c in audit["claims"]
                ]},
            )
            assert approved.status_code == 200, approved.text
            settled = approved.json()

            assert settled["audit"]["status"] == "complete"
            # EVERY verdict reached the catalog, not just the corrected contradiction.
            assert len(settled["writebacks"]) == len(audit["claims"])
            for w in settled["writebacks"]:
                assert w["ok"] is True, f"failed at {w['failed_step']}: {w['detail']}"
            assert OWNED_BY_CAROL in {w["target_urn"] for w in settled["writebacks"]}

        # --- and the verdict is really on the dataset, in DataHub -------------
        written = {
            p["structuredProperty"]["urn"]: [v["stringValue"] for v in p["values"]]
            for p in (client.get_dataset(OWNED_BY_CAROL)["structuredProperties"] or {})
            .get("properties", [])
        }
        assert written[VERDICT.urn] == ["Contradicted"], (
            "the approved verdict never reached the catalog"
        )
        assert written[AUDIT_RUN.urn] == [run_id], (
            "the catalog carries a verdict with no way back to the audit that produced it"
        )
    finally:
        app.dependency_overrides.clear()

    with capsys.disabled():
        r = settled["audit"]["receipts"]
        saved = r["catalog_lookups"] - r["catalog_fetches"]
        print(f"\n\n  SERVICE: audited {len(settled['audit']['claims'])} claims, run {run_id}")
        print(f"  PROJECTED: {project_fetches()}")
        print(
            f"  CATALOG: {r['catalog_lookups']} lookups over {r['catalog_entities']} "
            f"datasets -> {r['catalog_fetches']} fetches ({saved} saved)"
        )
        print(f"  WROTE BACK: {VERDICT.qualified_name}=Contradicted on {OWNED_BY_CAROL}")
        print()


# --- the claim artifact, against the real catalog (Session 15) ----------------


def _await_verdict(client, claim_urn: str, run_id: str | None = None, timeout_s: float = 40.0):
    """Read a claim artifact back, waiting for THIS run's verdict to become readable.

    DataHub serves run events through an eventually-consistent index, so a verdict that was
    definitely written is not readable the instant the mutation returns. Polling keeps the
    assertions about the FEATURE rather than about the lag: a bare sleep is a guess, and
    reading once makes the test flake on a busy server and blame the write.

    `run_id` is what makes this correct rather than merely patient, and it was learned by
    watching this flake. A claim's URN is content-addressed, so re-running these tests
    re-audits the SAME claim and appends to the SAME artifact — which is the feature working.
    But it means the artifact already has a history from earlier runs, so "wait until it has
    a verdict" is satisfied instantly by a STALE one, and the test then asserts against
    another run's event. Waiting for this run's own verdict is the only honest read.
    """
    deadline = time.monotonic() + timeout_s
    artifact = None
    while time.monotonic() < deadline:
        artifact = writeback.read_claim_artifact(client, claim_urn)
        if artifact is not None and artifact.complete:
            if run_id is None or artifact.history[0].audit_run == run_id:
                return artifact
        time.sleep(1)
    return artifact


@pytest.mark.live
def test_two_claims_on_one_dataset_coexist_in_the_catalog(client, now, tmp_path, capsys):
    """THE SHARP QUESTION, answered against real DataHub:

        "Two claims about one dataset are approved. Show me what the next agent inherits
         from DataHub alone."

    Until Session 15 the honest answer was "an opaque run UUID". Verdicts were five
    dataset-level structured properties, and structured properties are last-write-wins: two
    claims about one dataset OVERWROTE each other, and the survivor was a verdict with no
    subject. `attest.verdict = "Contradicted"` cannot say WHICH claim was contradicted.

    This is the proof that it is no longer true, and it is deliberately not "the tests
    pass": it drives the REAL service -- real model, real catalog, real database, real HTTP
    -- approves two DIFFERENT claims about ONE dataset, and then reads the catalog back
    with `dataset.assertions(urn)`, which is the query a second agent would run. No Attest
    state is consulted in the read-back.

    ONLY A LIVE RUN CAN PROVE THIS. The fake has no timeseries aspect, no search index, no
    MCP validation and no notion of an undefined tag -- every mechanism this feature rides
    on is server machinery a fake does not have (the Session 5 rule). The offline tier pins
    the shape of the payload; it cannot pin that the write lands. Two real bugs were found
    exactly here and nowhere else: the unbootstrapped verdict tag, and the fieldPath trap.

    Two ownership claims naming two different owners are two DIFFERENT claims about one
    dataset -- precisely the case that used to collapse to a single property.

    NOTE: this WRITES to the local catalog -- two assertions on support_tickets. That is
    the feature. `just seed` restores the catalog from scratch.
    """
    agent_output = (
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}. "
        f"I also see that {OWNED_BY_CAROL} is owned by {ALICE}."
    )

    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=client, now=now),
        store=AuditStore(tmp_path / "attest.db"),
        client=client,
    )
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as http:
            audit = http.post(
                "/audit",
                json={"agent_output": agent_output, "source_agent": "claim-artifact-test"},
            ).json()
            run_id = audit["run_id"]
            assert audit["receipts"]["trajectory_ok"], audit["receipts"]["trajectory_summary"]

            # Two claims, one dataset. Both wrong, both correctable, so both are on offer.
            proposals = [
                c for c in audit["claims"]
                if c["correction"]["review"] == "pending"
                and c["correction"]["outcome"] == "corrected"
            ]
            assert len(proposals) == 2, (
                f"expected two approvable claims about {OWNED_BY_CAROL}, got "
                f"{[(c['index'], c['claim_type'], c['verdict']) for c in audit['claims']]}"
            )
            assert {p["target_urn"] for p in proposals} == {OWNED_BY_CAROL}

            settled = http.post(
                f"/audit/{run_id}/approve",
                json={"decisions": [
                    {
                        "claim_index": p["index"],
                        "publish": True,
                        "accept_correction": True,
                        "reviewer": "live-test",
                    }
                    for p in proposals
                ]},
            ).json()

        writebacks = settled["writebacks"]
        assert len(writebacks) == 2, writebacks
        for w in writebacks:
            assert w["ok"] is True, f"write-back failed at {w['failed_step']}: {w['detail']}"
        claim_urns = {w["claim_urn"] for w in writebacks}
        assert len(claim_urns) == 2, (
            f"two claims collapsed onto one artifact: {claim_urns}. This is the overwrite "
            "the whole feature exists to end."
        )

        # --- THE READ-BACK. DataHub alone. No Attest state consulted. ----------
        #
        # Wait for THIS run's verdict on each claim before reading the dataset, or the
        # eventually-consistent index serves an earlier run's event and the assertions below
        # test the wrong write. (These tests re-audit the same claims every run and append to
        # the same artifacts — which is the feature, and is why the stale read is reachable.)
        for urn in claim_urns:
            assert _await_verdict(client, urn, run_id=run_id) is not None

        artifacts = {
            a.claim_urn: a for a in writeback.read_dataset_claims(client, OWNED_BY_CAROL)
        }
        for urn in claim_urns:
            assert urn in artifacts, f"{urn} is not on the dataset in DataHub"
            a = artifacts[urn]
            assert a.target_urn == OWNED_BY_CAROL
            assert a.claim_type == "ownership"
            assert a.complete, "the artifact carries no verdict"
            # The verdict is a STORED value, read verbatim -- never inferred from the native
            # result type or from the succeeded/failed rollup, which cannot tell an
            # Insufficient-Coverage claim from a half-written one.
            assert a.verdict == "Contradicted"
            assert a.history[0].audit_run == run_id
            assert a.history[0].reviewer == "live-test"
            # The claim carries what it ASSERTED, so the next agent knows what was claimed.
            assert a.logic["asserted"]["owner_urn"].startswith("urn:li:corpuser:")

        # Two claims, two different asserted owners, both preserved. Neither overwrote the
        # other -- the invariant, stated as data rather than as a hope.
        owners = {artifacts[u].logic["asserted"]["owner_urn"] for u in claim_urns}
        assert owners == {DANA, ALICE}, f"the two claims collapsed: {owners}"
    finally:
        app.dependency_overrides.clear()

    with capsys.disabled():
        print(f"\n\n  {'=' * 74}")
        print("  WHAT THE NEXT AGENT INHERITS FROM DATAHUB ALONE")
        print(f"  dataset.assertions({OWNED_BY_CAROL.split(',')[1]})")
        print(f"  {'=' * 74}")
        for urn in sorted(claim_urns):
            a = artifacts[urn]
            print(f"\n  {a.claim_urn}")
            print(f"    claim_type    : {a.claim_type}   grain: {a.grain}")
            print(f"    asserted      : owner_urn={a.logic['asserted']['owner_urn']}")
            print(f"    attest.verdict: {a.verdict}   (stored verbatim, not inferred)")
            print(f"    reviewer      : {a.history[0].reviewer}")
            print(f"    audit_run     : {a.history[0].audit_run}")
            print(f"    history       : {len(a.history)} verdict(s)")
        print("\n  TWO claims. ONE dataset. Neither overwrote the other.\n")


@pytest.mark.live
def test_the_third_verdict_reaches_the_catalog_as_its_literal_self(client, now, capsys):
    """Insufficient-Coverage survives the round trip to real DataHub, legibly.

    The third verdict is load-bearing (CLAUDE.md 3) and DataHub has no native result type
    for it: `AssertionResultType` is INIT/SUCCESS/FAILURE/ERROR, and `result.error` -- where
    `INSUFFICIENT_DATA` would have gone -- is accepted by the mutation and silently
    discarded. So Insufficient-Coverage projects onto ERROR, which lands in NEITHER the
    passed nor the failed rollup (correct: the catalog being silent is not a pass and not a
    fail), and the verdict itself is carried verbatim in the run event's payload.

    This asserts it comes back as the literal string, from the real server.

    WHY THIS DOES NOT GO THROUGH `/approve`, and it is a real GAP rather than a shortcut:
    only a claim that was CONTRADICTED and successfully CORRECTED is ever offered to a human
    (`correction.awaits_human` is `outcome is CORRECTED and review is PENDING`), and
    Insufficient-Coverage is never sent round the correction loop. So an IC claim -- and a
    Supported one -- can never be approved, and therefore never reaches the catalog at all.
    Measured live: an audit of two claims about one dataset offered exactly one of them for
    approval. That gates the thesis, and it is reported at the Session A checkpoint rather
    than papered over here. This test drives `write_claim_artifact` -- the same function the
    approval path calls, with the same arguments -- to prove the CATALOG leg holds while the
    publication gate is decided.
    """
    claim = {
        "claim_type": "classification",
        "target_urn": UNREVIEWED,
        "raw_text": f"The dataset {UNREVIEWED} contains no PII.",
        "labels": ["urn:li:tag:PII"],
        "present": False,
        "field_path": None,
    }
    # The verdict is the DETERMINISTIC checker's, not a literal: this has to be the real
    # third verdict about a real under-documented dataset, or it proves nothing.
    result = check(ClassificationClaim(**claim), client.fetch_dataset(UNREVIEWED))
    assert result.verdict is Verdict.INSUFFICIENT_COVERAGE, (
        f"{UNREVIEWED} is no longer silent about PII ({result.verdict}); this test needs a "
        "dataset the catalog has not reviewed."
    )

    written = writeback.write_claim_artifact(
        client,
        claim=claim,
        verdict=result.verdict.value,
        run_id="live-third-verdict",
        checked_at=now,
        # STRUCTURED evidence, absence and all (Session 21 gap 2). The IC verdict's evidence
        # is where a None value is most likely, and it must round-trip as None.
        evidence=[{"field": e.field, "value": e.value, "note": e.note} for e in result.evidence],
        reviewer="live-test",
    )
    assert written.ok is True, f"failed at {written.failed_step}: {written.detail}"

    # A VERDICT IS NOT READABLE THE INSTANT IT IS WRITTEN, and this is not the test being
    # generous. `reportAssertionResult` returned true and the tag landed, yet the first read
    # came back with `history=()` — the run event is served through an eventually-consistent
    # index. Found here, on the first live run of this test.
    #
    # It is worth naming rather than sleeping past, because Session B's read path inherits
    # it: a UI that approves a claim and immediately GETs it can legitimately see a claim
    # with no verdict — which is exactly the shape of a HALF-WRITTEN artifact. Those are
    # different facts and the read path must not report the lag as an incomplete write.
    artifact = _await_verdict(client, written.claim_urn)
    assert artifact is not None
    assert artifact.complete is True, "the third verdict must be a VERDICT, not an absence"
    assert artifact.verdict == "Insufficient-Coverage", (
        f"the third verdict came back as {artifact.verdict!r}. It must survive as its "
        "literal self -- a reader cannot recover it from a bucket."
    )
    assert artifact.history[0].native_type == "ERROR"
    assert writeback.verdict_tag_urn("Insufficient-Coverage") in artifact.tags

    with capsys.disabled():
        print("\n\n  THIRD VERDICT, from real DataHub:")
        print(f"    {artifact.claim_urn}")
        print(f"    attest.verdict (stored) : {artifact.verdict!r}")
        print(f"    result.type    (native) : {artifact.history[0].native_type}"
              "   <- lossy projection; NOT what the verdict is read from")
        print(f"    tag                     : {[t for t in artifact.tags if 'Attest-' in t]}")
        print()


@pytest.mark.live
def test_all_three_verdicts_reach_the_catalog_through_the_real_approval_flow(
    client, now, tmp_path, capsys
):
    """THE SESSION B CHECKPOINT. Every verdict type, published by a human, through /approve.

    At the end of Session A this test could not have been written. Publication rode on an
    accepted CORRECTION, so the only thing that ever reached the catalog was a contradiction
    the agent had successfully self-corrected -- one of five contradiction outcomes and zero
    of the other two verdicts. An approved Supported claim and an approved
    Insufficient-Coverage claim were not merely absent from the demo; they were unreachable.

    That is what Option A fixed, and this is the proof, end to end and honest about it:
    nothing here calls `write_claim_artifact`. Everything goes through `POST /audit` and
    `POST /audit/{id}/approve`, exactly as a person would.

    Four claims, one per interesting case:

        Supported              a verdict that could never be published before
        Insufficient-Coverage  ditto -- and the catalog being silent is a FINDING, because
                               "we checked and found nothing recorded" is not the same fact
                               as "nobody ever checked". That distinction is the product.
        Contradicted           the ordinary finding
        Contradicted/unrevisable  the ssn column: the agent is shown the catalog and either
                               STANDS FIRM or refuses. THE case that decided Option A, and
                               the one that used to die silently.

    The outcome of the ssn claim is NOT asserted (CLAUDE.md): a real model chooses between
    stood-firm and refused and both are honest, so asserting one flakes 1-in-3. What is
    asserted is the property that holds every time -- it produces no proposal, and it is
    published anyway.
    """
    agent_output = (
        f"The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"The dataset {UNREVIEWED} contains no PII. "
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}. "
        f"The dataset {DOCUMENTED} has an ssn column."
    )

    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=client, now=now),
        store=AuditStore(tmp_path / "attest.db"),
        client=client,
    )
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as http:
            audit = http.post(
                "/audit", json={"agent_output": agent_output, "source_agent": "session-b"}
            ).json()
            run_id = audit["run_id"]
            assert audit["receipts"]["trajectory_ok"], audit["receipts"]["trajectory_summary"]

            by_verdict = {}
            for c in audit["claims"]:
                by_verdict.setdefault(c["verdict"], []).append(c)
            assert set(by_verdict) == {
                "Supported",
                "Contradicted",
                "Insufficient-Coverage",
            }, f"expected all three verdicts, got { {k: len(v) for k, v in by_verdict.items()} }"

            # EVERY claim parks -- including the ones with nothing to correct. That is the gate.
            assert audit["status"] == "awaiting-review"
            for c in audit["claims"]:
                assert c["publication"]["status"] == "pending"

            # The unrevisable claim: no proposal, whatever the model chose to do.
            ssn = [c for c in audit["claims"] if c["claim_type"] == "schema"]
            assert len(ssn) == 1
            assert ssn[0]["verdict"] == "Contradicted"
            assert ssn[0]["correction"]["proposal"] is None, (
                "Attest proposed a fix for a claim that cannot be fixed -- almost certainly "
                "by swapping in a column that does exist"
            )

            # --- a human publishes every verdict, through the real endpoint -----
            #
            # Publishing a verdict does NOT settle a proposed correction, and this test got
            # that wrong first time: it published all four and the run rightly parked again,
            # because the correctable claim's proposal was still on offer. That is the split
            # working — two acts, two decisions — so a claim carrying a proposal gets both.
            settled = http.post(
                f"/audit/{run_id}/approve",
                json={"decisions": [
                    {
                        "claim_index": c["index"],
                        "publish": True,
                        **(
                            {"accept_correction": True}
                            if c["correction"]["outcome"] == "corrected"
                            else {}
                        ),
                        "reviewer": "session-b",
                    }
                    for c in audit["claims"]
                ]},
            ).json()

        assert settled["audit"]["status"] == "complete"
        assert len(settled["writebacks"]) == len(audit["claims"])
        for w in settled["writebacks"]:
            assert w["ok"] is True, f"failed at {w['failed_step']}: {w['detail']}"

        # --- READ IT BACK FROM DATAHUB. No Attest state consulted. -------------
        published = {}
        for w in settled["writebacks"]:
            artifact = _await_verdict(client, w["claim_urn"], run_id=run_id)
            assert artifact is not None and artifact.complete, (
                f"{w['claim_urn']} carries no verdict in the catalog"
            )
            published[artifact.verdict] = artifact

        assert set(published) == {"Supported", "Contradicted", "Insufficient-Coverage"}, (
            f"only {sorted(published)} reached the catalog"
        )
        for verdict, artifact in published.items():
            assert artifact.history[0].reviewer == "session-b"
            assert artifact.history[0].audit_run == run_id
            assert writeback.verdict_tag_urn(verdict) in artifact.tags
    finally:
        app.dependency_overrides.clear()

    with capsys.disabled():
        print(f"\n\n  {'=' * 74}")
        print("  ALL THREE VERDICTS, PUBLISHED BY A HUMAN, READ BACK FROM DATAHUB")
        print(f"  {'=' * 74}")
        for verdict in ("Supported", "Contradicted", "Insufficient-Coverage"):
            a = published[verdict]
            dataset_name = a.target_urn.split(",")[1]
            print(f"\n  attest.verdict = {a.verdict!r}")
            print(f"    on          : {dataset_name}")
            print(f"    claim       : {a.claim_type}  ({a.logic.get('raw_text','')[:44]}...)")
            print(f"    native type : {a.history[0].native_type}   <- lossy projection")
            print(f"    reviewer    : {a.history[0].reviewer}")
        print("\n  Two of these three could not reach the catalog at all before Option A.\n")


@pytest.mark.live
def test_a_published_verdict_is_RETRIEVABLE_from_the_catalog_by_a_reader_with_no_store(
    client, now, tmp_path, capsys
):
    """THE SESSION 16 CHECKPOINT, and the other half of the thesis.

    Session 15 proved Attest WRITES results back. That is one of Challenge 1's two claims.
    The other is that *the next person or agent inherits the knowledge* — and an artifact
    nobody can retrieve inherits nothing. This is the retrieval half, against real GMS,
    through the real approval flow.

    **The reader here is given NO STORE.** `ClaimReader(client, store=None)` is not a
    convenience — it is the scenario, constructed: a second agent has Attest's catalog and
    not Attest's database, and if the answer needed SQLite the thesis would be false. So
    every claim, verdict, reviewer and history below comes out of DataHub alone.

    What the offline tier cannot do, and why this exists: a fake has no eventually-consistent
    index, so the read-state work is untestable there in the way that matters. This is the
    Session 5 rule — a fake cannot fail in a way the real thing fails through machinery the
    fake does not have — and `just live` is the evidence.
    """
    agent_output = (
        f"The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"The dataset {DOCUMENTED} has an ssn column."
    )

    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=client, now=now),
        store=AuditStore(tmp_path / "attest.db"),
        client=client,
    )
    app.dependency_overrides[get_service] = lambda: service
    try:
        with TestClient(app) as http:
            audit = http.post(
                "/audit", json={"agent_output": agent_output, "source_agent": "session-16"}
            ).json()
            run_id = audit["run_id"]
            assert audit["receipts"]["trajectory_ok"], audit["receipts"]["trajectory_summary"]

            settled = http.post(
                f"/audit/{run_id}/approve",
                json={"decisions": [
                    {
                        "claim_index": c["index"],
                        "publish": True,
                        **(
                            {"accept_correction": True}
                            if c["correction"]["outcome"] == "corrected"
                            else {}
                        ),
                        "reviewer": "session-16",
                    }
                    for c in audit["claims"]
                ]},
            ).json()
            assert settled["audit"]["status"] == "complete"
            for w in settled["writebacks"]:
                assert w["ok"] is True, f"failed at {w['failed_step']}: {w['detail']}"

            # Let this run's verdicts become readable before asserting on the read. The lag
            # is real and measured (~2s median); waiting for THIS run's event rather than
            # any event is what keeps it honest — the artifact is content-addressed, so a
            # previous run's verdict is already sitting there.
            for w in settled["writebacks"]:
                assert _await_verdict(client, w["claim_urn"], run_id=run_id) is not None

            # --- THE THESIS QUERY, through the API ---------------------------
            body = http.get("/claims", params={"target_urn": DOCUMENTED}).json()

        assert body["retrieval"]["entry_point"] == "dataset.assertions"
        assert body["retrieval"]["pushed_down"] == ["target_urn"]

        published = {c["claim_type"]: c for c in body["claims"]}
        assert {"ownership", "schema"} <= set(published), (
            f"the dataset does not carry both claims: {sorted(published)}"
        )
        for claim in body["claims"]:
            assert claim["state"] == "complete", (
                f"{claim['claim_urn']} reads {claim['state']} after its write was confirmed"
            )
            assert claim["verdict"] is not None
            assert claim["asserted"], "a verdict with no subject — the gap this design closed"

        # --- AND NOW WITH NO ATTEST STORE AT ALL. The second agent. ----------
        reader = ClaimReader(client, store=None)
        inherited = reader.list(ClaimQuery(target_urn=DOCUMENTED))

        assert len(inherited.claims) >= 2
        for c in inherited.claims:
            assert c.state is ReadState.COMPLETE, (
                "a settled verdict is not readable without Attest's store — the thesis is "
                "that the CATALOG carries this, and it does not"
            )
            assert c.artifact.verdict is not None
            assert c.artifact.history, "no verdict history survived into the catalog"

        # The cross-catalog entry point: filters, scopes to nothing. Disjoint, measured.
        contradicted = reader.list(ClaimQuery(verdict="Contradicted", claim_type="schema"))
        assert contradicted.retrieval.entry_point == "searchAcrossEntities"
        assert sorted(contradicted.retrieval.pushed_down) == ["claim_type", "verdict"]
        assert any(
            c.artifact.target_urn == DOCUMENTED for c in contradicted.claims
        ), "the ssn contradiction is not findable by a catalog-wide verdict search"
    finally:
        app.dependency_overrides.clear()

    with capsys.disabled():
        print(f"\n\n  {'=' * 74}")
        print("  WHAT A SECOND AGENT INHERITS — from DataHub alone, no Attest store")
        print(f"  {'=' * 74}")
        print(f"\n  ClaimReader(client, store=None).list(target_urn={DOCUMENTED.split(',')[1]})")
        print(f"    -> {len(inherited.claims)} claim artifacts, one GraphQL query\n")
        for c in inherited.claims:
            a = c.artifact
            print(f"    {a.claim_type:16} {str(a.verdict):22} {c.state.value}")
            print(f"      asserts   : {str(a.logic.get('asserted', ''))[:56]}")
            print(f"      history   : {len(a.history)} verdict(s), newest {a.history[0].verdict}")
            print(f"      signed by : {a.history[0].reviewer}")
        print(
            "\n  Every field above came out of the catalog. No Attest process, no SQLite.\n"
        )


# =====================================================================================
# Session 21 — the four inheritance edges, each proven against REAL DataHub. The offline
# tier proves the shapes against a fake; these prove the machinery a fake cannot have —
# the pagination loop, the eventually-consistent index, the real timeseries key. Scratch
# datasets, and the artifacts are content-addressed at fixed timestamps, so re-runs land on
# the same rows rather than growing the catalog.
# =====================================================================================

SCRATCH_PAGE = "urn:li:dataset:(urn:li:dataPlatform:datahub,attest_s21_pagination,PROD)"
SCRATCH_STALE = "urn:li:dataset:(urn:li:dataPlatform:datahub,attest_s21_staletag,PROD)"
SCRATCH_IDENT = "urn:li:dataset:(urn:li:dataPlatform:datahub,attest_s21_identity,PROD)"


@pytest.mark.live
def test_gap1_pagination_returns_everything_past_a_page_and_round_trips_the_total(
    client, now, monkeypatch, capsys
):
    """GAP 1, live: the client's REAL start/count loop over real GMS pages.

    The offline tier proves the retrieval SHAPE against a fake, but a fake has no pages to
    walk, so the pagination LOOP is untestable there (Session 5's rule). The investigation
    seeded 55 artifacts past the real 50-cap and watched the 51st vanish; this pins the fix
    cheaply and idempotently — the page size is dialled down to 2 so a handful of
    content-addressed claims exercises the multi-page loop against real DataHub without
    seeding fifty. Past the page the reader returns EVERYTHING, and the catalog's own total
    rides back so a truncated page can never pass as complete.
    """
    n = 6
    claims = [
        {
            "claim_type": "ownership",
            "target_urn": SCRATCH_PAGE,
            "owner_urn": f"urn:li:corpuser:s21.pageuser{i:02d}",
            "raw_text": f"{SCRATCH_PAGE} is owned by pageuser{i:02d}",
        }
        for i in range(n)
    ]
    urns = set()
    for c in claims:
        w = writeback.write_claim_artifact(
            client, claim=c, verdict="Supported", run_id="live-gap1",
            checked_at=now, reviewer="live-test",
        )
        assert w.ok, f"seed failed at {w.failed_step}: {w.detail}"
        urns.add(w.claim_urn)

    # Wait for the relationship index to show all n before asserting on pagination.
    deadline = time.monotonic() + 40
    total = 0
    while time.monotonic() < deadline:
        _nodes, total = client.list_dataset_assertions(SCRATCH_PAGE, limit=100)
        if total >= n:
            break
        time.sleep(1)
    assert total >= n, f"the index shows {total} of {n} scratch artifacts"

    # Force the multi-page loop: real pages of 2, over real GMS.
    monkeypatch.setattr(type(client), "_ASSERTION_PAGE", 2)
    reader = ClaimReader(client, store=None)

    full = reader.list(ClaimQuery(target_urn=SCRATCH_PAGE), limit=100)
    got = {c.artifact.claim_urn for c in full.claims}
    assert urns <= got, "the multi-page loop dropped an artifact past the first page"
    assert full.retrieval.total >= n
    assert "TRUNCATED" not in full.retrieval.note, "a complete read was reported as truncated"

    truncated = reader.list(ClaimQuery(target_urn=SCRATCH_PAGE), limit=3)
    assert len(truncated.claims) == 3
    assert truncated.retrieval.total >= n > 3, "the catalog's total did not round-trip"
    assert "TRUNCATED" in truncated.retrieval.note, (
        "a page cut off at the limit did not say so — a claim past it would be silently absent"
    )

    with capsys.disabled():
        print("\n\n  GAP 1 — pagination over REAL DataHub (page size forced to 2):")
        print(f"    limit=100 -> {len(full.claims)} claims, total={full.retrieval.total}, "
              "walked multiple pages, nothing dropped")
        print(f"    limit=3   -> {len(truncated.claims)} claims, total="
              f"{truncated.retrieval.total}, TRUNCATED named on the response\n")


@pytest.mark.live
def test_gap2_IC_evidence_and_its_snapshot_round_trip_through_real_datahub(client, now, capsys):
    """GAP 2, live: the third verdict's evidence IS the absence, and it survives round-trip.

    A freshness claim on a dataset the catalog holds no lastModified for is
    Insufficient-Coverage, and its evidence is a single row whose value is None — the
    catalog's silence, which is the whole justification for the verdict. The write-back used
    to flatten evidence to a string and DROP that row, so an IC verdict inherited back with no
    evidence at all. Here the structured evidence and the snapshot identity go to REAL
    DataHub and a store=None reader reconstructs them — the absence decoding back as None
    SPECIFICALLY, not '' and not a missing row.
    """
    claim = {
        "claim_type": "freshness",
        "target_urn": NO_TIMESTAMP,
        "raw_text": f"The dataset {NO_TIMESTAMP} is refreshed daily.",
        "max_age_hours": 24.0,
    }
    snapshot = client.fetch_dataset(NO_TIMESTAMP)
    result = check(FreshnessClaim(**claim), snapshot, now=now)
    assert result.verdict is Verdict.INSUFFICIENT_COVERAGE, (
        f"{NO_TIMESTAMP} now has a timestamp ({result.verdict}); this needs a dataset the "
        "catalog is silent about."
    )
    assert any(e.value is None for e in result.evidence), (
        "the IC freshness evidence carries no absence row — the checker changed shape"
    )

    written = writeback.write_claim_artifact(
        client, claim=claim, verdict=result.verdict.value, run_id="live-gap2-ic",
        checked_at=now,
        evidence=[{"field": e.field, "value": e.value, "note": e.note} for e in result.evidence],
        snapshot_id=snapshot.identity, reviewer="live-test",
    )
    assert written.ok, f"failed at {written.failed_step}: {written.detail}"
    assert _await_verdict(client, written.claim_urn, run_id="live-gap2-ic") is not None

    # store=None: the second agent, reconstructing from the catalog ALONE.
    event = ClaimReader(client, store=None).get(written.claim_urn).artifact.history[0]
    original = {e.field: e.value for e in result.evidence}
    inherited = {e.field: e.value for e in event.evidence}
    assert inherited == original, (
        f"evidence did not round-trip field-by-field: wrote {original}, read {inherited}"
    )
    absent = [e for e in event.evidence if e.value is None]
    assert absent, "no None-valued evidence survived — absence collapsed to empty on the round trip"
    assert absent[0].value is None, "the absence must decode back as None, not '' or []"
    assert event.snapshot_id == snapshot.identity, (
        f"snapshot identity did not survive: wrote {snapshot.identity}, read {event.snapshot_id!r}"
    )

    with capsys.disabled():
        print("\n\n  GAP 2 — IC evidence + snapshot, from REAL DataHub, store=None reader:")
        for e in event.evidence:
            shown = "None (the catalog is SILENT)" if e.value is None else repr(e.value)
            print(f"    {e.field} = {shown}")
        print(f"    snapshot_id = {event.snapshot_id}\n")


@pytest.mark.live
def test_gap3_a_stale_verdict_tag_is_detected_from_real_datahub_with_no_store(client, now, capsys):
    """GAP 3, live: a verdict that flipped without the tag swap, detected on real GMS.

    Report a claim's verdict Supported (tag: Supported), then report a LATER Contradicted
    verdict WITHOUT swapping the tag — the crash-between-report-and-tag shape. On the real
    server the artifact's latest verdict is Contradicted while its tag is still Supported, and
    a store=None reader detects the mismatch from the artifact ALONE. On-read detection, not a
    reconciler — the sweeper is deferred, but a reader is no longer blind to the stale tag in
    front of it.
    """
    claim = {
        "claim_type": "ownership",
        "target_urn": SCRATCH_STALE,
        "owner_urn": "urn:li:corpuser:s21.staletag",
        "raw_text": f"{SCRATCH_STALE} is owned by staletag",
    }
    urn = writeback.claim_urn(claim)

    # A complete write: Supported, tagged Supported.
    w = writeback.write_claim_artifact(
        client, claim=claim, verdict="Supported", run_id="live-gap3-a",
        checked_at=now - timedelta(hours=1), reviewer="live-test",
    )
    assert w.ok, f"seed failed at {w.failed_step}: {w.detail}"
    assert _await_verdict(client, urn, run_id="live-gap3-a") is not None

    # A LATER Contradicted verdict, and the tag deliberately NOT swapped (report only).
    client.report_assertion_result(
        urn, "FAILURE", int(now.timestamp() * 1000),
        {writeback.VERDICT_KEY: "Contradicted", writeback.AUDIT_RUN_KEY: "live-gap3-b"},
    )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        art = writeback.read_claim_artifact(client, urn)
        if art is not None and art.verdict == "Contradicted":
            break
        time.sleep(1)

    got = ClaimReader(client, store=None).get(urn)
    assert got.artifact.verdict == "Contradicted", "the latest run event is the authority"
    assert writeback.verdict_tag_urn("Supported") in got.artifact.tags, "the tag was not left stale"
    assert got.stale_tag, (
        "a store=None reader did not detect the stale tag on real DataHub — the verdict is "
        "Contradicted and the tag still says Supported"
    )

    with capsys.disabled():
        print("\n\n  GAP 3 — stale tag, detected from REAL DataHub with no store:")
        print(f"    latest verdict = {got.artifact.verdict}")
        print(f"    verdict tag    = {[t for t in got.artifact.tags if 'Attest-' in t]}")
        print(f"    stale_tag      = {got.stale_tag}\n")


@pytest.mark.live
def test_gap4_event_identity_retry_dedups_and_a_reaudit_appends_on_real_datahub(client, capsys):
    """GAP 4, live: the append-only history's two load-bearing guarantees, on the REAL
    timeseries.

    Offline the fake models the timeseries key by hand; here real GMS decides. Two guarantees:
      (a) a RETRY — the same verdict re-reported at the run's own timestamp — leaves ONE event.
          A retry that appended would forge a second audit in an append-only log.
      (b) a RE-AUDIT — the same verdict at a LATER timestamp — leaves a SECOND, distinguishable
          event. A re-audit that collapsed would erase the history the artifact exists to hold.

    THE RESIDUAL is documented, not tested as a bug (see docs/architecture.md): two DISTINCT
    runs sharing a start-millisecond and DISAGREEING collapse to one, because the timeseries
    key is (urn, aspect, timestampMillis) and `AssertionResultInput` exposes only that
    (measured — no messageId, runId or partitionSpec). The deterministic core makes it
    unreachable in practice — two runs reach different verdicts only if the catalog changed
    between them, which cannot happen inside their shared millisecond.
    """
    claim = {
        "claim_type": "ownership",
        "target_urn": SCRATCH_IDENT,
        "owner_urn": "urn:li:corpuser:s21.identity",
        "raw_text": f"{SCRATCH_IDENT} is owned by identity",
    }
    urn = writeback.claim_urn(claim)
    # FIXED timestamps: re-runs land on the same two rows (idempotent), not a growing history.
    base = 1_800_000_000_000
    t1 = datetime.fromtimestamp(base / 1000, tz=UTC)
    t2 = datetime.fromtimestamp((base + 60_000) / 1000, tz=UTC)

    # (a) the retry: same verdict, same timestamp, twice.
    for _ in range(2):
        w = writeback.write_claim_artifact(
            client, claim=claim, verdict="Supported", run_id="live-gap4",
            checked_at=t1, reviewer="live-test",
        )
        assert w.ok, f"seed failed at {w.failed_step}: {w.detail}"
    # (b) the re-audit: same verdict, a later timestamp.
    w2 = writeback.write_claim_artifact(
        client, claim=claim, verdict="Supported", run_id="live-gap4",
        checked_at=t2, reviewer="live-test",
    )
    assert w2.ok, f"seed failed at {w2.failed_step}: {w2.detail}"

    # Wait until both timestamps are indexed.
    deadline = time.monotonic() + 40
    ats: list[int] = []
    while time.monotonic() < deadline:
        art = writeback.read_claim_artifact(client, urn)
        ats = [e.at for e in art.history] if art else []
        if base in ats and base + 60_000 in ats:
            break
        time.sleep(1)

    assert ats.count(base) == 1, (
        f"the retry appended a duplicate at the same timestamp: {ats}. The run event must be "
        "keyed by the run's timestamp, so a re-report collapses onto the same row."
    )
    assert base + 60_000 in ats, "the re-audit at a later timestamp did not append a second event"
    assert len([a for a in ats if a in (base, base + 60_000)]) == 2, (
        "the two distinct-timestamp events did not both survive — the append-only history "
        "collapsed a real re-audit"
    )

    with capsys.disabled():
        print("\n\n  GAP 4 — event identity on the REAL timeseries:")
        print("    reported Supported@base TWICE, Supported@base+60s ONCE")
        print(f"    events at base       = {ats.count(base)}  (retry deduped -> 1)")
        print(f"    events at base+60s   = {ats.count(base + 60_000)}  (re-audit appended -> 1)")
        print("    residual same-ms/distinct-verdict collision: documented, un-keyable "
              "(AssertionResultInput = timestampMillis only)\n")
