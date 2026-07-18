"""CLASSIFICATION: does the catalog attach the asserted tags/glossary terms?

This is the checker where the three verdicts are hardest to keep apart, and where
getting them wrong does the most damage. Its rules, in order:

  1. The label is attached                -> Supported
  2. A label that EXCLUDES it is attached -> Contradicted   (PII claimed, NonPII tagged)
  3. Nothing is attached at this scope    -> Insufficient-Coverage
  4. Something is attached, but not this label, and nothing excludes it:
       - the table is marked classification-complete -> Contradicted
       - otherwise                                   -> Insufficient-Coverage

Rule 4 is the entire argument. Tags are open-world: a table tagged Tier1 says
nothing whatever about PII, and reading a missing tag as a denial would flag every
under-documented table in the catalog — which is most of them. So the default is
silence, not disagreement. Closed-world reasoning is only applied where the catalog
has explicitly declared its classification exhaustive (see policy.COMPLETENESS_MARKERS);
Attest never assumes that on its own.

The `present=False` direction ("this table is PII-free") is not the mirror image, and
rule 3 is why. An untagged table cannot SUPPORT "PII-free" — nobody has looked. A
naive checker returns Supported there, because it finds no PII tag and calls the
claim confirmed. That is precisely the false assurance a groundedness auditor exists
to prevent: it would certify an unreviewed table as clean.
"""

from __future__ import annotations

from attest.checkers import policy
from attest.checkers.base import result, worst
from attest.claims import CheckResult, ClassificationClaim, Evidence, Verdict
from attest.datahub import DatasetSnapshot

TABLE_FIELD = "tags.tags[].tag.urn + glossaryTerms.terms[].term.urn"
SCHEMA_FIELD = "schemaMetadata.fields"


def _field_scope(
    claim: ClassificationClaim, snapshot: DatasetSnapshot
) -> tuple[tuple[str, ...], Evidence] | CheckResult:
    """Resolve a column-scoped claim to that column's labels, or bail with a verdict."""
    assert claim.field_path is not None

    if snapshot.fields is None:
        return result(
            claim,
            Verdict.INSUFFICIENT_COVERAGE,
            f"The catalog holds no schema for this dataset, so it cannot say whether "
            f"column '{claim.field_path}' is classified.",
            Evidence(field=SCHEMA_FIELD, value=None, note="Schema aspect absent."),
        )

    column = snapshot.field(claim.field_path)
    if column is None:
        # The schema is exhaustive, so this is a denial, not silence: the claim is
        # about a column the catalog positively says does not exist.
        return result(
            claim,
            Verdict.CONTRADICTED,
            f"The claim is about column '{claim.field_path}', which does not exist. "
            f"The schema lists: {', '.join(f.path for f in snapshot.fields)}.",
            Evidence(
                field=f"{SCHEMA_FIELD}[].fieldPath",
                value=[f.path for f in snapshot.fields],
                note=f"'{claim.field_path}' is absent from an exhaustive schema.",
            ),
        )

    return column.labels, Evidence(
        field=f"schemaMetadata.fields[{claim.field_path}].globalTags + .glossaryTerms",
        # None, not [] — an unclassified column is the catalog holding nothing, and
        # Evidence spells "nothing" as None. An empty list would read as a value.
        value=list(column.labels) or None,
        note=(
            f"Column '{claim.field_path}' carries {len(column.labels)} label(s)."
            if column.labels
            else f"Column '{claim.field_path}' is unclassified."
        ),
    )


