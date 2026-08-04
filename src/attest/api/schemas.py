"""The API's wire types.

`AuditRecord` (record.py) is the response body for an audit — the same object the store
persists, so what a caller reads is what Attest kept, rather than a summary of it. These
are the types that have no home elsewhere: what a caller sends in, and what comes back
from the two endpoints that are not simply "here is the run".
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from attest.record import AuditRecord, EvidenceView
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
    """A human's call on one audited claim: publish its verdict, and rule on any correction.

    **TWO SEPARATE FIELDS, DELIBERATELY.** Until Session 15 this was one `accept` flag that
    meant "accept the correction" and, as a side effect nobody chose, "publish the verdict".
    They are different decisions about different things, and a human can hold them
    independently — "your claim was wrong (publish that), and the fix you proposed is also
    wrong (reject that)". One boolean could not say it.

    Both are optional: only what you name is settled, and anything you leave out stays
    PENDING and re-parks the run. A decision naming neither is legal and does nothing.
    """

    model_config = ConfigDict(extra="forbid")

    claim_index: int = Field(ge=0, description="Which audited claim this decides.")
    publish: bool | None = Field(
        default=None,
        description="True clears this claim's VERDICT for the catalog: it is written back "
        "as a claim artifact. False withholds it and writes nothing. Omit for 'no opinion' "
        "— the verdict stays pending and the run stays parked. Every audited claim has one "
        "of these, whatever its verdict: Supported and Insufficient-Coverage are findings a "
        "catalog needs too, and a verdict Attest never records is indistinguishable from a "
        "claim Attest never checked.",
    )
    accept_correction: bool | None = Field(
        default=None,
        description="True accepts the revision the agent proposed for a Contradicted claim; "
        "False rejects it. Omit if there is no proposal, or for 'no opinion'. This does NOT "
        "publish anything — that is `publish`. Note an accepted correction never rewrites "
        "the original verdict: the agent was wrong, and later saying something true does "
        "not unsay it.",
    )
    reviewer: str = Field(default="", description="Who decided. Recorded, not verified.")
    note: str = Field(default="", description="Why. Kept with the decision, forever.")

    def to_decision(self) -> Decision:
        """The wire type, as the domain type the graph's checkpoint already takes."""
        return Decision(
            claim_index=self.claim_index,
            publish=self.publish,
            accept_correction=self.accept_correction,
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
        "until every proposal has been decided. Each claim may appear AT MOST ONCE.",
    )

    @model_validator(mode="after")
    def _no_duplicate_claim_index(self) -> ApprovalRequest:
        """One decision per claim, and a duplicate is a 422 rather than a silent collapse.

        Two decisions naming the same `claim_index` in one call are contradictory by intent —
        the resume path keeps only the last (dict keyed by index), but BOTH rows still land in
        the append-only `approvals` log, so the record would show a claim decided two
        different ways in one request. The store exists precisely because DataHub cannot hold
        an honest history; letting a self-contradictory call in would corrupt it at the source.
        A caller who changed their mind resubmits — the checkpoint is a loop — rather than
        stacking two verdicts on one claim in a single request.
        """
        seen = [d.claim_index for d in self.decisions]
        dupes = sorted({i for i in seen if seen.count(i) > 1})
        if dupes:
            raise ValueError(
                f"a claim_index may be decided at most once per request; duplicated: {dupes}"
            )
        return self


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

    claim_index: int = Field(
        description="Stable identity of the originating claim within this audit run."
    )
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


class VerdictEventView(BaseModel):
    """One verdict a claim has had, at one point in time. Read from DataHub.

    The history is real history: `assertionRunEvent` is a timeseries aspect, so re-auditing
    a claim APPENDS rather than overwrites. A Supported that later became Contradicted has
    both, in order, with who signed each one off — which is what a last-write-wins field
    could never hold, and the reason "was this ever contradicted before someone fixed the
    tag" is answerable from the catalog alone.
    """

    model_config = ConfigDict(frozen=True)

    at: datetime = Field(description="When the audit that reached this verdict ran.")
    verdict: str = Field(
        description="The verdict, VERBATIM as stored on the run event "
        "(`attest.verdict`). Never inferred from DataHub's SUCCESS/FAILURE/ERROR rollup — "
        "that projection is lossy, and an Insufficient-Coverage claim and a half-written "
        "one land in the same bucket."
    )
    audit_run: str = ""
    reviewer: str = ""
    decision: str = ""
    evidence: tuple[EvidenceView, ...] = Field(
        default=(),
        description="The catalog fields this verdict was decided against — STRUCTURED, and "
        "absence-preserving. `value` is `null` where the catalog was SILENT (the absence that "
        "justifies Insufficient-Coverage), kept distinct from an empty string or list. It "
        "used to be a flattened string with every null row dropped, so a reader inheriting "
        "the artifact could not tell 'the catalog said nothing' from 'the catalog said empty'.",
    )
    snapshot_id: str = Field(
        default="",
        description="The identity of the catalog snapshot this verdict was decided against — "
        "which state of the world it held true for. Captured at audit time and stored, never "
        "recomputed at write-back.",
    )
    native_type: str = Field(
        default="",
        description="DataHub's own result type. A LOSSY PROJECTION for its health rollup, "
        "shown for transparency and read by nothing.",
    )


