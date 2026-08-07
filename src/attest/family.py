"""Does the extracted claim's FAMILY match the sentence it came from?

A deterministic, lexical cross-check on the decomposer's transcription. It decides nothing
about a claim's truth and it never reaches a checker: its whole job is to refuse a claim
whose typed family cannot be what the agent's sentence was about, so that no checker is
handed a question the agent did not ask.

--------------------------------------------------------------------------------
The failure this closes, measured
--------------------------------------------------------------------------------

CLAUDE.md §23, `ext-class-01`, on a catalog Attest did not author. The agent said *"the
cust_email column of X is labelled <term urn>"* — a CLASSIFICATION assertion. The decomposer
returned a SCHEMA claim (`columns: [{name: cust_email}]`) and dropped the term entirely. The
schema checker then answered a different question — *does this column exist?* — said
**Supported**, and that verdict was reported as the answer to a classification claim. It was
right by luck. Had the column not existed it would have been a confident **Contradicted**
about a claim that was never made: a Supported↔Contradicted error, which benchmark/README.md
names the worst thing this product can do.

Nothing upstream could catch it. The URN was quoted verbatim, the claim validated cleanly,
and the checker was correct about the question it was given. The mis-transcription is only
visible by comparing the SENTENCE with the FAMILY, and until now nothing did.

--------------------------------------------------------------------------------
What this is, and what it deliberately is not
--------------------------------------------------------------------------------

It is a **lexical detector**, in the same sense faithfulness.py and polarity.py are, and it
is claimed as no more than that. It cannot tell whether a claim is *right*; it can tell that
a sentence whose only family vocabulary is "labelled / tagged / PII" did not produce a schema
claim. There is no confidence score, no classifier, no second model call, and no rewriting:
a family is never corrected, only refused. Correcting it would put the semantic layer back in
charge of the thing it just got wrong.

**It FAILS OPEN, which is the opposite of the guards around the model's prose, and that is
deliberate.** A rejected claim is a gap in the audit — a claim nobody checked — so the cost
of a false rejection is real and lands on the honest path. So one condition, and only one,
rejects:

    the sentence signals EXACTLY ONE family, and it is not the family extracted.

Anything else goes through. No signal at all: silence is not evidence. Two or more families
signalled: the sentence genuinely spans them and the model's pick is defensible. The
extracted family among them: the sentence supports it.

--------------------------------------------------------------------------------
`column` and `field` are SCOPE, and they signal nothing
--------------------------------------------------------------------------------

The one place this departs from the obvious keyword list, and it is load-bearing rather than
a convenience. `ClassificationClaim.field_path` exists: *"the email column is PII"* is a
column-scoped CLASSIFICATION claim, and *"it has an email column"* is a SCHEMA claim. A word
both families own cannot discriminate between them — it says where the claim points, not what
it asserts. Were `column` a schema signal, the measured `ext-class-01` sentence would signal
{classification, schema}, read as legitimately two-family, and sail straight through the one
condition above.

So the scope terms are named (`SCOPE_TERMS`), excluded on purpose, and pinned by a test —
rather than quietly missing from a list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from attest.claims import ClaimType

# What each family's ASSERTION sounds like. Predicates and their objects, never the scope
# nouns below. Every term is matched whole-word and case-insensitively, so `PII` does not
# match inside `NonPII` (the faithfulness.py rule, one module over) and `day` does not match
# inside `daily` — which is why the inflections are spelled out rather than stemmed.
#
# A term appears under exactly ONE family. A term that two families could own would make the
# signal set less discriminating without making it safer, and the honest way to express
# "this word belongs to nobody" is SCOPE_TERMS.
SIGNALS: dict[ClaimType, tuple[str, ...]] = {
    ClaimType.CLASSIFICATION: (
        "pii",
        "personal data",
        "sensitive",
        "classification",
        "classified",
        "classify",
        "tag",
        "tags",
        "tagged",
        "label",
        "labels",
        "labeled",
        "labelled",
        "glossary",
        "glossaryterm",
        "term",
        "terms",
    ),
    ClaimType.OWNERSHIP: (
        "owner",
        "owners",
        "owned",
        "owns",
        "ownership",
        "steward",
        "stewards",
        "stewarded",
        "stewardship",
        "corpuser",
        "corpgroup",
    ),
    ClaimType.FRESHNESS: (
        "fresh",
        "freshness",
        "stale",
        "update",
        "updates",
        "updated",
        "refresh",
        "refreshes",
        "refreshed",
        "modified",
        "rebuilt",
        "age",
        "recent",
        "recently",
        "minute",
        "minutes",
        "hour",
        "hours",
        "hourly",
        "day",
        "days",
        "daily",
        "nightly",
        "week",
        "weeks",
        "weekly",
        "month",
        "months",
        "monthly",
        "year",
        "years",
    ),
    ClaimType.SCHEMA: (
        "schema",
        "schemas",
        "type",
        "types",
        "typed",
        "datatype",
        "datatypes",
        "data type",
        "data types",
        "native type",
        "exist",
        "exists",
    ),
}

# Words that say WHERE a claim points, not WHAT it asserts. Shared by ClassificationClaim
# (via `field_path`) and SchemaClaim, so they discriminate nothing and license no family.
# Named and tested rather than merely absent — see the module docstring.
SCOPE_TERMS: tuple[str, ...] = ("column", "columns", "field", "fields")

# One whole-word pattern per term. Interior whitespace matches any run of it, so a
# line-wrapped "data\ntype" reads the same as "data type".
_PATTERNS: dict[ClaimType, tuple[tuple[str, re.Pattern[str]], ...]] = {
    family: tuple(
        (term, re.compile(r"\b" + r"\s+".join(map(re.escape, term.split())) + r"\b", re.I))
        for term in terms
    )
    for family, terms in SIGNALS.items()
}

# How much of the refused sentence the diagnostic quotes. Long enough to recognise, short
# enough to read in a log line or a UI row — the payload carries it whole (record.DroppedView).
_QUOTE_CHARS = 140


@dataclass(frozen=True)
class FamilyCheck:
    """Whether a sentence can be about the family it was extracted as, and the words behind it."""

    ok: bool
    claim_type: ClaimType
    # Every family the text signals, in ClaimType order. Empty means the text carries no
    # family vocabulary at all, which is never grounds for a refusal.
    signalled: tuple[ClaimType, ...] = ()
    # The terms that decided a REJECTION. Empty on the way through: an allowed claim is not
    # asked to justify itself.
    matched: tuple[str, ...] = ()
    reason: str = ""


def matches(text: str, family: ClaimType) -> tuple[str, ...]:
    """The terms of one family present in `text`, in declaration order. Pure."""
    return tuple(term for term, pattern in _PATTERNS[family] if pattern.search(text))


def signals(text: str) -> tuple[ClaimType, ...]:
    """Every family `text` carries vocabulary for. Deterministic, in ClaimType order."""
    return tuple(family for family in ClaimType if matches(text, family))


def check(text: str, claim_type: ClaimType) -> FamilyCheck:
    """Can `text` have produced a claim of this family?

    Rejects on exactly one condition — the text signals one family and it is not this one —
    and says which words decided it. See the module docstring for why every other shape,
    including no signal at all, goes through.
    """
    signalled = signals(text)
    if not signalled or claim_type in signalled or len(signalled) > 1:
        return FamilyCheck(ok=True, claim_type=claim_type, signalled=signalled)

    (asserted,) = signalled
    matched = matches(text, asserted)
    quoted = " ".join(text.split())
    if len(quoted) > _QUOTE_CHARS:
        quoted = quoted[:_QUOTE_CHARS] + "..."
    return FamilyCheck(
        ok=False,
        claim_type=claim_type,
        signalled=signalled,
        matched=matched,
        reason=(
            f"family-mismatch: the text asserts {asserted.value} "
            f"({', '.join(matched)}) but it was extracted as {claim_type.value} — "
            f'"{quoted}"'
        ),
    )
