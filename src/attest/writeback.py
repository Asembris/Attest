"""Writing an approved verdict back to DataHub, as a per-claim artifact.

The goal is one sentence long, and until Session 15 it was not true:

    **"Two claims about one dataset are approved. Show me what the next agent inherits
      from DataHub alone."**

The old answer was an opaque run UUID. Verdicts were written as five dataset-level
structured properties, and structured properties are last-write-wins — so two claims about
one dataset OVERWROTE each other, the catalog held the verdict of whichever was written
last, and that verdict had no subject. `attest.verdict = "Contradicted"` cannot say WHICH
claim was contradicted. The rest of the story lived in Attest's SQLite store, behind a join
key a second agent cannot follow.

Now each claim is its own artifact, and the next agent inherits all of it from the catalog.

--------------------------------------------------------------------------------
The representation, and why an Assertion
--------------------------------------------------------------------------------

One claim = one CUSTOM **Assertion** + N appended **run events**. That split is the design:

    the assertion  = the CLAIM   (stable: what is asserted, about what, at what grain)
    a run event    = the VERDICT (append-only: what the catalog said, and who signed it off)

An assertion is, in DataHub's own vocabulary, a verifiable statement about data — which is
exactly what a claim is. Two properties fall out of that and neither was available before:

  * **Two claims on one dataset coexist.** Attest supplies the assertion's URN (derived from
    the claim's own content), so distinct claims land at distinct URNs and neither
    overwrites the other. This is the invariant the whole feature exists for.
  * **The history is IN the catalog.** `assertionRunEvent` is a *timeseries* aspect: it
    appends. "Was this ever contradicted before someone fixed the tag" is answerable from
    DataHub alone. Structured properties could never do this — which is what store.py's
    "DataHub is the catalog, not the event store" was really about. That claim was true of
    structured properties and overgeneralized to DataHub.

All of it was measured against the pinned Core v1.5.0.6 before any of it was designed:
`spikes/claim_artifact_probe.py`, and `docs/design/claim-artifact.md`.

--------------------------------------------------------------------------------
The verdict is a STORED VALUE. It is never inferred.
--------------------------------------------------------------------------------

`AssertionResultType` has four values (INIT/SUCCESS/FAILURE/ERROR) and Attest has three
verdicts, and they do not line up. So the verdict is written **verbatim** into the run
event's `nativeResults` as `attest.verdict`, and THAT is authoritative. The native type is a
**lossy projection** for DataHub's own health rollup:

    Supported             -> SUCCESS
    Contradicted          -> FAILURE
    Insufficient-Coverage -> ERROR    (lands in NEITHER the passed nor the failed bucket,
                                       which is exactly right: the catalog being silent is
                                       not a pass and not a fail)

**Never read the verdict back off `result.type` or the succeeded/failed counts.** The rollup
is measurably ambiguous: an Insufficient-Coverage claim and a HALF-WRITTEN one both read
`succeeded=0 failed=0`. `attest.verdict` present means the claim has a verdict and its value
IS the verdict; absent means the write never completed. Those are different facts and the
third verdict is far too load-bearing to recover from a bucket.

(`AssertionResultErrorType.INSUFFICIENT_DATA` exists and looks perfect for this. It is
unusable: `result.error` is accepted by the mutation and silently discarded — it reads back
`null`. Measured, twice.)

--------------------------------------------------------------------------------
Three writes, no atomicity, and why that still isn't a saga
--------------------------------------------------------------------------------

The write is necessarily three operations — upsert the claim, report the verdict, swap the
tag — and cannot be atomic. `WriteResult` therefore names WHICH STEP failed rather than
carrying a boolean, for the same reason `CorrectionOutcome` names six outcomes: a success
flag would hide precisely the failure modes this change introduces.

It needs no compensating transactions, because **all three writes are idempotent and
recovery is repetition**:

    upsert  idempotent by definition, AND at a URN derived from the claim's content, so a
            re-run lands on the SAME artifact and can never mint a second one
    report  idempotent by KEY: a timeseries row is keyed by (urn, aspect, timestampMillis),
            so the same verdict at the same timestamp collapses to one row
    tag     idempotent by set semantics

**There are exactly two ways to break that, and both are structurally unavailable here:**
mint the claim URN non-deterministically — `claim_id` is handed the claim and nothing else,
so there is no run id or clock to leak into it — or use `now()` for the timestamp, which is
why `write_claim_artifact` has NO DEFAULT for `checked_at`. Neither is forbidden by comment;
both are unreachable from the signature.

--------------------------------------------------------------------------------
Nothing is written until a human approves it
--------------------------------------------------------------------------------

Unchanged, and the reason the module reads the way it does. This is called from the approval
path and from the write-back retry, and the retry can only re-write what a human already
accepted — it cannot accept anything. An auditor that silently edits the system it audits
has stopped being an auditor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from attest.datahub import DataHubClient, DataHubError

log = logging.getLogger(__name__)

NAMESPACE = "attest"

# --- the claim artifact -------------------------------------------------------

# One custom type per claim type, because `customType` is one of only TWO fields that are
# filterable on an assertion (the other is `tags`), and "every contradicted OWNERSHIP claim
# this week" needs claim type and verdict to be filters at once. The filter accepts multiple
# values, so "every Attest claim" is still one query across all four. Measured.
CUSTOM_TYPE_PREFIX = "ATTEST_CLAIM_"

# The platform an Attest assertion is attributed to. Assertions require one; this says the
# statement came from Attest rather than from the dataset's own platform.
ATTEST_PLATFORM_URN = "urn:li:dataPlatform:datahub"

# Verdict -> DataHub's native result type. A LOSSY PROJECTION for DataHub's health rollup;
# `attest.verdict` in nativeResults is the authoritative value. See the module docstring.
NATIVE_RESULT_TYPE: dict[str, str] = {
    "Supported": "SUCCESS",
    "Contradicted": "FAILURE",
    "Insufficient-Coverage": "ERROR",
}

# nativeResults keys. The artifact's payload, and the reason a second agent needs nothing
# but the catalog.
VERDICT_KEY = f"{NAMESPACE}.verdict"
CLAIM_TYPE_KEY = f"{NAMESPACE}.claim_type"
AUDIT_RUN_KEY = f"{NAMESPACE}.audit_run"
REVIEWER_KEY = f"{NAMESPACE}.reviewer"
DECISION_KEY = f"{NAMESPACE}.decision"
EVIDENCE_KEY = f"{NAMESPACE}.evidence"
SNAPSHOT_KEY = f"{NAMESPACE}.snapshot_id"
POLICY_KEY = f"{NAMESPACE}.policy_version"
GRAIN_KEY = f"{NAMESPACE}.grain"

# The declared governance semantics the verdict was reached under. Bump when policy.py's
# rules change meaning: a verdict is only interpretable against the policy that produced it.
POLICY_VERSION = "1"

# Fields excluded from a claim's IDENTITY. `raw_text` is the prose the claim came from --
# "Alice owns customers" and "the customers table is owned by Alice" are the SAME claim, and
# hashing the wording would mint a second artifact for a re-phrased sentence.
IDENTITY_EXCLUDES = frozenset({"raw_text"})


def claim_id(claim: Mapping[str, Any]) -> str:
    """A stable, content-addressed id for one claim.

    THE GUARANTEE THIS EXISTS FOR: re-running the write-back lands on the SAME artifact and
    can never create a duplicate. The id is derived from the claim's own content and NOTHING
    else — this function is handed the claim and is not given the run id, the attempt
    number, or a clock, so none of them can leak into the identity. That is what makes a
    retry safe (see the module docstring) and it is structural, not a convention.

    What goes in is exactly the claim's identity — its target URN, its type, its grain
    (`field_path`), and what it asserts — because that IS the serialized claim minus its
    prose. Two different claims about one dataset hash differently and coexist; the same
    claim re-audited next month hashes identically and appends a verdict to its history.

    Canonical JSON, so key order and whitespace cannot change the id.
    """
    identity = {k: v for k, v in claim.items() if k not in IDENTITY_EXCLUDES}
    key = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return f"{NAMESPACE}-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def claim_urn(claim: Mapping[str, Any]) -> str:
    """The DataHub URN of a claim's artifact. Derived, never minted."""
    return f"urn:li:assertion:{claim_id(claim)}"


