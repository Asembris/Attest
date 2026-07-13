"""SCHEMA: does the catalog list the asserted columns, with the asserted types?

The schema, when present, is exhaustive — DataHub lists every column, not a sample.
So a column missing from a present schema is a denial: Contradicted. But a dataset
with NO schema aspect denies nothing at all; a claim about its columns is
Insufficient-Coverage. Absence of a schema is not absence of a column, and that
single distinction is the difference between "your agent hallucinated a column" and
"we never ingested this table's schema".

Type matching is deliberately lenient in one direction and strict everywhere else,
because DataHub records a column's type twice: `nativeDataType` is the platform's own
spelling ("VARCHAR(36)") and `type` is DataHub's abstract enum ("STRING"). A claim
may reasonably use either vocabulary, so an assertion matches if it matches either
one, and precision arguments are ignored ("varchar" matches "VARCHAR(36)"). Flagging
that as a contradiction would be crying wolf over a detail no one claimed.

What this checker will NOT do is map across dialects — see check_schema below.
"""

from __future__ import annotations

from attest.checkers.base import result, worst
from attest.claims import CheckResult, Evidence, SchemaClaim, Verdict
from attest.datahub import DatasetSnapshot, FieldSnapshot

SCHEMA_FIELD = "schemaMetadata.fields"


def _base_type(native: str) -> str:
    """'VARCHAR(36)' -> 'varchar'. Precision is a detail, not a claim."""
    return native.split("(", 1)[0].strip().lower()


def _type_matches(asserted: str, field: FieldSnapshot) -> bool:
    """Does `asserted` describe this column's type, in either of DataHub's vocabularies?

    Deliberately does not attempt cross-dialect equivalence: 'text' will not match
    'VARCHAR', and 'int' will not match 'NUMBER(12,0)'. Deciding those requires
    knowing a platform's type system, which is semantic work and belongs to the model
    layer, not here. See the module docstring in checkers/__init__.py.
    """
    want = asserted.strip().lower()
    candidates = {
        (field.data_type or "").lower(),
        (field.native_type or "").lower(),
        _base_type(field.native_type or ""),
    }
    return want in candidates or _base_type(want) in candidates


def check_schema(claim: SchemaClaim, snapshot: DatasetSnapshot) -> CheckResult:
    if snapshot.fields is None:
        return result(
            claim,
            Verdict.INSUFFICIENT_COVERAGE,
            "The catalog holds no schema for this dataset. It therefore says nothing "
            "about which columns exist — which is not the same as saying they do not.",
            Evidence(
                field=SCHEMA_FIELD,
                value=None,
                note="Schema aspect absent — no columns to compare against.",
            ),
        )

    present = [f.path for f in snapshot.fields]
    evidence: list[Evidence] = [
        Evidence(
            field=f"{SCHEMA_FIELD}[].fieldPath",
            value=present,
            note=f"Schema lists {len(present)} column(s); the list is exhaustive.",
        )
    ]
    verdicts: list[Verdict] = []
    reasons: list[str] = []

    for asserted in claim.columns:
        column = snapshot.field(asserted.name)

        if column is None:
            verdicts.append(Verdict.CONTRADICTED)
            reasons.append(
                f"Column '{asserted.name}' is not in the schema, which lists every column."
            )
            continue

        if asserted.native_type is None:
            verdicts.append(Verdict.SUPPORTED)
            reasons.append(f"Column '{asserted.name}' exists.")
            continue

        type_evidence = Evidence(
            field=f"{SCHEMA_FIELD}[{asserted.name}].nativeDataType",
            value=column.native_type,
            note=f"DataHub abstract type: {column.data_type}.",
        )
        evidence.append(type_evidence)

        if column.native_type is None and column.data_type is None:
            verdicts.append(Verdict.INSUFFICIENT_COVERAGE)
            reasons.append(
                f"Column '{asserted.name}' exists but the catalog records no type for it."
            )
        elif _type_matches(asserted.native_type, column):
            verdicts.append(Verdict.SUPPORTED)
            reasons.append(
                f"Column '{asserted.name}' exists with type {column.native_type}, "
                f"matching the asserted {asserted.native_type}."
            )
        else:
            verdicts.append(Verdict.CONTRADICTED)
            reasons.append(
                f"Column '{asserted.name}' is {column.native_type} "
                f"({column.data_type}), not the asserted {asserted.native_type}."
            )

    return result(claim, worst(verdicts), " ".join(reasons), *evidence)
