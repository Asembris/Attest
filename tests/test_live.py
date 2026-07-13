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

import pytest
from tests.conftest import (
    ALICE,
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
from attest.decompose import decompose
from attest.explain import explain
from attest.faithfulness import check as check_faithfulness
from attest.llm import LLM

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
