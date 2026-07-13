"""The explanation-faithfulness guard. Who audits the auditor.

Attest exists to catch AI systems asserting things they cannot back up. If Attest's own
explanations were unverified model prose, the product would be self-undermining — and it
is the sharpest question anyone can ask about it. This module is the answer, and it is
deterministic: no model checks the model.

**The rule.** Every *factual token* in an explanation must appear in the evidence that
produced the verdict. Factual tokens are the things a hallucination is made of — names,
URNs, column names, tag names, dates, numbers, quoted values. Ordinary prose is free:
the model may phrase, order, and connect however it likes. It may not introduce a fact.

**Matching is by contiguous word sequence, not substring.** `PII` does not match inside
`NonPII`, and a hallucinated `customer_email` does not match a catalog that happens to
contain `customer_profile` and an `email` column somewhere else. Getting this wrong
would make the guard a rubber stamp, so the two cases are pinned by tests.

**Derived numbers are rejected — even correct ones.** If the evidence says a table was
last modified `10009.9h` ago, an explanation may not say "417 days ago". The arithmetic
is right, but the guard cannot distinguish a correct derivation from a plausible one,
and a guard that accepts plausible arithmetic accepts hallucinated arithmetic. If we
want a number said, a checker must put it in the evidence. This is the strictness the
design is *for*: the cost of a false rejection is a less fluent sentence (we fall back to
a deterministic template), while the cost of a false acceptance is Attest inventing a
fact — and those costs are not remotely symmetric.

**What it does not catch, stated plainly.** This is a token-level guard. It cannot catch
a fluent lie told entirely in prose ("the catalog disagrees with this claim") that names
no false fact. That hole is closed elsewhere, not here: the verdict itself is decided by
code, never by the model, and crosscheck.py rejects an explanation whose implied verdict
differs from the deterministic one. The guard is one layer of three, and it is the layer
that stops fabricated *specifics*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from attest.claims import CheckResult

# Words that name this system rather than a fact about anyone's data. They cannot be
# hallucinated facts about a catalog entity, so they do not have to appear in evidence.
# Deliberately tiny: every entry is a hole in the guard, and "PII" is emphatically NOT
# here — it is the single most important token in the product.
ALLOWED = frozenset({"datahub", "attest", "gms"})

# A capitalized word is the shape a fabricated name comes in — "owned by Sarah Jennings"
# — and it is lexically identical to a capitalized prose word. There is no clever way to
# tell them apart, so the guard FAILS CLOSED: any capitalized word that is not evidence
# and not on this list is treated as a possible fabricated name, and the explanation is
# rejected.
#
# That is the right way round. A false rejection costs fluency — we fall back to the
# deterministic template, which is still true. A false acceptance costs the product its
# reason to exist. The list is therefore function words and connectives only: it does
# NOT contain domain nouns, because a domain noun that legitimately belongs in an
# explanation is already in the evidence, and one that is not in the evidence is exactly
# what we are hunting.
PROSE = frozenset(
    """
    a an the this that these those it its they them their there here we our you your
    and or but so if in on at by for from with without to of as than then thus hence
    because since while when where which who whom whose what why how
    no not nothing none nobody neither nor never every each all any some both other
    however therefore moreover furthermore nevertheless nonetheless meanwhile instead
    rather still also only just even overall finally notably crucially importantly
    unfortunately consequently additionally conversely given based per note yes
    """.split()
)

# The classes of token a hallucination is made of. Anything not matched here is prose,
# and prose is free — see the module docstring.
_URN = re.compile(r"urn:li:[a-zA-Z]+:\S+?(?=[\s,.;)]|$)")
_QUOTED = re.compile(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'")
_IDENTIFIER = re.compile(r"\b[A-Za-z0-9]+(?:[._][A-Za-z0-9]+)+\b")
_CAMEL = re.compile(r"\b[A-Za-z]*[a-z][A-Z][A-Za-z]*\b")
_ALLCAPS = re.compile(r"\b[A-Z]{2,}\b")
_CAPITALIZED = re.compile(r"\b[A-Z][a-z]+\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class Violation:
    """One factual token in the explanation that the evidence does not support."""

    token: str
    kind: str

    def __str__(self) -> str:
        return f"{self.kind} {self.token!r} does not appear in the evidence"


@dataclass(frozen=True)
class Faithfulness:
    """Whether an explanation is entailed by the evidence, token by token."""

    ok: bool
    violations: tuple[Violation, ...] = ()

    def __bool__(self) -> bool:
        return self.ok

    @property
    def summary(self) -> str:
        return "; ".join(str(v) for v in self.violations)


def _words(text: str) -> list[str]:
    """Lowercased alphanumeric runs. Separators become boundaries, and stay boundaries."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _sequences(result: CheckResult) -> list[list[str]]:
    """Everything the explanation is allowed to assert, as word sequences.

    One sequence per source string rather than one flat bag, so that a token has to
    match something the catalog actually said *contiguously* — `customer_email` cannot be
    assembled out of a `customer_profile` here and an `email` there.

    The claim is in scope as well as the evidence: an explanation has to be able to
    restate what the agent asserted in order to contradict it. The claim is data Attest
    was handed, not something a model made up.
    """
    sources: list[str] = [
        result.reason,  # code-generated, therefore trusted
        result.verdict.value,
        result.claim.model_dump_json(),
    ]
    for evidence in result.evidence:
        sources.append(evidence.field)
        sources.append(str(evidence.value))
        sources.append(evidence.note or "")

    return [_words(s) for s in sources if s]


def _supported(token: str, sequences: list[list[str]]) -> bool:
    """Does this token appear, as a contiguous run of words, in anything we were given?"""
    needle = _words(token)
    if not needle:
        return True  # punctuation-only; nothing is being asserted

    if all(w in ALLOWED or w in PROSE for w in needle):
        return True

    for sequence in sequences:
        for i in range(len(sequence) - len(needle) + 1):
            if sequence[i : i + len(needle)] == needle:
                return True
    return False


def _tokens(explanation: str) -> list[tuple[str, str]]:
    """The factual tokens in an explanation, as (token, kind) pairs."""
    found: list[tuple[str, str]] = []

    for match in _URN.finditer(explanation):
        found.append((match.group(0).rstrip(".,;"), "urn"))
    for match in _QUOTED.finditer(explanation):
        inner = next(g for g in match.groups() if g is not None)
        found.append((inner, "quoted value"))
    for pattern, kind in (
        (_IDENTIFIER, "identifier"),
        (_CAMEL, "name"),
        (_ALLCAPS, "label"),
        (_CAPITALIZED, "name"),
        (_NUMBER, "number"),
    ):
        for match in pattern.finditer(explanation):
            found.append((match.group(0), kind))

    return found


def check(explanation: str, result: CheckResult) -> Faithfulness:
    """Is every factual token in `explanation` present in the evidence for `result`?

    Deterministic. No model is consulted, because a model checking a model is not a
    guard — it is the same failure mode wearing a hat.
    """
    sequences = _sequences(result)

    violations: list[Violation] = []
    seen: set[str] = set()

    for token, kind in _tokens(explanation):
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        if not _supported(token, sequences):
            violations.append(Violation(token=token, kind=kind))

    return Faithfulness(ok=not violations, violations=tuple(violations))