def custom_type_for(claim_type: str) -> str:
    """`ownership` -> `ATTEST_CLAIM_OWNERSHIP`. The one filterable claim-type dimension."""
    return f"{CUSTOM_TYPE_PREFIX}{claim_type.upper()}"


def grain_of(claim: Mapping[str, Any]) -> str:
    """`column` if the claim names one, else `table`.

    The grain travels here, in the payload, and NOT in the assertion's `fieldPath` — which
    would make the artifact permanently unable to carry a verdict. See
    `client.upsert_custom_assertion`.
    """
    return "column" if claim.get("field_path") else "table"


# --- verdict tags -------------------------------------------------------------

VERDICTS: tuple[str, ...] = ("Supported", "Contradicted", "Insufficient-Coverage")


def verdict_tag_urn(verdict: str) -> str:
    return f"urn:li:tag:Attest-{verdict}"


@dataclass(frozen=True)
class PropertyDef:
    """One structured property Attest writes. Defined here so it can be bootstrapped."""

    name: str
    display_name: str
    description: str

    @property
    def qualified_name(self) -> str:
        return f"{NAMESPACE}.{self.name}"

    @property
    def urn(self) -> str:
        return f"urn:li:structuredProperty:{self.qualified_name}"


# The dataset-level badge. KEPT, and deliberately: it answers a different question from the
# claim artifacts -- "is there an Attest verdict on this dataset, at a glance, in the
# dataset's own property panel". It stays last-write-wins, which is fine ONLY because the
# subject-bearing record is the claim artifact and this is a pointer to it.
#
# THE DESCRIPTIONS BELOW STATE WHAT THE BADGE CANNOT DO, and that is the point of them.
# **A field that overstates its coverage is this project's characteristic bug** -- the same
# one faithfulness.py committed until Session 13 said so out loud -- and this badge is the
# most-read surface Attest has. What was here before claimed to be "the latest verdict about
# ANY claim ... a badge for the most recent one", and BOTH halves were false:
#
#   * NOT "any claim an agent made": only claims a HUMAN PUBLISHED reach the catalog, so a
#     claim a reviewer withheld leaves no trace here at all;
#   * NOT "the most recent one": every claim in one run shares that run's timestamp, so
#     among them there IS no most-recent. The survivor is whichever was written LAST, in
#     claim order -- an arbitrary one, dressed up as a chronological one.
#
# The badge cannot say WHICH claim it describes. That was the whole problem statement of
# the claim artifact (docs/design/claim-artifact.md §1), and the badge is still the thing
# that has it. So it says so, and points at the record that does not.
VERDICT = PropertyDef(
    "verdict",
    "Attest verdict",
    "ONE claim's verdict — Supported, Contradicted, or Insufficient-Coverage — and NOT "
    "this dataset's overall status. This badge cannot say which claim it is about: it is "
    "whichever verdict was published back last, and claims published together share a "
    "timestamp, so 'last' is arbitrary among them rather than most-recent. It is a "
    "presence indicator, not a summary. THE ACTUAL RECORD IS THIS DATASET'S ASSERTIONS: "
    "one artifact per claim, each naming what was asserted, at what grain, with every "
    "verdict it has ever had and who signed each one off. Read those, not this. Note also "
    "that only verdicts a human PUBLISHED appear here — a claim a reviewer withheld leaves "
    "this badge untouched, so its absence is not evidence that nothing was claimed.",
)
CLAIM_TYPE = PropertyDef(
    "claim_type",
    "Attest claim type",
    "The claim type — freshness, ownership, classification, or schema — of the same single "
    "claim `attest.verdict` describes. The five attest.* properties are written together in "
    "one call, so they are consistent with each other: they all describe ONE claim. They "
    "just cannot say which one. See this dataset's Assertions for every claim.",
)
CHECKED_AT = PropertyDef(
    "checked_at",
    "Attest checked at",
    "When the audit ran that produced the verdict in `attest.verdict`, ISO-8601. It is that "
    "one claim's audit, not the last time this dataset was audited — an audit whose verdicts "
    "were all withheld never touches this.",
)
SOURCE_AGENT = PropertyDef(
    "source_agent",
    "Attest source agent",
    "The agent that made the claim described by `attest.verdict`. Not every agent that has "
    "made a claim about this dataset — see this dataset's Assertions for those.",
)
AUDIT_RUN = PropertyDef(
    "audit_run",
    "Attest audit run",
    "The audit run that produced the verdict in `attest.verdict`. Every claim artifact on "
    "this dataset carries its own run id per verdict, in the run event — so the history is "
    "readable from this catalog alone, without Attest. This id additionally joins to "
    "Attest's own store, which holds what the run cost, what evidence it saw, and what its "
    "trajectory check said.",
)

