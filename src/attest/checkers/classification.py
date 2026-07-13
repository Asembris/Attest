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
    # claim: it is a statement about the review that was performed on the dataset, and
    # a reviewed table's untagged column was reviewed too.
    complete = policy.classification_is_complete(snapshot.labels)
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

    verdicts: list[Verdict] = []
    reasons: list[str] = []
    evidence: list[Evidence] = [observed_evidence]

    for label in claim.labels:
        short = label.rsplit(":", 1)[-1]
        attached = label in observed
        excluded_by = policy.contradicts(label, observed)

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
            elif not observed:
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(f"{where} is unclassified, so {short} cannot be confirmed.")
            elif complete:
                verdicts.append(Verdict.CONTRADICTED)
                reasons.append(
                    f"{short} is not attached to {where}, and the table is marked "
                    f"classification-complete — so the omission is deliberate."
                )
                evidence.append(completeness_evidence)
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
            elif not observed:
                # The trap. No PII tag is NOT evidence of no PII.
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(
                    f"{where} is unclassified. Absence of a {short} tag is not evidence "
                    f"of absence of {short} — nobody has reviewed it."
                )
            elif complete:
                verdicts.append(Verdict.SUPPORTED)
                reasons.append(
                    f"{where} carries no {short} tag and the table is marked "
                    f"classification-complete, so the absence is a reviewed finding."
                )
                evidence.append(completeness_evidence)
            else:
                verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
                reasons.append(
                    f"{where} carries no {short} tag, but the table is not marked "
                    f"classification-complete — absence here is silence, not a clean bill."
                )
                evidence.append(completeness_evidence)

    return result(claim, worst(verdicts), " ".join(reasons), *evidence)