def _decide_pii(
    claim: ClassificationClaim,
    where: str,
    finding: policy.PiiFinding | None,
    observed: tuple[str, ...],
    complete: bool,
    completeness_evidence: Evidence,
) -> tuple[Verdict, str, list[Evidence]]:
    """Decide a PII claim from the resolved signal.

    Structurally identical to the four rules above — a signal firing plays the part
    that an attached label plays for any other classification. `finding is None` is the
    silent case, and it is the one that must not be mistaken for a denial: no signal
    fired, so nobody has said this table is clean. Only a completeness marker can.
    """
    evidence: list[Evidence] = []

    if finding is not None:
        evidence.append(
            Evidence(field=finding.field, value=finding.value, note=finding.note)
        )
        signal = f" ({finding.signal.description})" if finding.signal else ""

        if finding.present:
            verdict = Verdict.SUPPORTED if claim.present else Verdict.CONTRADICTED
            phrasing = (
                f"{where} carries PII{signal.rstrip('.')}."
                if claim.present
                else f"{where} carries PII{signal.rstrip('.')}, contradicting the claim "
                f"that it is PII-free."
            )
            return verdict, f"{phrasing} {finding.note}", evidence

        # An explicit NonPII tag: PII is positively denied at this grain.
        verdict = Verdict.CONTRADICTED if claim.present else Verdict.SUPPORTED
        phrasing = (
            f"{where} is explicitly tagged NonPII, so the claim that it contains PII "
            f"is contradicted."
            if claim.present
            else f"{where} is explicitly tagged NonPII, affirming the claim."
        )
        return verdict, f"{phrasing} {finding.note}", evidence

    # --- no signal fired: the catalog is silent about PII at this grain -------
    if complete:
        evidence.append(completeness_evidence)
        verdict = Verdict.CONTRADICTED if claim.present else Verdict.SUPPORTED
        reason = (
            f"No PII signal on {where}, and the table is marked "
            f"classification-complete — so the omission is a reviewed finding, not an "
            f"oversight."
        )
        return verdict, reason, evidence

    evidence.append(completeness_evidence)
    unclassified = "is unclassified" if not observed else "carries no PII signal"
    return (
        Verdict.INSUFFICIENT_COVERAGE,
        f"{where} {unclassified}, and the table is not marked classification-complete. "
        f"None of the recognized PII signals fired "
        f"({', '.join(s.name for s in policy.PII_SIGNALS)}), but absence of a signal is "
        f"not evidence of absence — nobody has reviewed it.",
        evidence,
    )