PROPERTIES: tuple[PropertyDef, ...] = (
    VERDICT,
    CLAIM_TYPE,
    CHECKED_AT,
    SOURCE_AGENT,
    AUDIT_RUN,
)


# --- results ------------------------------------------------------------------

UPSERT = "upsert"
REPORT = "report"
TAG = "tag"


@dataclass(frozen=True)
class StepResult:
    """One of the three writes, and what the catalog did with it."""

    step: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class WriteResult:
    """What happened when a claim artifact was pushed to the catalog.

    `ok=False` carries the reason, and `failed_step` names WHICH of the three writes did not
    land — which is the whole point of it not being a boolean. The write cannot be atomic
    (upsert, then report, then tag), so "it failed" is not actionable on its own: a failed
    `report` leaves a claim with no verdict, while a failed `tag` leaves a claim whose
    verdict is perfectly correct and merely not yet findable by search. Different facts.

    An approval whose write-back failed is NOT reported as a success. The approval still
    stands — a human did decide — but the catalog does not know, the store records exactly
    that, and `POST /audit/{run_id}/writeback` re-runs it. A silent failure here would leave
    the catalog disagreeing with the audit history and nobody any the wiser.
    """

    ok: bool
    target_urn: str
    detail: str = ""
    claim_urn: str = ""
    steps: tuple[StepResult, ...] = ()

    @property
    def failed_step(self) -> str | None:
        """Which write did not land: `upsert`, `report`, `tag` — or None if all did."""
        for step in self.steps:
            if not step.ok:
                return step.step
        return None

    def __str__(self) -> str:
        if self.ok:
            return f"written to {self.target_urn}"
        step = self.failed_step
        where = f" at {step}" if step else ""
        return f"failed{where}: {self.detail}"