class ClaimView(BaseModel):
    """One claim artifact, as the catalog holds it — plus whether Attest can vouch for it.

    `state` is the load-bearing field. See `read_state` in retrieval.py: the claim comes
    from DataHub, and the state comes from joining DataHub's answer with Attest's own
    record of what it wrote. An absent verdict is not one fact.
    """

    model_config = ConfigDict(frozen=True)

    claim_urn: str = Field(
        description="The artifact's URN. Content-addressed from the claim itself, so the "
        "same claim re-audited lands here again and appends to the history below."
    )
    target_urn: str
    claim_type: str
    grain: str = Field(description="`table` or `column`.")
    description: str
    asserted: dict = Field(
        default_factory=dict,
        description="What the claim asserts, from the artifact's `logic` payload.",
    )
    verdict: str | None = Field(
        default=None,
        description="The LATEST verdict, or null when the catalog holds none. Null is not "
        "a verdict and must not be read as one — check `state` to find out why it is null.",
    )
    state: str = Field(
        description="complete | pending-lag | incomplete | unknown. `complete`: the "
        "catalog holds the verdict. `pending-lag`: Attest's record says the write landed "
        "and DataHub's index has not caught up (transient, ~2s measured; nothing to "
        "repair). `incomplete`: Attest's record says the write FAILED at a step, so no "
        "verdict is coming — repair with POST /audit/{run_id}/writeback. `unknown`: there "
        "is no verdict and Attest's record cannot say whether one was attempted. "
        "**`unknown` is not `incomplete`**: reading absence as an answer is the mistake "
        "this whole product exists to catch."
    )
    failed_step: str | None = Field(
        default=None,
        description="Which write did not land — `upsert`, `report`, or `tag` — when "
        "`state` is `incomplete`. This is what distinguishes a repairable claim from one "
        "that is merely waiting on an index, and it comes from Attest's store, not from "
        "DataHub, which cannot know it.",
    )
    audit_run: str = Field(
        default="",
        description="The run whose write-back produced this artifact's latest state, when "
        "Attest's store knows it. What POST /audit/{run_id}/writeback needs to repair it.",
    )
    tags: tuple[str, ...] = ()
    stale_tag: bool = Field(
        default=False,
        description="The verdict TAG disagrees with the latest verdict — detected from the "
        "artifact ALONE (a store=None reader sees it too). The tag is a derived search index "
        "written last, so a crash between `report` and `tag`, or a verdict that flipped "
        "without the tag swap landing, leaves a correct verdict that a verdict-filtered "
        "search cannot find. On-read detection, not a background reconciler (which is "
        "deferred): the claim is repairable via POST /audit/{run_id}/writeback when Attest's "
        "store has the failed write. False whenever there is no verdict — a claim with none "
        "cannot have a stale tag.",
    )
    history: tuple[VerdictEventView, ...] = Field(
        default=(),
        description="Every verdict this claim has ever had, newest first. Append-only. "
        "Capped at 50 events per read; when `history_truncated` is true, the OLDEST are not "
        "in this list.",
    )
    history_total: int = Field(
        default=0,
        description="The catalog's OWN count of this claim's verdict events. When it exceeds "
        "`len(history)` the history was TRUNCATED at the 50-event read cap — the oldest events "
        "are absent from this listing, NOT from the catalog. Round-tripped so a truncated "
        "history is never mistaken for a complete one (the run-event twin of "
        "`RetrievalView.total`).",
    )
    history_truncated: bool = Field(
        default=False,
        description="True when `history_total > len(history)`: the 50-event cap cut the "
        "history off and older verdicts are not shown here. Full timeseries pagination is "
        "deferred (see the README); naming the truncation is not.",
    )


