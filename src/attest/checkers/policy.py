"""Declared governance policy: the semantics the deterministic core is allowed to use.

This module is where the deterministic/semantic boundary is drawn, explicitly, in
one place. Everything a checker "knows" beyond set membership and arithmetic lives
here as data, so it can be reviewed and argued with — rather than being inferred by
a model at runtime, or worse, smuggled into an `if` somewhere.

Two questions force the issue.

**"Does NonPII contradict PII?"** Nothing in DataHub says so. They are two unrelated
tag URNs; the opposition is a fact about our governance vocabulary, not about the
catalog. A model could infer it from the names, but then a rename would silently
change verdicts and no one could say why one claim was Contradicted and another was
not. So it is declared, in EXCLUSIONS.

**"Does an untagged table contradict 'contains PII'?"** This is the harder one, and
the answer is: normally NO. Tags are open-world — a table tagged Tier1 asserts
nothing about PII, and treating a missing tag as a denial would make Attest cry wolf
on every under-documented entity, which is the failure this project exists to
prevent. Absence of evidence is Insufficient-Coverage.

But that rule alone makes Contradicted unreachable for a table that has genuinely
been reviewed and found clean, which is a real and important case. The way out is to
require the catalog to *say* that its classification is complete, rather than for us
to assume it. A COMPLETENESS_MARKER tag is exactly that statement: "the governance
team reviewed this and tagged what they found." On a table carrying one, a missing
PII tag is a considered denial and closed-world reasoning is licensed. On a table
without one, it is just silence.

So the closed-world assumption is never made by Attest — it is granted by the
catalog, per entity, via a tag someone deliberately applied. That is the whole trick.
"""

from __future__ import annotations

TAG = "urn:li:tag:"

# Labels that positively deny each other. Symmetric; closed under reversal below.
# Only put a pair here if the catalog's own vocabulary means one to EXCLUDE the
# other. "Tier1 vs Tier2" belongs; "PII vs Revenue" does not — a column can be both.
_DECLARED_EXCLUSIONS: list[tuple[str, str]] = [
    (f"{TAG}PII", f"{TAG}NonPII"),
    (f"{TAG}Tier1", f"{TAG}Tier2"),
]

EXCLUSIONS: dict[str, frozenset[str]] = {}
for _a, _b in _DECLARED_EXCLUSIONS:
    EXCLUSIONS.setdefault(_a, set()).add(_b)  # type: ignore[arg-type]
    EXCLUSIONS.setdefault(_b, set()).add(_a)  # type: ignore[arg-type]
EXCLUSIONS = {k: frozenset(v) for k, v in EXCLUSIONS.items()}


# A table carrying one of these asserts that its classification is COMPLETE: someone
# reviewed it and tagged what they found. Only then does a missing tag count as a
# denial rather than as silence. Without this marker, Attest never assumes the
# absence of a tag means anything at all.
COMPLETENESS_MARKERS: frozenset[str] = frozenset({f"{TAG}Verified"})


def contradicts(asserted: str, observed: tuple[str, ...]) -> str | None:
    """The observed label that positively denies `asserted`, if any."""
    excluded = EXCLUSIONS.get(asserted, frozenset())
    for label in observed:
        if label in excluded:
            return label
    return None


def classification_is_complete(table_labels: tuple[str, ...]) -> bool:
    """Has the catalog declared this table's classification exhaustive?"""
    return any(label in COMPLETENESS_MARKERS for label in table_labels)
