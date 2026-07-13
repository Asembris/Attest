"""The typed vocabulary Attest reasons in: claims, verdicts, and evidence.

A *claim* is one assertion an agent made about one catalog entity. A *verdict* is
what the catalog says about it. *Evidence* is the specific catalog field that
produced the verdict.

Three design decisions are load-bearing here.

**The three verdicts are not a confidence scale.** Supported and Contradicted both
mean the catalog spoke; Insufficient-Coverage means it did not. An agent is not
wrong because the catalog is incomplete, and most entities in a real catalog are
incomplete. Collapsing Insufficient-Coverage into Contradicted would make Attest
cry wolf on every under-documented table — which is the failure mode this project
exists to prevent. Every checker must be able to return all three.

**Claims carry an explicit target URN.** Resolving "the customer table" to a URN is
entity resolution, which is ambiguous and needs a model. That is deliberately out
of scope for the deterministic core: a claim arrives pointing at exactly one
entity, and the checkers never guess. Session 2's extraction layer is what turns
prose into a URN, and it will be wrong sometimes — keeping it upstream of here
means its errors can never be mistaken for catalog disagreement.

**Evidence is mandatory, not decorative.** A checker returns the catalog fields it
read, and it must return them even when the verdict is Insufficient-Coverage
(where the evidence is the absence itself). Session 2's explanation layer is
constrained to this evidence, which is what makes the pipeline auditable rather
than a model's opinion about a model's opinion.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClaimType(StrEnum):
    FRESHNESS = "freshness"
    OWNERSHIP = "ownership"
    CLASSIFICATION = "classification"
    SCHEMA = "schema"


class Verdict(StrEnum):
    """What the catalog says about a claim.

    INSUFFICIENT_COVERAGE is not a weaker CONTRADICTED. It is the catalog
    declining to answer, and it is a correct, final outcome — not a failure.
    """

    SUPPORTED = "Supported"
    CONTRADICTED = "Contradicted"
    INSUFFICIENT_COVERAGE = "Insufficient-Coverage"


# --- claims ------------------------------------------------------------------


class BaseClaim(BaseModel):
    """One assertion, about one entity, in a form a checker can decide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_urn: str = Field(
        description="The DataHub URN this claim is about. Explicit — never inferred here."
    )
    raw_text: str = Field(
        description="The original sentence the claim came from. Carried for explanation, "
        "never parsed by a checker."
    )

    @field_validator("target_urn")
    @classmethod
    def _must_be_dataset_urn(cls, v: str) -> str:
        # Every checker in this session reads dataset aspects. A urn:li:chart URN
        # would fetch nothing and be reported as Insufficient-Coverage, which would
        # be a lie: the catalog is not silent, we asked the wrong question.
        if not v.startswith("urn:li:dataset:"):
            raise ValueError(f"target_urn must be a dataset URN, got: {v!r}")
        return v


class FreshnessClaim(BaseClaim):
    """"Updated in the last 24 hours", "refreshed daily"."""

    claim_type: Literal[ClaimType.FRESHNESS] = ClaimType.FRESHNESS
    max_age_hours: float = Field(
        gt=0,
        description="The oldest the data may be for the claim to hold. 'Refreshed "
        "daily' is 24; 'updated this week' is 168.",
    )

    @property
    def asserted_value(self) -> float:
        return self.max_age_hours


class OwnershipClaim(BaseClaim):
    """"Sarah owns this dataset"."""

    claim_type: Literal[ClaimType.OWNERSHIP] = ClaimType.OWNERSHIP
    owner_urn: str = Field(
        description="The asserted owner, as a corpuser/corpGroup URN. Mapping a name "
        "like 'Sarah' onto a URN is entity resolution and happens upstream."
    )

    @field_validator("owner_urn")
    @classmethod
    def _must_be_actor_urn(cls, v: str) -> str:
        if not v.startswith(("urn:li:corpuser:", "urn:li:corpGroup:")):
            raise ValueError(f"owner_urn must be a corpuser or corpGroup URN, got: {v!r}")
        return v

    @property
    def asserted_value(self) -> str:
        return self.owner_urn


class ClassificationClaim(BaseClaim):
    """"Contains PII", "this table is PII-free", "the email column is PII".

    `present=False` is what makes "PII-free" a first-class claim rather than the
    absence of a claim. The two are wholly different: a table with no tags at all
    cannot support "PII-free" — the catalog has not reviewed it, so the verdict is
    Insufficient-Coverage, not Supported. Treating unlabelled as clean is the exact
    mistake a groundedness auditor exists to catch.
    """

    claim_type: Literal[ClaimType.CLASSIFICATION] = ClaimType.CLASSIFICATION
    labels: tuple[str, ...] = Field(
        min_length=1,
        description="Asserted tag or glossary-term URNs, e.g. urn:li:tag:PII.",
    )
    present: bool = Field(
        default=True,
        description="True: the labels ARE attached ('contains PII'). "
        "False: they are NOT ('PII-free').",
    )
    field_path: str | None = Field(
        default=None,
        description="Column the claim is about. None means the claim is about the table.",
    )

    @field_validator("labels")
    @classmethod
    def _must_be_label_urns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for label in v:
            if not label.startswith(("urn:li:tag:", "urn:li:glossaryTerm:")):
                raise ValueError(f"label must be a tag or glossaryTerm URN, got: {label!r}")
        return v

    @property
    def asserted_value(self) -> tuple[str, ...]:
        return self.labels


class ColumnAssertion(BaseModel):
    """One asserted column. `native_type` None means the claim is only that it exists."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    native_type: str | None = None


class SchemaClaim(BaseClaim):
    """"It has a customer_id column", "order_total is a NUMBER"."""

    claim_type: Literal[ClaimType.SCHEMA] = ClaimType.SCHEMA
    columns: tuple[ColumnAssertion, ...] = Field(min_length=1)

    @property
    def asserted_value(self) -> tuple[ColumnAssertion, ...]:
        return self.columns


Claim = Annotated[
    FreshnessClaim | OwnershipClaim | ClassificationClaim | SchemaClaim,
    Field(discriminator="claim_type"),
]


# --- results -----------------------------------------------------------------


class Evidence(BaseModel):
    """One catalog field that contributed to a verdict.

    `value=None` is meaningful and common: it is how a checker says "I looked here
    and the catalog had nothing", which is precisely the justification for an
    Insufficient-Coverage verdict. Evidence is required on every verdict, so the
    absence has to be shown rather than merely asserted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(
        description="Dotted path of the DataHub field read, e.g. "
        "'ownership.owners[].owner.urn' or 'properties.lastModified.time'."
    )
    value: Any = Field(
        default=None, description="What the catalog held there. None means: nothing."
    )
    note: str | None = Field(
        default=None, description="Why this field decided the verdict. Deterministic text."
    )


class CheckResult(BaseModel):
    """A verdict on one claim, plus the catalog fields that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: Claim
    verdict: Verdict
    evidence: tuple[Evidence, ...] = Field(min_length=1)
    reason: str = Field(
        description="Deterministic, human-readable justification. Generated by code, "
        "never by a model — Session 2's explanations are constrained to this and the "
        "evidence above."
    )

    @property
    def target_urn(self) -> str:
        return self.claim.target_urn
