"""The claim artifact, and the six ways it would silently rot.

Every test here BREAKS something. That is the point, and it is the house rule: a guard that
only ever passes is a green light wired to nothing.

The write-back stopped being one atomic mutation and became three (upsert the claim, report
the verdict, swap the verdict tag), which buys per-claim artifacts and costs atomicity. The
whole recovery story rests on those three being IDEMPOTENT, and idempotence rests on two
things that are easy to "tidy" away and impossible to notice once gone:

    1. the claim's URN is derived from the claim's CONTENT      -> a re-run hits the same artifact
    2. the run event is keyed by the AUDIT RUN's timestamp      -> a re-report hits the same row

Break either and a retry stops repairing and starts DUPLICATING — a second artifact for a
claim, or a second verdict in an append-only history that never happened. Both are invisible
until the retry path runs, and both corrupt the record this project exists to keep. So each
has a test that does the wrong thing on purpose and demands it fail.

WHAT THESE TESTS CANNOT DO, said plainly: the fake has no search index, no timeseries
aspect, and no MCP validation, so the index lag and the `fieldPath` trap do not exist here.
They are server machinery a fake does not have — the Session 5 rule — which is why
`test_live.py::test_two_claims_on_one_dataset_coexist_in_the_catalog` is the real evidence
for this feature and these tests are the regression net around its shape.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from attest import writeback
from attest.datahub import DataHubError
from fakes import FakeDataHub, dataset

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
ALICE = "urn:li:corpuser:alice.chen"
RUN_AT = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def ownership_claim(owner: str = ALICE, urn: str = SF) -> dict:
    return {
        "claim_type": "ownership",
        "target_urn": urn,
        "raw_text": f"{urn} is owned by {owner}.",
        "owner_urn": owner,
    }


def classification_claim(field_path: str | None = "email") -> dict:
    return {
        "claim_type": "classification",
        "target_urn": SF,
        "raw_text": f"the {field_path} column contains PII.",
        "labels": ["urn:li:tag:PII"],
        "present": True,
        "field_path": field_path,
    }


@pytest.fixture
def catalog() -> FakeDataHub:
    return FakeDataHub({SF: dataset(SF, owners=(ALICE,))})


def write(catalog: FakeDataHub, claim: dict, verdict: str = "Contradicted", **kw):
    return writeback.write_claim_artifact(
        catalog,
        claim=claim,
        verdict=verdict,
        run_id=kw.pop("run_id", "run-1"),
        checked_at=kw.pop("checked_at", RUN_AT),
        **kw,
    )


# --- the four break-it tests --------------------------------------------------


def test_reporting_a_verdict_twice_leaves_ONE_run_event(catalog):
    """THE RETRY GUARANTEE. A repaired write must not duplicate the history.

    A run event is keyed by (assertion urn, timestampMillis). Write the same claim's verdict
    twice from the same run and the second must land on the SAME row — because the repair
    for a partial write is simply to run the whole thing again, and a retry that appended
    would write a second verdict into an append-only log for an audit that happened once.

    That is not a cosmetic duplicate. The history is the evidence: two events say the claim
    was checked twice and agreed twice. One retry would forge that.
    """
    claim = ownership_claim()
    first = write(catalog, claim)
    second = write(catalog, claim)

    assert first.ok and second.ok
    assert first.claim_urn == second.claim_urn

    artifact = writeback.read_claim_artifact(catalog, first.claim_urn)
    assert len(artifact.history) == 1, (
        f"re-running the write-back appended a duplicate verdict: {artifact.history}. "
        "The run event must be keyed by the AUDIT RUN's timestamp, not by the clock."
    )
    # Pin the KEY too, not just the count. Sabotaging the timestamp to `now()` let this test
    # pass by luck: two writes microseconds apart collided in the same millisecond and
    # collapsed anyway. The count is only evidence of idempotence if the key is the run's.
    assert artifact.history[0].at == int(RUN_AT.timestamp() * 1000)


def test_writing_a_claim_twice_leaves_ONE_artifact(catalog):
    """THE IDENTITY GUARANTEE. A re-run lands on the artifact that is already there.

    The claim's URN is content-addressed, so the same claim always computes the same URN and
    the upsert updates rather than mints. If the URN ever carried a run id, an attempt
    number, or a timestamp, every retry would leave a NEW artifact and one claim would read
    as several — the exact "two claims about one dataset" the feature exists to represent,
    forged out of one claim written twice.
    """
    claim = ownership_claim()
    write(catalog, claim, run_id="run-1")
    write(catalog, claim, run_id="run-2-a-completely-different-run")

    artifacts = writeback.read_dataset_claims(catalog, SF)
    assert len(artifacts) == 1, (
        f"one claim produced {len(artifacts)} artifacts: {[a.claim_urn for a in artifacts]}. "
        "The claim urn must derive from the claim's CONTENT and nothing else."
    )


def test_fieldPath_is_never_sent_to_the_catalog(catalog):
    """A column-grain claim must NOT set the assertion's fieldPath. Ever.

    Setting it makes DataHub record the assertee as a schemaField URN while the run-event
    aspect requires a dataset URN, so the artifact is created, reads back perfectly, and can
    then never carry a verdict — the one thing it exists to carry. Measured on the real
    server (spikes/claim_artifact_probe.py §8).

    This is asserted at the WRITE rather than at the symptom, deliberately: whether the trap
    fires depends on what the index holds at that instant, so it sometimes appears to work.
    An assertion on the runtime symptom would be flaky and would teach people to re-run until
    green. The property that holds every time is that we never send the field.
    """
    result = write(catalog, classification_claim(field_path="email"))
    assert result.ok

    assert catalog.upserts, "nothing was upserted"
    for call in catalog.upserts:
        assert "fieldPath" not in call, (
            f"fieldPath reached the catalog: {call}. It permanently blocks the verdict."
        )

    # The grain is not lost, merely carried somewhere that works.
    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
    assert artifact.grain == "column"
    assert artifact.logic["field_path"] == "email"


def test_a_half_written_claim_reads_as_INCOMPLETE_not_as_verdictless(catalog):
    """Upsert lands, the verdict does not: the artifact must say so.

    A claim that exists with no verdict is a partial write, and it must be visibly that —
    never a claim the catalog quietly has no opinion about. The distinction is the whole
    product: "the catalog is silent" is Insufficient-Coverage, a real and final verdict.
    "Attest never finished writing" is a bug. DataHub's own rollup cannot tell them apart
    (both read succeeded=0 failed=0), so `complete` is computed from whether a verdict was
    STORED, not from the counts.
    """
    claim = ownership_claim()
    urn = writeback.claim_urn(claim)

    # The claim lands; the verdict write fails.
    catalog.upsert_custom_assertion(
        urn=urn,
        entity_urn=SF,
        custom_type=writeback.custom_type_for("ownership"),
        description="Contradicted — half-written",
        platform_urn=writeback.ATTEST_PLATFORM_URN,
        logic=json.dumps({"claim_type": "ownership", "grain": "table"}),
    )

    artifact = writeback.read_claim_artifact(catalog, urn)
    assert artifact is not None, "the artifact should exist — the upsert succeeded"
    assert artifact.complete is False
    assert artifact.verdict is None, "a half-written claim must not report a verdict"

    # And it is NOT the same thing as the catalog being silent, which IS a verdict.
    silent = write(catalog, ownership_claim(owner="urn:li:corpuser:bob"),
                   verdict="Insufficient-Coverage")
    ic = writeback.read_claim_artifact(catalog, silent.claim_urn)
    assert ic.complete is True
    assert ic.verdict == "Insufficient-Coverage"


# --- the two idempotency-rot guards -------------------------------------------


def test_claim_identity_cannot_depend_on_a_run_or_a_clock(catalog):
    """ROT GUARD 1: nothing but the claim may reach `claim_id`.

    Structural, not disciplinary. `claim_id` is handed the claim and nothing else, so a run
    id, an attempt counter, or `now()` cannot get into the identity even by accident — there
    is no parameter to pass one through. This test pins that signature, because the natural
    "improvement" is to make ids unique per write, which is correct for a log and fatal for
    a content-addressed artifact.
    """
    params = list(inspect.signature(writeback.claim_id).parameters)
    assert params == ["claim"], (
        f"claim_id takes {params}. It must take the CLAIM and nothing else: anything else in "
        "the signature is something that can leak into the identity and break the retry."
    )

    claim = ownership_claim()
    assert writeback.claim_id(claim) == writeback.claim_id(claim)
    # Key order must not move the id — canonical JSON, not dict order.
    assert writeback.claim_id(claim) == writeback.claim_id(dict(reversed(list(claim.items()))))
    # The prose is not the claim. Re-phrasing the sentence must not mint a second artifact.
    rephrased = {**claim, "raw_text": "Alice owns this table."}
    assert writeback.claim_id(rephrased) == writeback.claim_id(claim)
    # ...but what it ASSERTS is the claim, and a different assertion is a different claim.
    other_owner = {**claim, "owner_urn": "urn:li:corpuser:bob"}
    assert writeback.claim_id(other_owner) != writeback.claim_id(claim)


def test_write_claim_artifact_refuses_to_default_its_timestamp():
    """ROT GUARD 2: `checked_at` must have NO default.

    The run event is keyed by the timestamp, so `checked_at` is what makes a retry collapse
    onto one row. Give it a `now()` default and every retry mints a new key and appends a
    duplicate verdict — a repair inventing an audit that never happened, invisible until the
    retry path runs, with every other test green.

    So it is unreachable rather than forbidden: the caller cannot omit it. This pins the
    signature because a default is exactly the kind of convenience a future session adds
    while "tidying".
    """
    signature = inspect.signature(writeback.write_claim_artifact)
    checked_at = signature.parameters["checked_at"]
    assert checked_at.default is inspect.Parameter.empty, (
        "checked_at has a default. It must be REQUIRED: it keys the run event, and a "
        "clock-based default turns every retry into a duplicate verdict."
    )

    with pytest.raises(TypeError):
        writeback.write_claim_artifact(  # type: ignore[call-arg]
            FakeDataHub({}), claim=ownership_claim(), verdict="Supported", run_id="r"
        )


def test_the_timestamp_that_is_written_is_the_runs_not_the_clock(catalog):
    """The other half of rot guard 2: the value must actually BE the run's.

    A required parameter that the code then ignores would pass the signature test and fail
    in production. So: two writes of one claim from one run, and the event lands at the RUN's
    timestamp — not near it, ON it.
    """
    result = write(catalog, ownership_claim(), checked_at=RUN_AT)
    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)

    assert artifact.history[0].at == int(RUN_AT.timestamp() * 1000)

    # A DIFFERENT run of the same claim is a genuinely new verdict, and appends.
    later = RUN_AT + timedelta(days=1)
    write(catalog, ownership_claim(), verdict="Supported", run_id="run-2", checked_at=later)
    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
    assert len(artifact.history) == 2, "a re-audit on a later date must APPEND to the history"
    assert artifact.verdict == "Supported", "the latest verdict is the most recent event"
    assert [e.verdict for e in artifact.history] == ["Supported", "Contradicted"]


# --- the artifact's payload ---------------------------------------------------


def test_the_verdict_is_stored_verbatim_and_never_inferred(catalog):
    """All three verdicts survive the round trip as their literal selves.

    The native result type is a lossy projection: Insufficient-Coverage maps to ERROR, which
    is also what a genuine failure would look like, and DataHub's rollup puts it in neither
    the passed nor the failed bucket. So the verdict is read from the STORED value. This pins
    that the stored value is there for every verdict, including the third.
    """
    for verdict in ("Supported", "Contradicted", "Insufficient-Coverage"):
        claim = ownership_claim(owner=f"urn:li:corpuser:{verdict.lower()}")
        result = write(catalog, claim, verdict=verdict)
        artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
        assert artifact.verdict == verdict
        assert writeback.verdict_tag_urn(verdict) in artifact.tags


def test_a_run_event_with_no_stored_verdict_yields_NO_verdict(catalog):
    """THE "NEVER INFER" GUARD. A run event without `attest.verdict` is not a verdict.

    This is the one the rollup tempts you into. `result.type` is always present and always
    looks answerable — SUCCESS/FAILURE/ERROR — so recovering a verdict from it when the
    stored value is missing feels like graceful degradation. It is fabrication: ERROR means
    Insufficient-Coverage when Attest wrote it and means nothing of the sort otherwise, and
    the reader cannot tell which from the type alone.

    An event with no stored verdict is not Attest's, or predates the payload. The honest
    answer is "this claim has no verdict I can vouch for", which reads as INCOMPLETE — not a
    plausible verdict inferred from a bucket. Absence read as an answer is the exact mistake
    this project exists to catch, and here it would be Attest making it.
    """
    claim = ownership_claim()
    result = write(catalog, claim, verdict="Contradicted")

    # Something else reports a result on this artifact: a real type, no Attest payload.
    catalog.report_assertion_result(
        urn=result.claim_urn,
        result_type="SUCCESS",
        timestamp_millis=int((RUN_AT + timedelta(days=2)).timestamp() * 1000),
        properties={"some.other.tool": "ran here"},
    )

    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
    verdicts = [e.verdict for e in artifact.history]
    assert verdicts == ["Contradicted"], (
        f"history is {verdicts}: a run event carrying no attest.verdict was read as a "
        "verdict. SUCCESS is not 'Supported' unless Attest stored that it was."
    )


def test_an_artifact_with_only_unattributed_events_is_INCOMPLETE(catalog):
    """The same rule at the edge: no stored verdict anywhere means no verdict at all."""
    claim = ownership_claim()
    urn = writeback.claim_urn(claim)
    catalog.upsert_custom_assertion(
        urn=urn,
        entity_urn=SF,
        custom_type=writeback.custom_type_for("ownership"),
        description="not Attest's verdict",
        platform_urn=writeback.ATTEST_PLATFORM_URN,
        logic=json.dumps({"claim_type": "ownership", "grain": "table"}),
    )
    catalog.report_assertion_result(
        urn=urn, result_type="FAILURE",
        timestamp_millis=int(RUN_AT.timestamp() * 1000),
        properties={"other.tool.verdict": "failed"},
    )

    artifact = writeback.read_claim_artifact(catalog, urn)
    assert artifact.complete is False, "a FAILURE with no attest.verdict became a verdict"
    assert artifact.verdict is None


def test_a_verdict_that_flips_swaps_its_tag_rather_than_accumulating(catalog):
    """The tag is CURRENT STATE. A claim must never carry two verdicts at once.

    The history lives in the run events; the tag exists only so "every contradicted claim" is
    a real catalog query. Leave the old tag behind and that query answers with a claim that
    is now Supported — a search index quietly lying about a verdict the artifact itself
    reports correctly.
    """
    claim = ownership_claim()
    write(catalog, claim, verdict="Contradicted", run_id="run-1", checked_at=RUN_AT)
    result = write(catalog, claim, verdict="Supported", run_id="run-2",
                   checked_at=RUN_AT + timedelta(days=1))

    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
    verdict_tags = [t for t in artifact.tags if t.startswith("urn:li:tag:Attest-")]
    assert verdict_tags == [writeback.verdict_tag_urn("Supported")], (
        f"the claim carries {verdict_tags}: a stale verdict tag makes search report the "
        "wrong verdict for a claim the artifact itself gets right."
    )


def test_two_different_claims_on_one_dataset_coexist(catalog):
    """THE INVARIANT, offline. Neither claim overwrites the other."""
    a = write(catalog, ownership_claim(), verdict="Contradicted")
    b = write(catalog, classification_claim(), verdict="Supported")

    assert a.claim_urn != b.claim_urn
    artifacts = {x.claim_urn: x for x in writeback.read_dataset_claims(catalog, SF)}
    assert len(artifacts) == 2
    assert artifacts[a.claim_urn].verdict == "Contradicted"
    assert artifacts[b.claim_urn].verdict == "Supported"
    assert artifacts[a.claim_urn].claim_type == "ownership"
    assert artifacts[b.claim_urn].claim_type == "classification"


# --- the failed step ----------------------------------------------------------


def test_the_retry_completes_a_half_written_claim_without_duplicating_it(catalog, monkeypatch):
    """THE RECOVERY PATH, end to end: repair by repetition.

    The verdict write fails, leaving a claim with no verdict. Re-running the whole write-back
    — which is all the retry endpoint does — must finish it and leave exactly ONE artifact
    with exactly ONE verdict. Not a second artifact (the URN is content-addressed) and not a
    second run event (the timestamp is the run's).

    This is why the design needed no saga: the repair is the original operation, run again.
    """
    claim = ownership_claim()
    calls = {"n": 0}
    real = catalog.report_assertion_result

    def fail_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DataHubError("the catalog did not index the assertion in time")
        return real(*args, **kwargs)

    monkeypatch.setattr(catalog, "report_assertion_result", fail_once)

    first = write(catalog, claim)
    assert first.ok is False and first.failed_step == writeback.REPORT
    half = writeback.read_claim_artifact(catalog, first.claim_urn)
    assert half.complete is False

    # The retry: the same call, again. Nothing else.
    second = write(catalog, claim)
    assert second.ok is True
    assert second.claim_urn == first.claim_urn, "the retry minted a DIFFERENT artifact"

    artifacts = writeback.read_dataset_claims(catalog, SF)
    assert len(artifacts) == 1, "the retry left a duplicate claim"
    assert artifacts[0].complete is True
    assert artifacts[0].verdict == "Contradicted"
    assert len(artifacts[0].history) == 1, "the retry appended a duplicate verdict"


def test_a_failed_write_names_the_step_that_failed(catalog):
    """`WriteResult` is not a boolean, and the reason is that "it failed" is not actionable.

    A failed `report` left a claim with no verdict; a failed `tag` left a verdict that is
    entirely correct and merely not findable by search. Different facts, different urgency.
    """
    broken = FakeDataHub({SF: dataset(SF)}, fail=True)
    result = write(broken, ownership_claim())

    assert result.ok is False
    assert result.failed_step == writeback.UPSERT
    assert "failed at upsert" in str(result)


def test_a_failed_verdict_write_is_named_as_the_report_step(catalog, monkeypatch):
    """The middle step is the one that fails in the real world (the index lag), so pin it."""

    def refuse(*args, **kwargs):
        raise DataHubError("the catalog did not index the assertion in time")

    monkeypatch.setattr(catalog, "report_assertion_result", refuse)
    result = write(catalog, ownership_claim())

    assert result.ok is False
    assert result.failed_step == writeback.REPORT
    # The claim landed even though its verdict did not — and that is exactly the state the
    # retry endpoint exists to repair, and the read path must show as INCOMPLETE.
    artifact = writeback.read_claim_artifact(catalog, result.claim_urn)
    assert artifact is not None and artifact.complete is False
