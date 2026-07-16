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

    final = pipeline.resume(
        report.thread_id, [Decision(claim_index=contradicted.index, accept=True)]
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
    assert report.status is RunStatus.COMPLETE, "nothing to propose, so no human is summoned"
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
            approved = http.post(
                f"/audit/{run_id}/approve",
                json={"decisions": [
                    {"claim_index": proposal["index"], "accept": True, "reviewer": "live-test"}
                ]},
            )
            assert approved.status_code == 200, approved.text
            settled = approved.json()

            assert settled["audit"]["status"] == "complete"
            assert len(settled["writebacks"]) == 1
            assert settled["writebacks"][0]["target_urn"] == OWNED_BY_CAROL
            assert settled["writebacks"][0]["ok"] is True, settled["writebacks"][0]
            assert settled["writebacks"][0]["failed_step"] is None

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
                    {"claim_index": p["index"], "accept": True, "reviewer": "live-test"}
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
        evidence="; ".join(f"{e.field}={e.value}" for e in result.evidence if e.value),
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
