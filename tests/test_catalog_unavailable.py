"""When the CATALOG cannot be reached, not when it disagrees.

Two failures wore the same coat and were handled the same way. `EntityNotFoundError` and
`MalformedResponseError` are facts about the ENTITY — the URN names nothing, or the answer
was structurally broken — and both are correctly surfaced as a `report.ClaimError`: a
malformed question, kept out of the verdict tally. A transport failure is not a fact about
the entity at all. It says Attest never got to ask.

**The bug these were written for reached a user.** DataHub's quickstart accepts the TCP
connection while it is still bootstrapping and closes it before answering, which httpx
reports verbatim as `RemoteProtocolError("Server disconnected without sending a
response")`. Wrapped into a bare `DataHubError`, that was indistinguishable from a bad URN,
so every claim was filed as "could not be checked" and the run settled `awaiting-review`
with `trajectory_ok`, ZERO verdicts, and a UI reading AUDIT COMPLETE. Attest rendering
silence as an answer, in its own output, about its own failure — the cardinal sin of its own
thesis. It is §18's provider bug at the other transport, and §12 predicted the location:
*nothing defends the catalog READ, because the read was the one thing that could be trusted
to mean what it said.*

**It survived a green suite because the fake could not fail that way.** `FakeDataHub` has
had fault injection on its WRITE side since Session 4; the read side — the one that feeds
every verdict — had none, so no offline test could distinguish an unreachable catalog from a
URN that does not exist. That asymmetry is the cover the bug walked through, exactly as
`FakeChat`'s missing `faults` was in Session 24. So these tests are as much about
`FakeCatalog.read_faults` existing as about `CatalogUnavailable` existing.

Offline and free: no server, no key, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from attest import graph as graph_module
from attest.api.app import app, get_service
from attest.api.service import AuditService
from attest.datahub import (
    CatalogUnavailable,
    DataHubClient,
    DataHubError,
    EntityNotFoundError,
    SnapshotCache,
)
from attest.graph import Pipeline
from attest.llm import LLM
from attest.report import RunStatus
from attest.store import AuditStore
from fakes import FakeCatalog, FakeChat, FakeDataHub, claim_reply, dataset, explanation_reply

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
PG = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD)"
CAROL = "urn:li:corpuser:carol.davis"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

SAYS = f"The dataset {SF} is owned by {CAROL}."

# A one-claim run the catalog would SUPPORT, so that anything other than one Supported
# verdict is the failure under test rather than a quirk of the fixture.
SUPPORTED = (
    claim_reply(
        [{"claim_type": "ownership", "target_urn": SF, "raw_text": SAYS, "owner_urn": CAROL}]
    ),
    explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
)

# The exact exception the observed outage produced. httpx's own type, not a stand-in: the
# classification is `isinstance(exc, httpx.TransportError)`, and a hand-rolled exception with
# the right name would prove the code works against a shape httpx does not raise.
DISCONNECTED = httpx.RemoteProtocolError("Server disconnected without sending a response")


def snapshots() -> dict:
    return {
        SF: dataset(
            SF,
            last_modified=NOW - timedelta(hours=6),
            owners=(CAROL,),
            tags=(),
            terms=(),
            custom_properties={},
        )
    }


def build(tmp_path, *replies: str, read_faults: dict[int, Exception] | None = None):
    """A service wired to a catalog that can fail like the real one, and a real database."""
    catalog = FakeDataHub(snapshots(), read_faults=read_faults)
    chat = FakeChat(replies=list(replies))
    service = AuditService(
        pipeline=Pipeline(llm=LLM(client=chat), client=catalog, now=NOW),
        store=AuditStore(tmp_path / "attest.db"),
        client=catalog,
        write_back=False,
    )
    app.dependency_overrides[get_service] = lambda: service
    return service, catalog


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# --- the translation, against real httpx shapes ------------------------------


def wired(handler) -> DataHubClient:
    """A REAL DataHubClient whose socket is a MockTransport.

    `_client` is replaced rather than injected because the client builds its own on purpose
    (one connection pool for the process). Everything under test — the try/except, the status
    branch, the message — is the shipped code path; only the wire is faked, which is the only
    part that cannot be exercised offline.
    """
    client = DataHubClient(gms_url="http://gms.test")
    client._client = httpx.Client(
        base_url="http://gms.test", transport=httpx.MockTransport(handler)
    )
    return client


@pytest.mark.parametrize(
    "fault",
    [
        DISCONNECTED,
        httpx.ConnectError("All connection attempts failed"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectTimeout("timed out"),
        httpx.PoolTimeout("pool"),
    ],
)
def test_a_request_that_never_produced_a_response_is_CatalogUnavailable(fault):
    """The transient line is drawn at `httpx.TransportError`, where httpx draws it.

    A hand-enumerated list of exception types would drift from the library that defines the
    failures. Every one of these means the same thing to a caller — Attest never got an
    answer — and none of them says anything about the entity.
    """

    def handler(request):
        raise fault

    with pytest.raises(CatalogUnavailable) as caught:
        wired(handler).execute("query { x }")

    # It stays a DataHubError, which is what lets writeback and /health keep degrading on it.
    assert isinstance(caught.value, DataHubError)
    # The operator needs to know WHICH catalog, and httpx's own words for why.
    assert "gms.test" in str(caught.value)


@pytest.mark.parametrize("status_code", [500, 502, 503, 504, 429, 408])
def test_a_server_that_could_not_serve_is_CatalogUnavailable(status_code):
    """GMS answers a GraphQL problem with 200 and an `errors` payload.

    So a 5xx here is the server itself and never the query — and never an answer about the
    entity. 429 and 408 join it for the same reason they do in llm.py: waiting is the thing
    that helps.
    """
    with pytest.raises(CatalogUnavailable):
        wired(lambda request: httpx.Response(status_code, text="nope")).execute("query { x }")


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_a_refused_request_is_NOT_dressed_up_as_an_outage(status_code):
    """Waiting cannot fix a bad request. Promising a retry that cannot help wastes time."""
    with pytest.raises(DataHubError) as caught:
        wired(lambda request: httpx.Response(status_code, text="nope")).execute("query { x }")

    assert not isinstance(caught.value, CatalogUnavailable)


def test_a_graphql_errors_payload_is_an_ANSWER_and_stays_a_plain_DataHubError():
    """The server answered. The query was broken, and no amount of waiting mends it."""

    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "Cannot query field 'x'"}]})

    with pytest.raises(DataHubError) as caught:
        wired(handler).execute("query { x }")

    assert not isinstance(caught.value, CatalogUnavailable)
    assert "Cannot query field" in str(caught.value)


# --- the anti-silence rule: the run dies rather than reporting nothing -------


def test_an_unreachable_catalog_kills_the_run_instead_of_filing_every_claim_as_unchecked():
    """THE FIX'S REASON FOR EXISTING, at the pipeline.

    Degrading here returns a report with zero verdicts and a green trajectory. There is no
    honest reading of that: a caller cannot tell it from an agent who made no checkable
    claims, and the one fact that would let anyone act — the catalog was down — is nowhere in
    it.
    """
    catalog = FakeCatalog(snapshots(), read_faults={1: CatalogUnavailable("gms is down")})
    chat = FakeChat(replies=list(SUPPORTED))

    with pytest.raises(CatalogUnavailable):
        Pipeline(llm=LLM(client=chat), client=catalog, now=NOW).run(SAYS)

    # One attempt. Attest adds no retry loop of its own on top of a dead server.
    assert len(catalog.fetched) == 1


def test_a_missing_entity_is_STILL_a_ClaimError_and_the_run_still_completes():
    """The non-regression that matters most: the narrowed catch must not take this with it.

    A hallucinated URN is a fact about the claim, it is surfaced in `errors`, and it must not
    take down an audit whose other claims are perfectly checkable. Narrowing the catch is only
    correct if this stays exactly as it was.
    """
    catalog = FakeCatalog(snapshots())
    missing = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.nope.gone,PROD)"
    chat = FakeChat(
        replies=[
            claim_reply(
                [
                    {
                        "claim_type": "ownership",
                        "target_urn": missing,
                        "raw_text": f"The dataset {missing} is owned by {CAROL}.",
                        "owner_urn": CAROL,
                    }
                ]
            )
        ]
    )
    report = Pipeline(llm=LLM(client=chat), client=catalog, now=NOW).run(
        f"The dataset {missing} is owned by {CAROL}."
    )

    assert report.status is not RunStatus.FLAGGED
    assert len(report.errors) == 1
    assert report.audits == ()
    assert report.trajectory.ok


def test_the_outage_can_land_MID_RUN_and_still_takes_the_whole_report_with_it():
    """A partial report is not a smaller truth, it is a differently-shaped lie.

    Claim 1 was checked against a catalog that answered; claim 2 was not checked at all. A
    report carrying only the first, marked complete, says the audit covered what it covered —
    with no way for a reader to know a claim went missing because the server died halfway.
    """
    # TWO datasets, deliberately: one run resolves each entity exactly once (the consistency
    # boundary in cache.py), so two claims about ONE dataset would make only one fetch and
    # the second fault would never fire.
    both = snapshots()
    both[PG] = dataset(
        PG,
        last_modified=NOW - timedelta(hours=6),
        owners=(),
        tags=(),
        terms=(),
        custom_properties={},
    )
    catalog = FakeCatalog(both, read_faults={2: CatalogUnavailable("gms is down")})
    two = f"{SAYS}\n\nThe dataset {PG} was updated within the last 24 hours."
    chat = FakeChat(
        replies=[
            claim_reply(
                [
                    {
                        "claim_type": "ownership",
                        "target_urn": SF,
                        "raw_text": SAYS,
                        "owner_urn": CAROL,
                    },
                    {
                        "claim_type": "freshness",
                        "target_urn": PG,
                        "raw_text": f"The dataset {PG} was updated within the last 24 hours.",
                        "max_age_hours": 24,
                    },
                ]
            ),
            explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
        ]
    )

    with pytest.raises(CatalogUnavailable):
        Pipeline(llm=LLM(client=chat), client=catalog, now=NOW).run(two)


# --- the API says the one thing that lets someone act ------------------------


def test_an_unreachable_catalog_is_a_503_with_Retry_After_and_never_a_201(tmp_path):
    """Same disposition as ProviderUnavailable, because it is the same fact: try again.

    The 201 this replaces is what a user actually saw — a created run, a green banner, and
    nothing in it.
    """
    _, catalog = build(tmp_path, *SUPPORTED, read_faults={1: CatalogUnavailable("gms is down")})

    with TestClient(app) as c:
        response = c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

    assert response.status_code == 503
    assert response.status_code != 201
    assert "Retry-After" in response.headers
    # The banner must name the catalog, not blame the caller and not blame the model.
    detail = response.json()["detail"].lower()
    assert "catalog" in detail or "datahub" in detail
    # And it must point at the check that settles it, since the fix is not in Attest.
    assert "/health" in response.json()["detail"]


def test_a_failed_run_is_not_stored_as_an_audit_and_leaks_no_ledger(tmp_path):
    """No verdicts were reached, so there is no audit trail to write — and none is invented.

    The leak half is not theoretical: `forget` is only reached on the success path and on the
    crash path added in Session 24. A catalog outage produces these in batches, and each one
    would pin that run's snapshots and checkpoint rows for the life of the process.
    """
    service, _ = build(tmp_path, *SUPPORTED, read_faults={1: CatalogUnavailable("gms is down")})

    with TestClient(app) as c:
        c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

    assert service.store.find_claims() == ()
    assert service.pipeline._ledgers == {}


def test_the_retrieval_routes_503_rather_than_answering_that_the_catalog_holds_nothing(
    tmp_path,
):
    """`claims: []` from an unreachable catalog is absence rendered as an answer.

    These routes read DataHub and nothing else, which is the whole point of them. An empty
    listing would tell the next agent that nothing is known about a dataset when the truth is
    that Attest could not ask — the same collapse `state: unknown` exists to prevent on the
    claims themselves.
    """
    service, catalog = build(tmp_path, *SUPPORTED)
    catalog.fail = True

    with TestClient(app) as c:
        response = c.get("/claims", params={"target_urn": SF})

    assert response.status_code == 503
    assert "Retry-After" in response.headers
    assert service is not None


# --- the cache must not turn a two-second outage into a fact about a URN -----


def test_an_unreachable_catalog_is_NOT_memoized_as_a_miss():
    """A miss is cached because it is a fact about the URN. This is not one.

    Cached, a blip on the first of twenty claims about one dataset would be replayed as that
    dataset's answer for the rest of the run — an outage promoted to a property of an entity.
    """
    catalog = FakeCatalog(snapshots(), read_faults={1: CatalogUnavailable("gms is down")})
    cache = SnapshotCache(catalog)

    with pytest.raises(CatalogUnavailable):
        cache.fetch_dataset(SF)
    # Second ask goes back to the server, which has since recovered.
    assert cache.fetch_dataset(SF).urn == SF
    assert len(catalog.fetched) == 2


def test_a_missing_entity_IS_still_memoized():
    """The pre-existing guarantee, pinned: one lookup, five identical errors."""
    catalog = FakeCatalog(snapshots())
    cache = SnapshotCache(catalog)
    missing = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.nope.gone,PROD)"

    for _ in range(3):
        with pytest.raises(EntityNotFoundError):
            cache.fetch_dataset(missing)

    assert len(catalog.fetched) == 1
    assert cache.stats.lookups == 3


# --- the vacuity check -------------------------------------------------------


def test_widening_the_catch_back_to_DataHubError_reproduces_the_shipped_bug(tmp_path):
    """THE VACUITY CHECK, and it reproduces the exact screen a user reported.

    It runs in the suite rather than in a command someone has to remember — the same
    discipline as `test_breaking_a_checker_collapses_the_benchmark`. Rebinding
    `graph.EntityNotFoundError` to `DataHubError` restores the old broad `except` verbatim:
    the outage is swallowed, every claim is filed as unchecked, and the run reports itself
    COMPLETE with a green trajectory and no verdicts at all.

    If this ever passes with the narrow catch still in place, every test above is a green
    light wired to nothing.
    """
    monkeypatch_target = "EntityNotFoundError"
    original = getattr(graph_module, monkeypatch_target)
    setattr(graph_module, monkeypatch_target, DataHubError)
    try:
        build(tmp_path, *SUPPORTED, read_faults={1: CatalogUnavailable("gms is down")})
        with TestClient(app) as c:
            response = c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

        # 1. A created run, and a green banner, for an audit that read nothing.
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "complete"
        assert body["receipts"]["trajectory_ok"] is True
        # 2. Zero verdicts, and the outage relabelled as a question about the claim.
        assert body["claims"] == []
        assert len(body["errors"]) == 1
    finally:
        setattr(graph_module, monkeypatch_target, original)
