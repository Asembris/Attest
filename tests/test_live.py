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
            assert settled["writebacks"] == [
                {"target_urn": OWNED_BY_CAROL, "ok": True, "detail": ""}
            ]

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
