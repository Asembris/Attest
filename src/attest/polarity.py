"""The polarity guard: an explanation may not assert a direction the verdict did not reach.

The faithfulness guard (faithfulness.py) proves lexical *provenance* — every fabricated-
looking token traces to the evidence. It was never a proof of semantic faithfulness, and its
own docstring said so. What it cannot catch is a fluent lie told entirely in ordinary prose:
"the catalog supports the claim" names no fabricated fact, so every token in it is free, and it
will sit next to a **Contradicted** verdict and pass the guard clean. That hole is not
hypothetical — it was reproduced against the live explanation path, and an explanation that says
the opposite of its own verdict is the exact failure this project exists to catch, printed by
Attest itself.

This module closes it, and it closes it the only way a lexical check honestly can. It does NOT
try to prove the prose entails the verdict — a token filter cannot, and pretending otherwise is
what got us here. It refuses prose that asserts a *direction the verdict never produced*. The
deterministic verdict has a polarity — **Supported affirms, Contradicted denies,
Insufficient-Coverage is silent** — and an explanation is allowed to state that polarity and
only that polarity. Prose that says the catalog "supports" / "confirms" / "agrees" beside a
Contradicted verdict states a polarity the code never reached; it is rejected, and the
deterministic template (explain.template) ships instead.

**This is a guard on the model's PRESENTATION. The polarity that ships is the deterministic
verdict's, always.** The model prose is optional, and this is a gate it must pass to be used at
all: it may rephrase the finding, it may never contradict its direction. It **fails closed** —
an assertion this module cannot confidently read as the verdict's own polarity is treated as a
mismatch, and the true template ships. A duller sentence is the whole cost; a falsifiable one is
what it buys out. The model is free to describe the catalog's facts — "carol is the listed
owner, not dana" asserts no relationship word and passes untouched. What it may not do is
editorialise on whether the claim was right, in a direction the verdict did not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from attest.claims import CheckResult, Verdict


class Polarity(Enum):
    """The direction a verdict — or a sentence — takes on a claim."""

    AFFIRM = "affirm"  # the catalog backs the claim
    DENY = "deny"  # the catalog refutes the claim
    SILENT = "silent"  # the catalog is silent — no coverage, in either direction


# The one true mapping. A verdict's polarity is a fact about the verdict, not about the prose,
# and it is what the prose is held to.
OF_VERDICT: dict[Verdict, Polarity] = {
    Verdict.SUPPORTED: Polarity.AFFIRM,
    Verdict.CONTRADICTED: Polarity.DENY,
    Verdict.INSUFFICIENT_COVERAGE: Polarity.SILENT,
}

# Single-word roots that assert a claim/catalog RELATIONSHIP, and the direction each takes
# before any negation. Deliberately the strong, unambiguous relational verbs — not "match",
# "back" or "align", which carry an innocent factual sense ("the column type matches") that
# would make the guard reject truthful prose. A root missed here is not a hole the way a missed
# fabrication is: an unstated relationship is simply allowed, and the verdict field still
# carries the direction. The danger this catches is the STATED wrong one.
_AFFIRM_ROOTS = frozenset(
    """
    support supports supported supporting
    confirm confirms confirmed confirming
    affirm affirms affirmed affirming
    corroborate corroborates corroborated
    substantiate substantiates substantiated
    validate validates validated validating
    verify verifies verified verifying
    uphold upholds upheld
    agree agrees agreed
    consistent
    """.split()
)
_DENY_ROOTS = frozenset(
    """
    contradict contradicts contradicted contradicting
    refute refutes refuted refuting
    disprove disproves disproved disproven
    rebut rebuts rebutted
    deny denies denied denying
    negate negates negated
    disagree disagrees disagreed
    conflict conflicts conflicted conflicting
    inconsistent contrary
    false incorrect untrue mistaken
    """.split()
)

# Multi-word markers that assert a direction as a phrase. Checked as substrings of the
# normalized (single-spaced, lowercased) text so the words have to be adjacent.
# NB: bare "consistent"/"inconsistent" are handled as word roots below, on word boundaries —
# not as phrases here, because "consistent with the claim" is a substring of "INconsistent with
# the claim" and a substring match would read the denial as an affirmation.
_AFFIRM_PHRASES = (
    "in agreement with",
    "bears out the claim",
    "the claim holds",
    "the claim is correct",
    "the claim is true",
    "the claim is accurate",
    "the claim is valid",
    "checks out",
)
_DENY_PHRASES = (
    "at odds with",
    "contrary to the claim",
    "the claim is false",
    "the claim is incorrect",
    "the claim does not hold",
    "the claim fails",
    "evidence against the claim",
)
# Meta-statements that the catalog has NOTHING to say. These are the honest words for
# Insufficient-Coverage and a misrepresentation next to any other verdict. Kept narrow and
# explicitly about coverage, so an ordinary factual sentence ("dana is not a listed owner")
# is never mistaken for a claim of silence.
_SILENT_PHRASES = (
    "silent",
    "says nothing",
    "no evidence",
    "no information",
    "no coverage",
    "does not cover",
    "insufficient coverage",
    "the catalog is silent",
)

# A negator flips AFFIRM <-> DENY when it sits just before the relational word: "does not
# support" is a denial. Three tokens of reach covers "does not support", "is not consistent",
# "cannot confirm"; more than that and the negation usually belongs to another clause.
_NEGATORS = frozenset(
    "not no never cannot cant nor neither without nothing none fails fail".split()
)
_NEGATION_REACH = 3


@dataclass(frozen=True)
class PolarityViolation:
    """One place the prose asserted a direction the verdict did not produce."""

    asserted: Polarity
    verdict_polarity: Polarity
    trigger: str

    def __str__(self) -> str:
        return (
            f"the explanation asserts {self.asserted.value} (on {self.trigger!r}) but the "
            f"verdict is {self.verdict_polarity.value}"
        )


@dataclass(frozen=True)
class PolarityCheck:
    """Whether the prose stays within the direction the verdict reached."""

    ok: bool
    verdict_polarity: Polarity
    violations: tuple[PolarityViolation, ...] = ()

    def __bool__(self) -> bool:
        return self.ok

    @property
    def summary(self) -> str:
        return "; ".join(str(v) for v in self.violations)


# Identifiers this guard must NOT read as prose. A DataHub tag can be named `Verified`,
# `Confirmed`, `Deprecated` — words that are relational verbs in a sentence but are just names
# inside `urn:li:tag:Verified`. Polarity is a property of the PROSE; the factual tokens
# (URNs, dotted/underscored identifiers) are faithfulness.py's charge, and reading a verb out
# of a tag name is exactly the mistake that would make this guard reject a truthful template.
_URN = re.compile(r"urn:li:\S+")
_IDENTIFIER = re.compile(r"\b[A-Za-z0-9]+(?:[._][A-Za-z0-9]+)+\b")


def _prose(text: str) -> str:
    """The sentence with its identifiers removed, so only prose words remain to be read."""
    text = _URN.sub(" ", text)
    text = _IDENTIFIER.sub(" ", text)
    return text


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z]+", _prose(text).lower())


def _negated(words: list[str], i: int) -> bool:
    """Is the word at index i under a negation within reach? Odd count flips."""
    negators = sum(
        1 for w in words[max(0, i - _NEGATION_REACH) : i] if w in _NEGATORS
    )
    return negators % 2 == 1


# The reach within which an affirm root and a deny root, negated and joined, read as a
# statement of silence rather than of either direction.
_IDIOM_REACH = 5


def _silent_idioms(words: list[str]) -> set[int]:
    """Indices consumed by a "neither confirmed nor denied" construction.

    An affirm root and a deny root, close together, under a negation and joined by or/nor,
    assert that the catalog says NOTHING — "can be neither confirmed nor denied", "cannot be
    confirmed or denied". Both verbs are present, but the construction is SILENT, not a
    contradictory pair of directions. This is exactly how the deterministic checkers phrase
    Insufficient-Coverage, so a model that echoes them must not be misread. Handling it here,
    rather than by flipping each verb under negation, is the difference between reading the
    idiom and mangling it.
    """
    affirms = [i for i, w in enumerate(words) if w in _AFFIRM_ROOTS]
    denies = [i for i, w in enumerate(words) if w in _DENY_ROOTS]
    consumed: set[int] = set()
    for a in affirms:
        for d in denies:
            lo, hi = min(a, d), max(a, d)
            if hi - lo > _IDIOM_REACH:
                continue
            joined = "or" in words[lo:hi] or "nor" in words[lo:hi]
            negated = any(w in _NEGATORS for w in words[max(0, lo - 3) : hi + 1])
            if joined and negated:
                consumed.add(a)
                consumed.add(d)
    return consumed


def _asserted_polarities(explanation: str) -> list[tuple[Polarity, str]]:
    """Every direction the prose asserts, with the word or phrase that asserted it.

    Order does not matter and duplicates are fine — the caller only asks whether any asserted
    polarity differs from the verdict's.
    """
    found: list[tuple[Polarity, str]] = []
    normalized = " ".join(_words(explanation))

    for phrase in _SILENT_PHRASES:
        if phrase in normalized:
            found.append((Polarity.SILENT, phrase))
    for phrase in _AFFIRM_PHRASES:
        if phrase in normalized:
            found.append((Polarity.AFFIRM, phrase))
    for phrase in _DENY_PHRASES:
        if phrase in normalized:
            found.append((Polarity.DENY, phrase))

    words = _words(explanation)
    idiom = _silent_idioms(words)
    if idiom:
        found.append((Polarity.SILENT, "neither confirmed nor denied"))

    for i, word in enumerate(words):
        if i in idiom:
            continue
        base: Polarity | None = None
        if word in _AFFIRM_ROOTS:
            base = Polarity.AFFIRM
        elif word in _DENY_ROOTS:
            base = Polarity.DENY
        if base is None:
            continue
        if _negated(words, i):
            base = Polarity.DENY if base is Polarity.AFFIRM else Polarity.AFFIRM
        found.append((base, word))

    return found


def check(explanation: str, result: CheckResult) -> PolarityCheck:
    """Does the explanation assert only the direction the verdict reached?

    Deterministic. The verdict's polarity is fixed by the code that produced it; the prose is
    measured against it and never the other way round.
    """
    verdict_polarity = OF_VERDICT[result.verdict]

    violations = tuple(
        PolarityViolation(
            asserted=asserted, verdict_polarity=verdict_polarity, trigger=trigger
        )
        for asserted, trigger in _asserted_polarities(explanation)
        if asserted is not verdict_polarity
    )
    return PolarityCheck(
        ok=not violations, verdict_polarity=verdict_polarity, violations=violations
    )