class RetrievalView(BaseModel):
    """WHERE each predicate was applied: DataHub's index, or Attest, afterwards.

    **This is part of the answer, not diagnostics.** The thesis is that claim artifacts are
    RETRIEVABLE FROM DATAHUB, which is true. That they are FULLY QUERYABLE IN DATAHUB would
    be false: the two server-side entry points are disjoint — `dataset.assertions` scopes to
    a dataset but filters nothing, and search filters `customType` and `tags` but cannot
    scope to a dataset at all (measured: nine candidate field names, all zero). `reviewer`
    and `since` cannot be pushed down at either, because an assertion indexes nothing else.

    So: DataHub does the scoping; Attest does most of the filtering. A response that hid
    that would let a reader believe the catalog answered a question Attest answered — an
    unfounded claim about where the evidence came from, published by the tool built to
    catch exactly that.
    """

    model_config = ConfigDict(frozen=True)

    entry_point: str = Field(
        description="The DataHub query used: `dataset.assertions` or `searchAcrossEntities`."
    )
    pushed_down: tuple[str, ...] = Field(
        default=(), description="Predicates DataHub's index applied."
    )
    filtered_locally: tuple[str, ...] = Field(
        default=(),
        description="Predicates ATTEST applied, over the response DataHub returned. Not a "
        "scan and not N+1: run events come back inline, so this is a filter over one "
        "already-narrowed page.",
    )
    considered: int = Field(
        default=0,
        description="How many artifacts the catalog returned before Attest filtered them. "
        "`considered` minus the number returned is what the local half cost.",
    )
    total: int = Field(
        default=0,
        description="The catalog's OWN total for this entry point's server-side scope. When "
        "`total > considered` the listing was TRUNCATED at the limit — there are more "
        "artifacts the catalog holds that this page did not return, and `note` says so. A "
        "claim past the limit is absent from this listing, NOT absent from the catalog.",
    )
    note: str = ""


class ClaimsResponse(BaseModel):
    """Claim artifacts from the catalog, and an honest account of how they were found."""

    model_config = ConfigDict(frozen=True)

    claims: tuple[ClaimView, ...] = ()
    retrieval: RetrievalView


class CatalogHitView(BaseModel):
    """One candidate dataset from discovery. A URN, and a name that is not evidence.

    `urn` is the ONLY field anything may act on, and it is the only one the MCP transport
    returns losslessly (§12 measured the rest: field tags and terms arrive as display names,
    `type` is commented out of the server's own fragment, `lastModified` is never asked for).
    `name` is the catalog's display name, shown so a human can tell two URNs apart.
    """

    model_config = ConfigDict(frozen=True)

    urn: str = Field(
        description="The dataset's canonical URN. The one value that crosses from discovery "
        "into an audit — and only after a human picks it AND writes it into the agent text, "
        "where it must appear verbatim.",
    )
    name: str = Field(
        default="",
        description="The catalog's display name for the entity, or empty when it has none. "
        "For display only: never matched against, never stored, never evidence.",
    )


class CatalogSearchResponse(BaseModel):
    """Candidate datasets, and an honest account of where they came from.

    **SEARCH SUCCEEDING IS NOT VERIFICATION SUCCEEDING**, and this response is built so a
    caller cannot confuse the two. `advisory` is a constant `True` on the wire rather than a
    caveat in the docs — the same argument as `RetrievalView` reporting where each predicate
    was applied. A list of datasets returned by an auditor invites the reading that the
    auditor has checked something about them. It has not: it has asked a catalog search
    engine what matches some text.
    """

    model_config = ConfigDict(frozen=True)

    hits: tuple[CatalogHitView, ...] = ()
    total: int = Field(
        default=0,
        description="The catalog's OWN match count. `total > len(hits)` means this is a "
        "page and a dataset past it is absent from the LISTING, not from the catalog — the "
        "same round-trip as RetrievalView.total, for the same reason.",
    )
    transport: str = Field(
        default="",
        description="Which transport answered. Named because it is NOT the one Attest reads "
        "verdicts through: see docs/mcp-evaluation.md.",
    )
    server: str = Field(
        default="",
        description="The MCP server as it identified itself at `initialize`, e.g. "
        "`datahub v3.4.5`. Provenance for a human; nothing reads it.",
    )
    advisory: bool = Field(
        default=True,
        description="Always true. Nothing in this response has been verified against the "
        "catalog by a checker, and no value in it can reach a verdict.",
    )
    note: str = ""


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
    datahub_ui_url: str = Field(
        default="http://localhost:9002",
        description="The DataHub UI origin the frontend should deep-link to. Backend-supplied "
        "so the built SPA does not hardcode a host that breaks behind a real URL.",
    )
    discovery: str = Field(
        default="",
        description="Catalog discovery (the DataHub MCP Server, §22): `disabled`, "
        "`not-installed`, `not-contacted`, `up: <server>`, or `unreachable: <why>`. "
        "**Reported, never probed** — a health check that spawned the server would spawn one "
        "on every browser load, and `not-contacted` is the honest answer to a question nobody "
        "has asked yet. Empty means the field was not reported at all, which is what a "
        "response recorded before this field existed says; it is not a claim that discovery "
        "is down.",
    )
