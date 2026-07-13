"""Deterministic checkers: one per claim type. No model, in any of them.

Each is a pure function of (claim, snapshot) -> CheckResult. They do date
arithmetic, set membership, and string comparison, and they return the specific
DataHub fields that produced the verdict. Session 2's explanation layer is
constrained to that evidence, which is what keeps the pipeline auditable instead of
being a model's opinion about a model's opinion.

--------------------------------------------------------------------------------
THE BOUNDARY: what these checkers deliberately refuse to decide
--------------------------------------------------------------------------------

Four questions came up while writing this layer that cannot be answered by code
without inventing semantics. Each was pushed out rather than guessed at, because a
checker that quietly guesses is worse than one that abstains: it produces a verdict
with the same confident shape as a real one.

1. ENTITY RESOLUTION — "the customer table" -> which URN?
   Not here. Claims arrive with an explicit target_urn (enforced in claims.py). This
   is Session 2's job, upstream. Keeping it upstream means a resolution error can
   never be laundered into a catalog disagreement.

2. LABEL OPPOSITION — does NonPII contradict PII?
   Nothing in DataHub says it does; they are two unrelated URNs. A model could infer
   it from the names, but then a tag rename would silently flip verdicts. So it is
   DECLARED as data in policy.EXCLUSIONS, where it can be reviewed and argued with.

3. THE CLOSED-WORLD ASSUMPTION — does an untagged table contradict "contains PII"?
   Normally no: tags are open-world and a missing tag is silence, so the verdict is
   Insufficient-Coverage. Contradicted requires the catalog itself to declare its
   classification complete (policy.COMPLETENESS_MARKERS). Attest never makes that
   assumption on its own; the catalog grants it, per entity, via a tag a human
   applied. This is the single most consequential rule in the layer.

4. CROSS-DIALECT TYPE EQUIVALENCE — is a 'text' column a 'string'?
   Partially handled, deliberately. DataHub stores both a native type ('VARCHAR(36)')
   and an abstract enum ('STRING'), so both vocabularies are checkable by exact match,
   and precision arguments are ignored. What is NOT done is genuine dialect mapping
   ('text' ~ 'VARCHAR', 'int8' ~ 'BIGINT'), which needs a model of each platform's
   type system. A claim in a vocabulary the catalog does not use is Contradicted
   today. If that proves too harsh in practice it is an escalation to the semantic
   layer, not an `if` to add here.

If a fifth question shows up that needs a model, it goes on this list — it does not
go into a checker.
"""

from __future__ import annotations

from datetime import datetime

from attest.checkers.classification import check_classification
from attest.checkers.freshness import check_freshness
from attest.checkers.ownership import check_ownership
from attest.checkers.schema import check_schema
from attest.claims import (
    CheckResult,
    Claim,
    ClassificationClaim,
    FreshnessClaim,
    OwnershipClaim,
    SchemaClaim,
)
from attest.datahub import DatasetSnapshot

__all__ = [
    "check",
    "check_classification",
    "check_freshness",
    "check_ownership",
    "check_schema",
]


def check(
    claim: Claim, snapshot: DatasetSnapshot, now: datetime | None = None
) -> CheckResult:
    """Route a claim to its checker.

    The snapshot is fetched by the caller (see DataHubClient.fetch_dataset), which
    raises on a missing entity. By the time a checker runs, the entity is known to
    exist — so any Insufficient-Coverage it returns is a real statement about the
    catalog's coverage, never a swallowed lookup failure.
    """
    match claim:
        case FreshnessClaim():
            return check_freshness(claim, snapshot, now=now)
        case OwnershipClaim():
            return check_ownership(claim, snapshot)
        case ClassificationClaim():
            return check_classification(claim, snapshot)
        case SchemaClaim():
            return check_schema(claim, snapshot)
        case _:  # pragma: no cover — the union is closed
            raise TypeError(f"no checker for claim type: {type(claim).__name__}")
