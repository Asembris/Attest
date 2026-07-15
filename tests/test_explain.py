"""Explanation generation, and the two things that stand between a draft and a user.

The model drafts; the faithfulness guard and the cross-check decide whether anyone ever
sees it. What these tests pin is the failure behaviour, because that is where a
groundedness auditor either keeps its promise or quietly stops keeping it: **a rejected
explanation degrades to something TRUE, never to something plausible.**
"""

from __future__ import annotations

from tests.conftest import CAROL, DANA, OWNED_BY_CAROL
from tests.fakes import FakeChat, explanation_reply

from attest.checkers import check_ownership
from attest.claims import OwnershipClaim, Verdict
from attest.explain import explain, template
from attest.llm import LLM

OWNERS_FIELD = "ownership.owners[].owner.urn"

TRUTHFUL = (
    f"The catalog lists {CAROL} as the owner of this dataset. The claim names {DANA}, "
    f"who is not among the owners it lists."
)
HALLUCINATED = "This dataset is owned by Sarah Jennings, who leads the analytics team."


def contradicted(snapshot):
    """support_tickets is carol's; a claim that dana owns it is Contradicted."""
    claim = OwnershipClaim(
        target_urn=OWNED_BY_CAROL,
        raw_text="The support_tickets table is owned by dana.wu.",
        owner_urn=DANA,
    )
    result = check_ownership(claim, snapshot(OWNED_BY_CAROL))
    assert result.verdict is Verdict.CONTRADICTED
    return result


def test_a_faithful_draft_is_used(snapshot):
    result = contradicted(snapshot)
    chat = FakeChat(replies=[explanation_reply(TRUTHFUL, "Contradicted", [OWNERS_FIELD])])

    written = explain(result, llm=LLM(client=chat))

    assert written.source == "model"
    assert written.text == TRUTHFUL
    assert written.conflicts == ()
    assert not written.is_fallback


def test_a_hallucinating_draft_is_rejected_and_repaired(snapshot):
    """One repair attempt, with the violations handed back. Then the draft is used."""
    result = contradicted(snapshot)
    chat = FakeChat(
        replies=[
            explanation_reply(HALLUCINATED, "Contradicted", [OWNERS_FIELD]),
            explanation_reply(TRUTHFUL, "Contradicted", [OWNERS_FIELD]),
        ]
    )

    written = explain(result, llm=LLM(client=chat))

    assert written.source == "model"
    assert written.text == TRUTHFUL
    assert len(written.rejected) == 1
    assert "Sarah" in written.rejected[0]
    # The repair prompt told it what it did wrong, which is the only reason a second
    # attempt is worth more than the first.
    assert "REJECTED" in chat.calls[1]["messages"][-1]["content"]


def test_a_model_that_keeps_hallucinating_falls_back_to_the_template(snapshot):
    """The load-bearing case. Attest would rather be dull than wrong.

    The fallback is not an error path — it is the floor. It is built from the checker's
    own reason and the evidence, so it is true by construction, and it says the same
    thing the model was trying to say, without the invented owner.
    """
    result = contradicted(snapshot)
    chat = FakeChat(
        replies=[
            explanation_reply(HALLUCINATED, "Contradicted", [OWNERS_FIELD]),
            explanation_reply(
                "It is owned by Sarah Jennings and was last checked on 2019-04-02.",
                "Contradicted",
                [OWNERS_FIELD],
            ),
        ]
    )

    written = explain(result, llm=LLM(client=chat))

    assert written.is_fallback
    assert written.text == template(result)
    assert written.faithfulness.ok, "the fallback must pass the guard it fell back to"
    assert len(written.rejected) == 2
    assert "Sarah" not in written.text
    assert CAROL in written.text  # the true owner, from the evidence


def test_a_model_that_disagrees_with_the_verdict_is_overruled_and_surfaced(snapshot):
    """The LLM never overrides a deterministic verdict — and the disagreement is not lost.

    A draft can be perfectly faithful token-by-token and still be arguing with a verdict
    it was told was final. That draft is rejected (shipping it beside the opposite verdict
    would be incoherent), the template is used, and the conflict is reported rather than
    silently resolved in anyone's favour.
    """
    result = contradicted(snapshot)
    chat = FakeChat(
        replies=[explanation_reply(TRUTHFUL, "Supported", [OWNERS_FIELD])]  # faithful, wrong
    )

    written = explain(result, llm=LLM(client=chat))

    assert written.is_fallback, "a conflicted draft must not be shipped"
    assert written.conflicts, "the disagreement must be surfaced"
    assert written.conflicts[0].kind == "verdict-disagreement"
    assert "Supported" in written.conflicts[0].detail
    # And the verdict itself is untouched: the code decided it, and it stands.
    assert result.verdict is Verdict.CONTRADICTED


def test_the_reproduced_fluent_lie_cannot_ship(snapshot):
    """THE COUNTEREXAMPLE, from the external audit. This exact draft shipped as source=model.

    A Contradicted verdict, prose that says "the catalog supports the claim", an HONEST
    self-reported implied_verdict (Contradicted, so crosscheck sees no disagreement), and no
    cited fields (so no uncited-field conflict). Every token in the prose is ordinary, so the
    faithfulness guard passes it clean — and it is the exact opposite of its own verdict.

    Before Session 13 this returned source="model", guard_ok=True, conflicts=(). The polarity
    guard is what makes it impossible now: it degrades to the deterministic template, which
    states the true direction. If this test goes green with the polarity check removed, the
    hole is back.
    """
    result = contradicted(snapshot)
    chat = FakeChat(
        replies=[
            explanation_reply("the catalog supports the claim", "Contradicted", []),
            explanation_reply("the catalog supports the claim", "Contradicted", []),
        ]
    )

    written = explain(result, llm=LLM(client=chat))

    assert written.is_fallback, "an explanation asserting the opposite of its verdict shipped"
    assert written.source == "template"
    assert written.text == template(result)
    assert written.conflicts == (), "the model was honest about its verdict; polarity caught it"
    assert written.polarity is not None and written.polarity.ok, (
        "the template that shipped must itself be polarity-safe"
    )
    assert any("affirm" in r and "deny" in r for r in written.rejected), written.rejected


def test_a_field_the_checker_never_read_is_a_conflict(snapshot):
    """"I concluded this from X" — where X is not among the fields the checker looked at."""
    result = contradicted(snapshot)
    chat = FakeChat(
        replies=[
            explanation_reply(TRUTHFUL, "Contradicted", ["properties.description"]),
            explanation_reply(TRUTHFUL, "Contradicted", ["properties.description"]),
        ]
    )

    written = explain(result, llm=LLM(client=chat))

    assert written.is_fallback
    assert any(c.kind == "uncited-field" for c in written.conflicts)


def test_the_model_only_ever_sees_the_evidence(snapshot):
    """Not the raw catalog, not the snapshot. It cannot reach for a fact it wasn't given."""
    result = contradicted(snapshot)
    chat = FakeChat(replies=[explanation_reply(TRUTHFUL, "Contradicted", [OWNERS_FIELD])])

    explain(result, llm=LLM(client=chat))

    sent = "\n".join(chat.prompts)
    assert CAROL in sent  # the evidence the checker returned
    assert OWNERS_FIELD in sent
    # Nothing from the snapshot that the checker did not put in evidence:
    assert "Tier2" not in sent, "a tag the ownership checker never read must not leak in"
    assert "customer_id" not in sent, "the schema is not evidence for an ownership claim"
