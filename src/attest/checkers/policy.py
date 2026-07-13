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

--------------------------------------------------------------------------------
PII_SIGNALS, and the precedence rule
--------------------------------------------------------------------------------

**"How does Attest decide what is PII?"** By a named list, not a string match. Real
catalogs mark PII in more than one place, and a checker that knows about one of them
certifies the others clean — the single worst verdict this product can return. So the
recognized signals are enumerated in PII_SIGNALS, and ANY ONE of them is sufficient:

  1. the `PII` global tag                       — a governance decision
  2. a glossary term under the `PII` node       — a governance decision, structurally
  3. the `hasPII` custom property, truthy       — an upstream classifier's finding

None of the three is inferred. A term is a PII signal because someone filed it under
the PII node in the catalog's own hierarchy, not because "EmailAddress" reads as
personal — the same discipline as EXCLUSIONS above. `hasPII=false` deliberately fires
NOTHING: a scanner that looked and found nothing is not a review, and absence of
evidence is not evidence of absence. A clean bill needs COMPLETENESS_MARKERS.

**Precedence: the most specific, most authoritative signal wins.** Signals disagree in
real catalogs, and the disagreement is usually meaningful rather than a mistake. Two
rules, in order:

  A. COLUMN over TABLE. A table-level signal never propagates down into a column that
     carries its own classification. `email_campaign_stats` is filed under the
     EmailAddress term — the table is *about* email — while its `recipient_email_hash`
     column is explicitly tagged NonPII, because the one column that held an address
     was de-identified. A claim about that column is answered by that column's tag.
     (The converse also matters: a table tagged PII does not make its `signup_ts`
     column PII. Table-level PII means "somewhere in here", not "everywhere in here".)

  B. Within one grain, an explicit TAG beats an IMPLIED signal. A tag is a
     classification act performed on this entity — NonPII's own definition in the seed
     is "reviewed and confirmed to carry no personal information". A glossary term is a
     coarser statement of subject matter, and a scanner property is an automated guess.
     When a human's classification and a machine's contradict, the human wins; that is
     what review is for.

Both rules are precedence, not suppression: the losing signal is still returned as
evidence, so an explanation can say *why* the conflict resolved the way it did.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

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


# --- PII signals -------------------------------------------------------------
#
# The answer to "how does Attest decide what is PII". Three signals, named, any one
# sufficient. See the module docstring for why each is a declaration rather than an
# inference, and for the precedence rule that resolves them when they disagree.

PII_TAG = f"{TAG}PII"
PII_NODE = "urn:li:glossaryNode:PII"
HAS_PII_PROPERTY = "hasPII"

# What an upstream classifier writes to mean yes. Anything else — including "false" —
# is not a PII signal. It is emphatically not a denial either: see the docstring.
TRUTHY = frozenset({"true", "yes", "1"})


@dataclass(frozen=True)
class PiiSignal:
    """One recognized way a catalog says "there is PII here"."""

    name: str
    # An explicit signal is a classification act (a tag). An implied one is inferred
    # from structure or from a machine (a term's node, a scanner's property). Explicit
    # outranks implied at the same grain — precedence rule B.
    kind: Literal["explicit", "implied"]
    # The DataHub field an explanation should cite. Evidence, not decoration.
    field: str
    description: str


PII_SIGNALS: tuple[PiiSignal, ...] = (
    PiiSignal(
        name="tag",
        kind="explicit",
        field="tags.tags[].tag.urn",
        description=f"The {PII_TAG} global tag.",
    ),
    PiiSignal(
        name="term",
        kind="implied",
        field="glossaryTerms.terms[].term.urn",
        description=f"A glossary term filed under {PII_NODE}.",
    ),
    PiiSignal(
        name="property",
        kind="implied",
        field=f"properties.customProperties[{HAS_PII_PROPERTY}]",
        description=f"The {HAS_PII_PROPERTY} custom property, set truthy.",
    ),
)

SIGNALS_BY_NAME: dict[str, PiiSignal] = {s.name: s for s in PII_SIGNALS}


@dataclass(frozen=True)
class PiiFinding:
    """A signal that fired (or an exclusion that did), and the value that fired it."""

    signal: PiiSignal | None  # None for the NonPII exclusion, which fires no signal
    present: bool  # True: PII is asserted here. False: PII is positively denied here.
    value: str | list[str]
    field: str
    note: str
    # Set when a table-scoped finding came from one of the table's COLUMNS — i.e. the
    # existential case in resolve_pii_at_table. None means the table said it itself.
    column: str | None = None

    @property
    def is_explicit(self) -> bool:
        """Did a human's classification tag produce this? An exclusion always did."""
        return self.signal is None or self.signal.kind == "explicit"