# --- bootstrap ----------------------------------------------------------------


def ensure_definitions(client: DataHubClient) -> tuple[str, ...]:
    """Define Attest's structured properties, and RECONCILE the ones already there.

    Attest bootstraps its own write surface. The ingestion CLI is for seed data, and a
    service that cannot write its results without someone first running a recipe by hand is
    not deployable.

    **A property that already exists has its description brought up to date, and skipping
    that was a real bug.** This used to `continue` on any existing property, so a definition
    created once was frozen forever and every later correction to its text was a no-op in
    the code and invisible in the catalog. Found by looking, in Session 16:

        attest.verdict    live description: "test"

    A placeholder, on Attest's most-read surface, for as long as the property has existed —
    while `writeback.py` carried three careful sentences nobody could see. Session 15 then
    rewrote those sentences to stop the badge overstating its coverage (design §6), the
    property already existed, and the catalog never heard about it. The fix was written, was
    committed, and did nothing.

    That is the loud-then-silent shape this project keeps finding: the code says the right
    thing, the surface says the old thing, and nothing fails. So the description is now
    reconciled on every call rather than only at creation — one extra mutation per property
    on the first write-back after the text changes, and none afterwards.

    Returns what it created or updated.
    """
    touched: list[str] = []
    for prop in PROPERTIES:
        existing = client.get_structured_property(prop.urn)
        if existing is None:
            client.create_structured_property(
                qualified_name=prop.qualified_name,
                display_name=prop.display_name,
                description=prop.description,
            )
            touched.append(prop.qualified_name)
            log.info("defined structured property %s", prop.qualified_name)
            continue

        definition = existing.get("definition") or {}
        if definition.get("description") == prop.description:
            continue
        # The catalog is showing something other than what this file says. The file wins:
        # it is the reviewed text, and the catalog's copy is whatever happened to be
        # written first.
        client.update_structured_property(
            urn=prop.urn,
            display_name=prop.display_name,
            description=prop.description,
        )
        touched.append(prop.qualified_name)
        log.info("refreshed the definition of %s", prop.qualified_name)
    return tuple(touched)


