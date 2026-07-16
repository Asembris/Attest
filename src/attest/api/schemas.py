"""The API's wire types.

`AuditRecord` (record.py) is the response body for an audit — the same object the store
persists, so what a caller reads is what Attest kept, rather than a summary of it. These
are the types that have no home elsewhere: what a caller sends in, and what comes back
from the two endpoints that are not simply "here is the run".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from attest.record import AuditRecord
from attest.report import Decision


class AuditRequest(BaseModel):
    """An agent's output, submitted for audit."""

    model_config = ConfigDict(extra="forbid")

    agent_output: str = Field(
        min_length=1,
        description="The agent's prose, verbatim and untrusted. Claims are extracted from "
        "it; instruction-like spans are stripped before any model sees it.",
    )
    source_agent: str = Field(
        default="",
        description="Who made these claims. Carried through to the verdict written back "
        "to DataHub, so a verdict can be attributed later.",
    )
    target_urns: tuple[str, ...] = Field(
        default=(),
        description="The dataset URNs this audit is REQUIRED to cover. Optional. Each must "
        "appear verbatim in agent_output (422 if not), and each must produce a claim (422 "
        "if not — the audit still runs and is stored). It never narrows the audit.",
    )

    @model_validator(mode="after")
    def _urns_must_appear_in_the_text(self) -> AuditRequest:
        """A declared target URN has to be one the agent actually named.

        HALF of the precondition; it is the half knowable at request time. The other half —
        that a claim was actually extracted for each declared URN — cannot be known until
        the decomposer has run, and it is enforced in `AuditService._require_coverage`. Both
        halves answer to the same 422, because they are the same promise: an audit that
        covers what the caller required, or an honest refusal.

        Two things this is NOT, and the distinction is the interesting part.

        It is not entity resolution: a claim's `target_urn` must appear verbatim in the
        source text (decompose.py), because a model that may MINT a URN can hallucinate an
        entity into an audit. A caller who declares a URN the agent never wrote has
        declared something no claim can ever be about, and the honest answer is to say so
        rather than to return an audit that silently covers nothing.

        And it is not a FILTER. Claims about datasets outside `target_urns` are still
        audited. Letting a caller narrow the audit to a subset of what the agent actually
        claimed would let them hide a claim from the auditor by not declaring it — which is
        the one thing an auditor must never offer. The scope of an audit is what the agent
        said, not what the caller admits to. So the field can only ever DEMAND more
        coverage, never less.
        """
        missing = [u for u in self.target_urns if u not in self.agent_output]
        if missing:
            raise ValueError(
                "these target URNs do not appear in agent_output, so no claim can be about "
                f"them: {missing}. A claim's URN must be quoted by the agent, never minted."
            )
        return self


class DecisionRequest(BaseModel):
    """A human's call on one proposed correction."""

    model_config = ConfigDict(extra="forbid")

    claim_index: int = Field(ge=0, description="Which audited claim this decides.")
    accept: bool = Field(
        description="True accepts the proposed correction and writes the verdict back to "
        "DataHub. False rejects it and writes nothing."
    )
    reviewer: str = Field(default="", description="Who decided. Recorded, not verified.")
    note: str = Field(default="", description="Why. Kept with the decision, forever.")

    def to_decision(self) -> Decision:
        """The wire type, as the domain type the graph's checkpoint already takes."""
        return Decision(
            claim_index=self.claim_index,
            accept=self.accept,
            reviewer=self.reviewer,
            note=self.note,
        )


class ApprovalRequest(BaseModel):
    """The human checkpoint, as an HTTP call.

    An empty `decisions` list is legal and means exactly what it says: a person looked and
    settled nothing. Those proposals stay PENDING. There is no "approve all" and its
    absence is deliberate — it is the accountability decision from Session 3, and it does
    not soften because there is now an API in front of it.

    A call that leaves any proposal undecided leaves the run AWAITING_REVIEW, and it stays
    resumable: call again with the rest. The run settles to COMPLETE on the call that
    decides the last one. Until Session 14 this endpoint said all of the above and then
    ended the run anyway, which turned "your proposals are still PENDING" into "your
    proposals are PENDING and there is no longer any way to decide them".
    """

    model_config = ConfigDict(extra="forbid")

    decisions: tuple[DecisionRequest, ...] = Field(
        default=(),
        description="One entry per proposal you are settling. Proposals you do not name "
        "stay PENDING — nothing is accepted by default, and the run stays awaiting review "
        "until every proposal has been decided.",
    )


class WriteBackView(BaseModel):
    """What the catalog did with an accepted verdict.

    `failed_step` names WHICH of the three writes did not land, and it is not a nicety. The
    claim artifact cannot be written atomically — upsert the claim, report the verdict, swap
    the verdict tag — so "it failed" is not actionable on its own. A failed `report` left a
    claim with no verdict; a failed `tag` left a verdict that is entirely correct and merely
    not yet findable by search. Different facts, and a caller is entitled to tell them apart.
    Every failure is repairable with `POST /audit/{run_id}/writeback`.
    """

    model_config = ConfigDict(frozen=True)

    target_urn: str
    ok: bool
    detail: str = ""
    claim_urn: str = Field(
        default="",
        description="The claim artifact's URN in DataHub. Derived from the claim's own "
        "content, so it is stable across re-runs and names the same artifact every time.",
    )
    failed_step: str | None = Field(
        default=None,
        description="Which write did not land: `upsert`, `report`, or `tag`. None if all did.",
    )


class WriteBackResponse(BaseModel):
    """The result of re-running the catalog write for a run's already-accepted claims.

    No decision is taken here and none can be: this re-executes the side effect of decisions
    that are already in the append-only log. A claim nobody accepted is not reachable.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    writebacks: tuple[WriteBackView, ...] = ()


class ApprovalResponse(BaseModel):
    """The settled run, plus what reached DataHub.

    The write-backs are reported separately from the record because they can fail
    independently: the decision is a human act and it stands, while the catalog write is a
    network call that may not have landed. An approval whose write-back failed is reported
    as exactly that, not as a success.
    """

    model_config = ConfigDict(frozen=True)

    audit: AuditRecord
    writebacks: tuple[WriteBackView, ...] = ()


class HealthResponse(BaseModel):
    """Liveness. Attest's own, and the catalog's, kept separate on purpose.

    DataHub being down does NOT make Attest unhealthy — the service is up, it can serve
    stored audits, and it will report a resolve failure honestly on a new one. Collapsing
    the two would take the service out of rotation for a dependency's outage, and would
    also hide the outage behind a bare 503.
    """

    model_config = ConfigDict(frozen=True)

    status: str
    version: str
    model: str
    datahub: str