def check_classification(
    claim: ClassificationClaim, snapshot: DatasetSnapshot
) -> CheckResult:
    if claim.field_path is not None:
        scope = _field_scope(claim, snapshot)
        if isinstance(scope, CheckResult):
            return scope
        observed, observed_evidence = scope
        where = f"column '{claim.field_path}'"
    else:
        observed = snapshot.labels
        observed_evidence = Evidence(
            field=TABLE_FIELD,
            # None, not [] — see _field_scope. An unclassified table has no value here.
            value=list(observed) or None,
            note=(
                f"Table carries {len(observed)} label(s)."
                if observed
                else "Table has no tags and no glossary terms."
            ),
        )
        where = "this table"

    # The completeness marker is always read from the TABLE, even for a column-scoped
    # claim: it is a statement about the review that was performed on the dataset, and a
    # reviewed table's untagged column was reviewed too.
    #
    # That last clause used to be this comment and nothing else — a governance rule living
    # in an `if` inside a checker, which is what policy.py exists to prevent. It is now
    # DECLARED (policy.COMPLETENESS_REACHES_COLUMNS), because the benchmark's independent
    # cross-family labeler read the whole declared policy, could not find this rule in it,
    # and correctly concluded something different. See policy.py.
    complete = policy.classification_is_complete(snapshot.labels) and (
        claim.field_path is None or policy.COMPLETENESS_REACHES_COLUMNS
    )
    completeness_evidence = Evidence(
        field=TABLE_FIELD,
        value=sorted(policy.COMPLETENESS_MARKERS & set(snapshot.labels)) or None,
        note=(
            "Table is marked classification-complete, so an absent label is a denial."
            if complete
            else "Table is not marked classification-complete, so an absent label is "
            "silence, not denial."
        ),
    )

    # PII is resolved by signal, not by exact URN match: three recognized signals, any
    # one sufficient (policy.PII_SIGNALS). Which grains a claim can see is the whole of
    # the precedence rule, and it is asymmetric on purpose.
    #
    #   A column-scoped claim sees ONLY that column. A table-level signal never
    #   propagates down: "this table contains PII" means *somewhere in it*, not in every
    #   column of it, so it says nothing about an untagged `signup_ts`.
    #
    #   A table-scoped claim sees the table AND its columns. A table-level PII claim is
    #   existential, so a column tagged PII settles it — otherwise a table nobody tagged
    #   at table level could be certified PII-free while its own `email` column carries
    #   the tag. See policy.resolve_pii_at_table.
    if claim.field_path is not None:
        pii_finding = policy.resolve_pii(
            labels=observed,
            term_parents=snapshot.term_parents,
            properties={},
        )
    else:
        pii_finding = policy.resolve_pii_at_table(
            table_labels=snapshot.labels,
            term_parents=snapshot.term_parents,
            properties=snapshot.custom_properties or {},
            columns=tuple((f.path, f.labels) for f in snapshot.fields or ()),
        )

    verdicts: list[Verdict] = []
    reasons: list[str] = []
    evidence: list[Evidence] = [observed_evidence]

    for label in claim.labels:
        short = label.rsplit(":", 1)[-1]

        if label == policy.PII_TAG:
            verdict, reason, found = _decide_pii(
                claim, where, pii_finding, observed, complete, completeness_evidence
            )
            verdicts.append(verdict)
            reasons.append(reason)
            evidence.extend(found)
            continue

        attached = label in observed
        excluded_by = policy.contradicts(label, observed)

        # `complete` is checked BEFORE `not observed` in both arms, and the order is
        # load-bearing (Session 23, Hole 3). At TABLE grain the two are mutually exclusive
        # — a complete table carries the Verified marker, so `observed` is non-empty — so
        # the order is inert there. At COLUMN grain `observed` is the COLUMN's labels while
        # `complete` derives from the TABLE's, so an unclassified column of a Verified table
        # has empty `observed` AND `complete` True. Checking `not observed` first sent it to
        # Insufficient-Coverage, diverging from the PII path (_decide_pii, which honors
        # completeness) and from the declared policy.COMPLETENESS_REACHES_COLUMNS. A column
        # the review did not tag is a column it reviewed and found clean — for EVERY label,
        # not only PII.
        if claim.present:
            if attached:
                verdicts.append(Verdict.SUPPORTED)
                reasons.append(f"{short} is attached to {where}.")
            elif excluded_by:
                verdicts.append(Verdict.CONTRADICTED)
                reasons.append(
                    f"{short} is not attached to {where}; it carries "
                    f"{excluded_by.rsplit(':', 1)[-1]}, which excludes it."
                )
                evidence.append(
                    Evidence(
                        field=TABLE_FIELD if claim.field_path is None else SCHEMA_FIELD,
                        value=excluded_by,
                        note=f"{excluded_by.rsplit(':', 1)[-1]} positively denies {short}.",
                    )
                )
            elif complete:
                verdicts.append(Verdict.CONTRADICTED)
                reasons.append(
                    f"{short} is not attached to {where}, and the table is marked "
                    f"classification-complete — so the omission is deliberate."
                )
                evidence.append(completeness_evidence)
            elif not observed:
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(f"{where} is unclassified, so {short} cannot be confirmed.")
            else:
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(
                    f"{short} is not attached to {where}, but the table is not marked "
                    f"classification-complete — the catalog is silent, not disagreeing."
                )
                evidence.append(completeness_evidence)
        else:  # claim asserts the label is ABSENT ("PII-free")
            if attached:
                verdicts.append(Verdict.CONTRADICTED)
                reasons.append(f"{where} is tagged {short}, contradicting the claim.")
            elif excluded_by:
                verdicts.append(Verdict.SUPPORTED)
                reasons.append(
                    f"{where} carries {excluded_by.rsplit(':', 1)[-1]}, which "
                    f"affirmatively rules out {short}."
                )
                evidence.append(
                    Evidence(
                        field=TABLE_FIELD if claim.field_path is None else SCHEMA_FIELD,
                        value=excluded_by,
                        note=f"{excluded_by.rsplit(':', 1)[-1]} affirms the absence of {short}.",
                    )
                )
            elif complete:
                verdicts.append(Verdict.SUPPORTED)
                reasons.append(
                    f"{where} carries no {short} tag and the table is marked "
                    f"classification-complete, so the absence is a reviewed finding."
                )
                evidence.append(completeness_evidence)
            elif not observed:
                # The trap, and it only fires when the table is NOT complete: no {short}
                # tag on an unreviewed entity is NOT evidence of absence of {short}.
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(
                    f"{where} is unclassified. Absence of a {short} tag is not evidence "
                    f"of absence of {short} — nobody has reviewed it."
                )
            else:
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(
                    f"{where} carries no {short} tag, but the table is not marked "
                    f"classification-complete — absence here is silence, not a clean bill."
                )
                evidence.append(completeness_evidence)

    return result(claim, worst(verdicts), " ".join(reasons), *evidence)