def ensure_verdict_tags(client: DataHubClient) -> tuple[str, ...]:
    """Create the three verdict tags if they are not there yet.

    The latest verdict is filterable ONLY as a tag: assertions do not support structured
    properties, and of an assertion's own fields only `customType` is indexed. So "every
    contradicted claim in the catalog" is a tag query, and the tags have to exist before one
    can be applied. Bootstrapped here for the same reason the properties are.

    `createTag` on an existing tag is a no-op that reports the URN, so this is idempotent.
    """
    created: list[str] = []
    for verdict in VERDICTS:
        tag_id = f"Attest-{verdict}"
        try:
            client.create_tag(
                tag_id=tag_id,
                name=tag_id,
                description=(
                    f"Attest's latest verdict on a claim: {verdict}. Applied to the claim's "
                    f"assertion, not to the dataset — one claim, one artifact, one verdict."
                ),
            )
            created.append(tag_id)
        except DataHubError:
            # Already defined. The mutation is not reliably idempotent across versions, and
            # a tag that exists is exactly the state we wanted.
            continue
    return tuple(created)


# --- the write ----------------------------------------------------------------


def write_claim_artifact(
    client: DataHubClient,
    claim: Mapping[str, Any],
    verdict: str,
    run_id: str,
    checked_at: datetime,
    evidence: Sequence[Mapping[str, Any]] = (),
    reviewer: str = "",
    decision: str = "accepted",
    source_agent: str = "",
    snapshot_id: str = "",
    external_url: str = "",
) -> WriteResult:
    """Attach one approved claim, and its verdict, to its dataset.

    Three writes, in this order, and the order is chosen so a partial failure degrades
    DOWNWARD rather than falsely:

        1. upsert  the claim artifact         fails -> nothing exists. Nothing false.
        2. report  the verdict                fails -> claim with NO verdict: reads INCOMPLETE.
        3. tag     the latest verdict         fails -> verdict is CORRECT, merely not yet
                                                       findable by search. The tag is a
                                                       derived index, never the truth.

    **`checked_at` HAS NO DEFAULT, AND THAT IS THE POINT.** It is the audit run's own
    timestamp and it keys the run event. Defaulting it to `now()` would make every retry
    append a duplicate verdict to an append-only history — a retry inventing an audit that
    never happened, invisible until the retry path runs, with every test green. So the
    caller must produce it, and the only honest source is the run's stored `created_at`.

    A DataHub failure is returned, not raised: the approval is a human decision and it
    happened, whatever the catalog did with it afterwards.
    """
    urn = claim_urn(claim)
    target_urn = str(claim.get("target_urn", ""))
    claim_type = str(claim.get("claim_type", ""))
    steps: list[StepResult] = []

    def failed(step: str, exc: Exception) -> WriteResult:
        steps.append(StepResult(step=step, ok=False, detail=str(exc)))
        log.error("write-back of %s to %s failed at %s: %s", urn, target_urn, step, exc)
        return WriteResult(
            ok=False,
            target_urn=target_urn,
            detail=str(exc),
            claim_urn=urn,
            steps=tuple(steps),
        )

    # 1. The claim.
    try:
        client.upsert_custom_assertion(
            urn=urn,
            entity_urn=target_urn,
            custom_type=custom_type_for(claim_type),
            # The description leads with the verdict because DataHub's OWN UI renders an
            # Insufficient-Coverage claim as an "error" (the native type is a lossy
            # projection and result.error is silently discarded). This is the one place the
            # native surface lies, so the description tells the truth beside it. It is
            # refreshed on every write, exactly like the tag: current state, not identity.
            description=f"{verdict} — {claim.get('raw_text') or claim_type}",
            platform_urn=ATTEST_PLATFORM_URN,
            logic=json.dumps(
                {
                    "claim_id": claim_id(claim),
                    "claim_type": claim_type,
                    "target_urn": target_urn,
                    "grain": grain_of(claim),
                    "field_path": claim.get("field_path"),
                    "asserted": {
                        k: v
                        for k, v in claim.items()
                        if k not in {"target_urn", "claim_type", "raw_text"}
                    },
                    "raw_text": claim.get("raw_text", ""),
                    "policy_version": POLICY_VERSION,
                },
                sort_keys=True,
            ),
            external_url=external_url,
        )
        steps.append(StepResult(step=UPSERT, ok=True))
    except DataHubError as exc:
        return failed(UPSERT, exc)

    # 2. The verdict.
    try:
        client.report_assertion_result(
            urn=urn,
            result_type=NATIVE_RESULT_TYPE.get(verdict, "ERROR"),
            # The run's timestamp, never the clock. See the docstring.
            timestamp_millis=int(checked_at.timestamp() * 1000),
            properties={
                # AUTHORITATIVE. Never inferred from result.type or the rollup counts.
                VERDICT_KEY: verdict,
                CLAIM_TYPE_KEY: claim_type,
                AUDIT_RUN_KEY: run_id,
                REVIEWER_KEY: reviewer or "unknown",
                DECISION_KEY: decision,
                GRAIN_KEY: grain_of(claim),
                POLICY_KEY: POLICY_VERSION,
                # Structured JSON, so a value=None row (the catalog is silent) survives to a
                # store=None reader instead of being dropped in a rendered string. See
                # `_dump_evidence`, and Session 21 gap 2.
                **({EVIDENCE_KEY: _dump_evidence(evidence)} if evidence else {}),
                **({SNAPSHOT_KEY: snapshot_id} if snapshot_id else {}),
                **({f"{NAMESPACE}.source_agent": source_agent} if source_agent else {}),
            },
            external_url=external_url,
        )
        steps.append(StepResult(step=REPORT, ok=True))
    except DataHubError as exc:
        return failed(REPORT, exc)

    # 3. The latest verdict, as the one filterable dimension.
    try:
        _swap_verdict_tag(client, urn, verdict)
        steps.append(StepResult(step=TAG, ok=True))
    except DataHubError as exc:
        return failed(TAG, exc)

    log.info("wrote claim artifact %s (%s) for %s (run %s)", urn, verdict, target_urn, run_id)
    return WriteResult(ok=True, target_urn=target_urn, claim_urn=urn, steps=tuple(steps))


