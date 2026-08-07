"""The claim-family guard: the decomposer may not answer a different question.

The failure this closes was MEASURED, on a catalog Attest did not author (CLAUDE.md §23,
`ext-class-01`). The agent said *"the cust_email column of X is labelled <term urn>"* — a
CLASSIFICATION assertion. The decomposer transcribed it as a SCHEMA claim
(`columns: [{name: cust_email}]`), dropping the term entirely. The schema checker then
answered a question nobody asked — *does this column exist?* — said **Supported**, and that
verdict was reported as the answer to a classification claim. It was RIGHT BY LUCK, which is
the only reason it did not become a wrong answer, and banking those is how a benchmark
flatters a broken decomposer.

Everything here is offline: the pure function has no dependencies at all, and the pipeline
tests drive the real graph with the scripted fake and a fixture catalog.

**The guard is CONSERVATIVE and the tests say so in both directions.** Rejecting a claim is
itself a gap in the audit, so a text that legitimately spans two families, or that carries no
family vocabulary at all, must go through untouched. The rejections here are the ones where
the text says one family and one family only, and the extraction says another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from attest import decompose as decompose_module
from attest import family
from attest.claims import ClaimType, SchemaClaim, Verdict
from attest.datahub import FieldSnapshot
from attest.decompose import decompose
from attest.graph import Pipeline
from attest.llm import LLM
from attest.trajectory import CHECKER
from fakes import FakeCatalog, FakeChat, claim_reply, dataset

CASES = Path(__file__).resolve().parents[1] / "benchmark" / "cases.json"

URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)"
ALICE = "urn:li:corpuser:alice.chen"
TERM = "urn:li:glossaryTerm:EmailAddress"

# The four mismatch directions the guard exists for. Each `text` is unambiguous about ONE
# family — that is what licenses the rejection — and `extracted` is the family the model
# came back with.
CLASSIFICATION_TEXT = (
    f"The cust_email column of {URN} is labelled {TERM} and tagged as PII."
)
OWNERSHIP_TEXT = f"The dataset {URN} is owned by {ALICE}, its steward of record."
FRESHNESS_TEXT = f"The table {URN} was last modified 3 hours ago and is not stale."
SCHEMA_TEXT = f"{URN} has an order_total column of type NUMBER(12,2)."


# --- the pure function -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "extracted", "expected"),
    [
        (CLASSIFICATION_TEXT, ClaimType.SCHEMA, ClaimType.CLASSIFICATION),
        (OWNERSHIP_TEXT, ClaimType.FRESHNESS, ClaimType.OWNERSHIP),
        (FRESHNESS_TEXT, ClaimType.SCHEMA, ClaimType.FRESHNESS),
        (SCHEMA_TEXT, ClaimType.CLASSIFICATION, ClaimType.SCHEMA),
    ],
)
def test_a_text_about_one_family_extracted_as_another_is_rejected(text, extracted, expected):
    """The four directions named in the requirement, each measured on its own text."""
    checked = family.check(text, extracted)

    assert not checked.ok
    assert checked.signalled == (expected,)
    assert checked.matched, "a rejection must name the words it was decided on"
    assert checked.reason.startswith("family-mismatch:")
    # The diagnostic has to be readable by whoever has to act on it: it names both families
    # and quotes the text that was refused.
    assert expected.value in checked.reason
    assert extracted.value in checked.reason


@pytest.mark.parametrize(
    ("text", "extracted"),
    [
        (CLASSIFICATION_TEXT, ClaimType.CLASSIFICATION),
        (OWNERSHIP_TEXT, ClaimType.OWNERSHIP),
        (FRESHNESS_TEXT, ClaimType.FRESHNESS),
        (SCHEMA_TEXT, ClaimType.SCHEMA),
    ],
)
def test_the_right_family_is_always_allowed(text, extracted):
    """All four claim types, transcribed correctly, pass. This is the common case."""
    checked = family.check(text, extracted)

    assert checked.ok
    assert checked.reason == ""


@pytest.mark.parametrize(
    ("label", "text", "extracted"),
    [
        # Genuinely two families in one sentence. Whichever one the model picked, the other
        # is present in the text, so nothing here is evidence of a mis-transcription.
        (
            "ownership + freshness",
            f"{URN} is owned by {ALICE} and is refreshed daily.",
            ClaimType.FRESHNESS,
        ),
        (
            "ownership + freshness, the other way",
            f"{URN} is owned by {ALICE} and is refreshed daily.",
            ClaimType.OWNERSHIP,
        ),
        # A third family the sentence does not mention at all. Two families are already
        # signalled, so the text is not making a single-family assertion and the guard has
        # nothing to be sure about.
        (
            "two signalled, a third extracted",
            f"{URN} is owned by {ALICE} and is refreshed daily.",
            ClaimType.SCHEMA,
        ),
        # THE SCOPE CASE. `column` and `field` are shared vocabulary — a classification claim
        # carries `field_path` — so a column-scoped classification sentence must not read as
        # a schema sentence.
        (
            "column-scoped classification",
            f"The email column of {URN} contains PII.",
            ClaimType.CLASSIFICATION,
        ),
        # No family vocabulary whatsoever. Silence is not evidence.
        ("no signal at all", f"{URN} looks fine to me.", ClaimType.OWNERSHIP),
        # A bare existence claim: its only marker is the shared scope noun.
        (
            "scope noun only",
            f"{URN} has a payload column.",
            ClaimType.SCHEMA,
        ),
        # An empty quotation cannot be evidence of anything.
        ("empty text", "", ClaimType.CLASSIFICATION),
    ],
)
def test_ambiguous_or_silent_text_is_never_rejected(label, text, extracted):
    """Conservatism, pinned. A rejection is itself a gap in the audit."""
    assert family.check(text, extracted).ok, label


def test_scope_words_alone_signal_no_family():
    """`column` / `field` are SCOPE, and the guard is explicit that they license nothing.

    This is the one place the implementation departs from the obvious keyword list, and it
    is not a convenience. `ClassificationClaim.field_path` exists, so a claim about a column
    is as likely to be a classification claim as a schema one — a word both families own
    cannot discriminate between them. Were `column` a schema signal, the measured
    `ext-class-01` mis-transcription would read as a legitimately two-family sentence and
    sail through.
    """
    for word in family.SCOPE_TERMS:
        assert family.signals(f"the {word} of the table") == (), word


def test_pii_is_not_matched_inside_nonpii():
    """Whole words only — the faithfulness.py rule, one module over."""
    assert ClaimType.CLASSIFICATION not in family.signals("the NonPII marker")
    assert ClaimType.CLASSIFICATION in family.signals("the table contains PII")


def test_every_benchmark_case_survives_its_own_label():
    """THE TRACEABILITY PIN: the guard may not move the 40-case benchmark.

    The core benchmark never reaches the decomposer, but the FULL run does, and a guard that
    rejected a case whose extraction was correct would silently turn an EXACT extraction into
    a No-Claim. Every case's own prose is checked against its own labeled family, offline,
    against the committed dataset — so a signal word added carelessly to `family.py` fails
    here rather than in a paid run.
    """
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    assert len(cases) == 40

    rejected = [
        case["id"]
        for case in cases
        if not family.check(case["agent_text"], ClaimType(case["claim"]["claim_type"])).ok
    ]
    assert rejected == []


# --- the boundary: decompose ------------------------------------------------


def _reply_as_schema_claim() -> str:
    """The measured `ext-class-01` shape: classification prose, a schema claim back."""
    return claim_reply(
        [
            {
                "claim_type": "schema",
                "target_urn": URN,
                "raw_text": CLASSIFICATION_TEXT,
                "columns": [{"name": "cust_email", "native_type": None}],
            }
        ]
    )


def test_a_wrong_family_extraction_is_dropped_with_the_text_and_the_reason():
    chat = FakeChat(replies=[_reply_as_schema_claim()])

    result = decompose(CLASSIFICATION_TEXT, llm=LLM(client=chat))

    assert result.claims == ()
    assert len(result.dropped) == 1
    dropped = result.dropped[0]
    assert dropped.reason.startswith("family-mismatch:")
    # The rejected claim's own text survives, whole, in the payload the API and the store
    # already carry (record.DroppedView) — a refusal nobody can inspect is not a finding.
    assert dropped.payload["raw_text"] == CLASSIFICATION_TEXT
    assert dropped.payload["claim_type"] == "schema"


def test_a_correctly_transcribed_claim_is_untouched_by_the_guard():
    """The guard sits on the same path every good claim takes. It must not be felt there."""
    chat = FakeChat(
        replies=[
            claim_reply(
                [
                    {
                        "claim_type": "classification",
                        "target_urn": URN,
                        "raw_text": CLASSIFICATION_TEXT,
                        "labels": [TERM],
                        "present": True,
                        "field_path": "cust_email",
                    }
                ]
            )
        ]
    )

    result = decompose(CLASSIFICATION_TEXT, llm=LLM(client=chat))

    assert result.dropped == ()
    assert len(result.claims) == 1
    assert result.claims[0].claim_type is ClaimType.CLASSIFICATION


# --- the boundary: the pipeline ---------------------------------------------

CATALOG = {
    URN: dataset(
        URN,
        owners=(ALICE,),
        fields=(
            FieldSnapshot(path="cust_email", native_type="VARCHAR(255)", data_type="STRING"),
        ),
    )
}


def _pipeline() -> tuple[Pipeline, FakeChat]:
    chat = FakeChat(replies=[_reply_as_schema_claim()])
    return Pipeline(llm=LLM(client=chat), client=FakeCatalog(CATALOG), max_retries=0), chat


def test_a_wrong_family_claim_never_reaches_a_checker_and_produces_no_verdict():
    """THE INVARIANT. Not "the verdict is right" — that no verdict is reached at all.

    The claim is refused in the decomposer, so it never enters the run's claim list and the
    router never sees it. This is structural rather than disciplinary: there is no code path
    from a dropped claim to `_route_by_claim_type`.
    """
    pipe, _ = _pipeline()

    report = pipe.run(CLASSIFICATION_TEXT)

    assert report.audits == ()
    assert report.by_verdict(Verdict.SUPPORTED) == ()
    assert report.errors == ()
    assert [d.reason.split(":")[0] for d in report.dropped] == ["family-mismatch"]

    ran = {step.name for step in report.trace}
    assert not (ran & set(CHECKER.values())), "no checker may run for a refused claim"
    # And the run is still a well-formed one: refusing a claim is a gap in the audit, not a
    # violation of the pipeline's own architecture.
    assert report.trajectory.ok


def test_the_guard_is_load_bearing__sabotage(monkeypatch):
    """THE VACUITY CHECK. Disable the guard and the measured defect comes straight back.

    Run RED-equivalent by construction: with the guard neutered, the very same input that
    the test above proves is refused now produces a `SchemaClaim`, runs the SCHEMA checker,
    and returns a **Supported** verdict — the schema checker answering *does this column
    exist?* about a sentence that asserted a classification. That is `ext-class-01`,
    reproduced. A guard whose removal changed nothing would be a green light wired to
    nothing.
    """
    monkeypatch.setattr(
        decompose_module.family,
        "check",
        lambda text, claim_type: family.FamilyCheck(
            ok=True, claim_type=claim_type, signalled=(), matched=()
        ),
    )

    pipe, _ = _pipeline()
    report = pipe.run(CLASSIFICATION_TEXT)

    assert report.dropped == ()
    assert len(report.audits) == 1
    audit = report.audits[0]
    assert isinstance(audit.claim, SchemaClaim)
    assert audit.verdict is Verdict.SUPPORTED
    assert CHECKER[ClaimType.SCHEMA] in {step.name for step in report.trace}
