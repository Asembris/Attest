"""OWNERSHIP: is the asserted owner the actual owner?

Set membership. Unlike tags, DataHub's owners list is exhaustive by construction —
it is the complete set of owners, not a sample of them. So a populated owners list
that omits the claimed owner is a positive denial, and Contradicted is correct
without needing any completeness marker. An *empty* or absent list carries no such
weight: nobody has been assigned, so nobody has been ruled out.
"""

from __future__ import annotations

from attest.checkers.base import result
from attest.claims import CheckResult, Evidence, OwnershipClaim, Verdict
from attest.datahub import DatasetSnapshot

FIELD = "ownership.owners[].owner.urn"


def check_ownership(claim: OwnershipClaim, snapshot: DatasetSnapshot) -> CheckResult:
    owners = snapshot.owners

    if not owners:
        return result(
            claim,
            Verdict.INSUFFICIENT_COVERAGE,
            "No owner is assigned to this dataset in the catalog, so the asserted "
            "owner can be neither confirmed nor denied.",
            Evidence(
                field=FIELD,
                value=None,
                note=(
                    "Ownership aspect absent."
                    if owners is None
                    else "Ownership aspect present but lists no owners."
                ),
            ),
        )

    observed = Evidence(
        field=FIELD,
        value=list(owners),
        note=f"Catalog lists {len(owners)} owner(s).",
    )

    if claim.owner_urn in owners:
        return result(
            claim,
            Verdict.SUPPORTED,
            f"{claim.owner_urn} is listed as an owner in the catalog.",
            observed,
        )

    return result(
        claim,
        Verdict.CONTRADICTED,
        f"{claim.owner_urn} is not an owner. The catalog lists "
        f"{', '.join(owners)} — and that list is exhaustive.",
        observed,
    )