def _swap_verdict_tag(client: DataHubClient, assertion_urn: str, verdict: str) -> None:
    """Make the artifact carry exactly one verdict tag: the current one.

    The tag is CURRENT STATE, not history — the history is the run events. So a verdict that
    flipped must not leave the old tag behind, or "every contradicted claim" answers with a
    claim that is now Supported.
    """
    for other in VERDICTS:
        if other == verdict:
            continue
        try:
            client.remove_tag(verdict_tag_urn(other), assertion_urn)
        except DataHubError:
            # Not applied in the first place. Removing what is not there is the state we want.
            continue

    try:
        client.add_tag(verdict_tag_urn(verdict), assertion_urn)
    except DataHubError:
        # The verdict tags are not defined yet. Attest bootstraps its own write surface —
        # a service that cannot write its results until someone runs a recipe by hand is not
        # deployable — and it does it HERE, lazily, rather than on every write: `createTag`
        # refuses an existing tag ("This Tag already exists!"), so an eager bootstrap would
        # throw three caught errors per approval forever. This costs one extra call once, on
        # the first write-back against a fresh catalog, and nothing afterwards.
        #
        # Found live: the first real write-back failed at exactly this step with "Failed to
        # validate label with urn urn:li:tag:Attest-Contradicted. Urn does not exist." The
        # claim and its verdict had already landed, which is the ordering working as designed
        # — but a verdict search cannot find is half a feature.
        ensure_verdict_tags(client)
        client.add_tag(verdict_tag_urn(verdict), assertion_urn)