def resolve_pii(
    labels: tuple[str, ...],
    term_parents: Mapping[str, tuple[str, ...]],
    properties: Mapping[str, str],
) -> PiiFinding | None:
    """The most authoritative thing this ONE grain says about PII. None means silence.

    Callers pass one grain at a time — a column's labels, or a table's — which is how
    precedence rule A (column over table) is enforced: a column-scoped claim simply
    never passes the table's labels in. Rule B (explicit over implied) is the ordering
    below: the tag checks come first and return, so a term or a scanner property is
    only consulted when no tag has spoken.
    """
    # --- explicit: the classification act ------------------------------------
    if PII_TAG in labels:
        return PiiFinding(
            signal=SIGNALS_BY_NAME["tag"],
            present=True,
            value=PII_TAG,
            field=SIGNALS_BY_NAME["tag"].field,
            note="Tagged PII.",
        )

    denied_by = contradicts(PII_TAG, labels)
    if denied_by:
        return PiiFinding(
            signal=None,
            present=False,
            value=denied_by,
            field=SIGNALS_BY_NAME["tag"].field,
            note=(
                f"Tagged {denied_by.rsplit(':', 1)[-1]}, which positively denies PII. "
                f"An explicit classification tag outranks any implied signal at the "
                f"same grain."
            ),
        )

    # --- implied: structure, then machine ------------------------------------
    pii_terms = [label for label in labels if PII_NODE in term_parents.get(label, ())]
    if pii_terms:
        return PiiFinding(
            signal=SIGNALS_BY_NAME["term"],
            present=True,
            value=sorted(pii_terms),
            field=SIGNALS_BY_NAME["term"].field,
            note=(
                f"Carries {', '.join(t.rsplit(':', 1)[-1] for t in sorted(pii_terms))}, "
                f"filed under the PII node."
            ),
        )

    raw = properties.get(HAS_PII_PROPERTY)
    if raw is not None and raw.strip().lower() in TRUTHY:
        return PiiFinding(
            signal=SIGNALS_BY_NAME["property"],
            present=True,
            value=raw,
            field=SIGNALS_BY_NAME["property"].field,
            note=f"{HAS_PII_PROPERTY}={raw}: a classifier found personal data here.",
        )

    # hasPII=false lands here, deliberately: a scanner's miss is not a review, so it
    # is silence, not a clean bill. Only COMPLETENESS_MARKERS can license that.
    return None


def resolve_pii_at_table(
    table_labels: tuple[str, ...],
    term_parents: Mapping[str, tuple[str, ...]],
    properties: Mapping[str, str],
    columns: Sequence[tuple[str, tuple[str, ...]]] = (),
) -> PiiFinding | None:
    """What the catalog says about PII in this TABLE, columns included.

    Signals propagate UP and not DOWN, and the asymmetry is not an oversight — it is
    what the signals actually mean.

      UP (here): a table-level PII claim is EXISTENTIAL. "This table contains PII" is
      true if PII is anywhere in it, so a column tagged PII settles it, whatever the
      table-level metadata does or does not say. Without this, a table whose `email`
      column is tagged PII but which nobody tagged at table level would answer "is this
      table PII-free?" with silence — and on a table that also carries a `Verified`
      marker, it would answer SUPPORTED. Certifying a table clean while one of its own
      columns is tagged PII is the worst verdict this product can produce, and it was
      reachable until this function existed.

      DOWN (deliberately not): the same existential reading forbids the converse. "This
      table contains PII" does NOT mean "every column is PII", so a table's PII tag says
      nothing about its untagged `signup_ts` column. Inheriting it downward would mark
      every column of every PII table as personal data — crying wolf on the entire
      warehouse, which is the failure this project exists to prevent. A column-scoped
      claim is therefore answered by that column's own classification, or by silence.
      See check_classification, which passes one grain at a time.

    Precedence within the table scope, most authoritative first:

      1. the table's own explicit PII tag
      2. an explicit PII tag on ANY column   (existential; positive evidence is positive)
      3. the table's explicit NonPII tag     (rule B: explicit outranks implied)
      4. the table's implied signals         (a term under the PII node, or hasPII)
      5. an implied signal on ANY column
      6. silence

    Step 2 sits above step 3 on purpose: a column a human tagged PII outweighs a table
    someone tagged NonPII, because the more specific classification act is the better
    evidence — and because a positive finding of personal data should never be talked out
    of existence by a coarser label.
    """
    table = resolve_pii(table_labels, term_parents, properties)

    column_findings: list[tuple[str, PiiFinding]] = []
    for path, labels in columns:
        found = resolve_pii(labels, term_parents, {})
        if found is not None:
            column_findings.append((path, found))

    def _from_column(explicit: bool) -> PiiFinding | None:
        for path, found in column_findings:
            if found.present and found.is_explicit is explicit:
                return replace(
                    found,
                    column=path,
                    field=f"schemaMetadata.fields[{path}].globalTags + .glossaryTerms",
                    note=f"Column '{path}' carries PII. {found.note}",
                )
        return None

    if table is not None and table.present and table.is_explicit:
        return table

    from_explicit_column = _from_column(explicit=True)
    if from_explicit_column is not None:
        return from_explicit_column

    if table is not None:
        # Either the NonPII exclusion (explicit, absent) or an implied positive signal.
        return table

    return _from_column(explicit=False)