def write_verdict(
    client: DataHubClient,
    target_urn: str,
    verdict: str,
    claim_type: str,
    run_id: str,
    source_agent: str = "",
    checked_at: datetime | None = None,
) -> WriteResult:
    """Attach the dataset-level badge: the latest verdict, at a glance. One mutation.

    This is NOT the audit record any more — `write_claim_artifact` is. This is the
    convenience view on the dataset's own property panel, and it stays last-write-wins,
    which is now honest rather than lossy: the subject-bearing record is the claim artifact,
    and a dataset carrying several claims points at all of them from its Assertions.

    Never called on its own initiative — see the module docstring.
    """
    when = (checked_at or datetime.now(tz=UTC)).isoformat()
    try:
        ensure_definitions(client)
        client.set_structured_properties(
            target_urn,
            {
                VERDICT.urn: [verdict],
                CLAIM_TYPE.urn: [claim_type],
                CHECKED_AT.urn: [when],
                SOURCE_AGENT.urn: [source_agent or "unknown"],
                AUDIT_RUN.urn: [run_id],
            },
        )
    except DataHubError as exc:
        log.error("badge write-back to %s failed: %s", target_urn, exc)
        return WriteResult(ok=False, target_urn=target_urn, detail=str(exc))

    return WriteResult(ok=True, target_urn=target_urn)


# --- evidence, structured (Session 21) ----------------------------------------
#
# The write-back used to flatten a verdict's evidence to a `field=value; ...` string and DROP
# every row whose value was None. That was gap 2: an Insufficient-Coverage verdict's evidence
# IS the absence — `Evidence(field="properties.lastModified.time", value=None)` is the catalog
# being silent, which is the whole justification for the third verdict — and a reader
# inheriting the artifact from DataHub alone got back a rendered string with the absence gone.
# So the inherited artifact for the most load-bearing verdict was lamer than the one Attest
# computed. Now the evidence travels as JSON in the run event's nativeResults, and `value=None`
# round-trips as None, kept distinct from '' and [] — the absent-vs-empty distinction snapshot.py
# exists to preserve, preserved all the way to a store=None reader.


@dataclass(frozen=True)
class EvidenceItem:
    """One catalog field a verdict was decided against, as a reader inherits it.

    `value=None` is the catalog being SILENT — the absence that justifies
    Insufficient-Coverage — and it round-trips as None, never collapsed to '' or []. That
    fidelity is the point of the structured round-trip; see `_dump_evidence`.
    """

    field: str
    value: Any = None
    note: str | None = None


def _dump_evidence(items: Sequence[Mapping[str, Any]]) -> str:
    """Serialize a verdict's evidence for a run event's nativeResults, absence and all.

    JSON, not the old `field=value; ...` rendering. A rendered string dropped every `value is
    None` row and flattened the rest, so a reader could not tell the catalog said NOTHING from
    the catalog saying empty — exactly the collapse this project exists to refuse, committed in
    its own write path. JSON `null` keeps None apart from '' and [].
    """
    return json.dumps(
        [
            {"field": e.get("field", ""), "value": e.get("value"), "note": e.get("note")}
            for e in items
        ],
        sort_keys=True,
    )


def _parse_evidence(raw: str | None) -> tuple[EvidenceItem, ...]:
    """Structured evidence back out of a run event. The inverse of `_dump_evidence`.

    A JSON `null` value decodes to Python `None` — the absence, preserved. A pre-Session-21
    artifact carried a rendered string here instead of JSON; that is not dropped but wrapped as
    a single legacy item, so an older artifact stays legible rather than reading as empty.
    """
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return (EvidenceItem(field="", value=raw, note="legacy rendered evidence"),)
    if not isinstance(data, list):
        return (EvidenceItem(field="", value=data),)
    return tuple(
        EvidenceItem(
            field=str(d.get("field", "")),
            value=d.get("value"),
            note=d.get("note"),
        )
        for d in data
        if isinstance(d, dict)
    )


# --- reads --------------------------------------------------------------------


@dataclass(frozen=True)
class VerdictEvent:
    """One verdict a claim has had, at one point in time."""

    at: int
    verdict: str
    audit_run: str = ""
    reviewer: str = ""
    decision: str = ""
    # STRUCTURED, and absence-preserving (Session 21). A `value=None` item is the catalog
    # being silent, kept distinct from empty all the way to a store=None reader.
    evidence: tuple[EvidenceItem, ...] = ()
    # The identity of the catalog snapshot this verdict was decided against — "this held
    # against the catalog as it stood when this run read it". Captured at audit time and
    # stored; never recomputed at write-back (a re-fetch would violate the same-snapshot rule).
    snapshot_id: str = ""
    native_type: str = ""


@dataclass(frozen=True)
class ClaimArtifact:
    """One claim as the catalog holds it: what it asserts, and every verdict it has had.

    `complete` is False when the artifact exists but carries no verdict — the write got as
    far as the claim and not as far as the verdict. It is NOT a claim the catalog declined
    to answer: THAT is Insufficient-Coverage, which is a verdict and reads as one. The two
    are easy to confuse from DataHub's rollup (both show succeeded=0 failed=0) and this type
    keeps them apart by construction.
    """

    claim_urn: str
    target_urn: str
    claim_type: str
    grain: str
    description: str
    logic: dict[str, Any]
    history: tuple[VerdictEvent, ...] = ()
    tags: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.history)

    @property
    def verdict(self) -> str | None:
        """The latest verdict, or None if the artifact was never finished.

        Read off the STORED value, never off `result.type` or the rollup counts.
        """
        return self.history[0].verdict if self.history else None


def artifact_from_graphql(node: Mapping[str, Any]) -> ClaimArtifact:
    info = node.get("info") or {}
    custom = info.get("customAssertion") or {}
    try:
        logic = json.loads(custom.get("logic") or "{}")
    except json.JSONDecodeError:
        logic = {}

    events: list[VerdictEvent] = []
    for ev in ((node.get("runEvents") or {}).get("runEvents") or []):
        result = ev.get("result") or {}
        native = {n["key"]: n["value"] for n in (result.get("nativeResults") or [])}
        verdict = native.get(VERDICT_KEY)
        if not verdict:
            # Not Attest's, or written before the payload existed. A run event with no
            # stored verdict is not a verdict — refusing to infer one from result.type is
            # the whole rule.
            continue
        events.append(
            VerdictEvent(
                at=ev.get("timestampMillis") or 0,
                verdict=verdict,
                audit_run=native.get(AUDIT_RUN_KEY, ""),
                reviewer=native.get(REVIEWER_KEY, ""),
                decision=native.get(DECISION_KEY, ""),
                evidence=_parse_evidence(native.get(EVIDENCE_KEY)),
                snapshot_id=native.get(SNAPSHOT_KEY, ""),
                native_type=result.get("type") or "",
            )
        )
    events.sort(key=lambda e: e.at, reverse=True)

    return ClaimArtifact(
        claim_urn=node.get("urn", ""),
        target_urn=custom.get("entityUrn", ""),
        claim_type=str(logic.get("claim_type", "")),
        grain=str(logic.get("grain", "")),
        description=info.get("description") or "",
        logic=logic,
        history=tuple(events),
        tags=tuple(
            t["tag"]["urn"] for t in ((node.get("tags") or {}).get("tags") or [])
        ),
    )


def read_claim_artifact(client: DataHubClient, urn: str) -> ClaimArtifact | None:
    """One claim artifact from the catalog, or None if it is not there."""
    node = client.get_assertion(urn)
    if not node:
        return None
    return artifact_from_graphql(node)


def read_dataset_claims(client: DataHubClient, dataset_urn: str) -> tuple[ClaimArtifact, ...]:
    """Every Attest claim on a dataset, from DataHub alone. THE THESIS QUERY.

    This is the answer to "show me what the next agent inherits": every claim ever made
    about this dataset, what it asserted, at what grain, and every verdict it has ever had —
    with no Attest process running and no access to Attest's store.

    `limit=None`: the thesis query returns EVERYTHING on the dataset, paginating past the
    50-item page. A capped "everything" would be the silent-absence bug this helper is the
    whole point of avoiding.
    """
    nodes, _total = client.list_dataset_assertions(dataset_urn, limit=None)
    return tuple(
        artifact_from_graphql(node)
        for node in nodes
        if (
            (node.get("info") or {}).get("customAssertion") or {}
        ).get("type", "").startswith(CUSTOM_TYPE_PREFIX)
    )


def find_datasets(client: DataHubClient, verdict: str) -> tuple[str, ...]:
    """Datasets whose latest Attest badge is `verdict`.

    The dataset-level badge, not the claim artifacts. Proves the badge is indexed, which is
    the point of writing it as a structured property rather than as text.
    """
    result = client.search_by_structured_property(VERDICT.qualified_name, verdict)
    return tuple(
        r["entity"]["urn"] for r in result.get("searchResults") or [] if r.get("entity")
    )
