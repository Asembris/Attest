"""When the model PROVIDER fails, not when the model answers badly.

Two failures wear the same coat and must not be handled the same way. `MalformedOutput` is
the model producing unusable output — an honest "no claims" is a reasonable thing to make of
it. A 500 from OpenAI is the provider never producing output at all, and making "no claims"
of THAT would be Attest reporting silence as an answer, which is the one thing this product
exists to refuse.

The bug these were written for shipped and reached a user: every OpenAI SDK exception
descends from `openai.OpenAIError` -> `Exception`, while every degradation path in the
semantic layer catches `LLMError` (a `RuntimeError`). `issubclass(InternalServerError,
RuntimeError)` is False, so the entire transport failure class walked past all three of them
and crashed the run — sharpest at EXPLAIN, where the verdict was already deterministically
decided and the safe template was one line away.

**It survived a green suite because the fake could not fail that way.** The scripted chat
client is a Python object with no HTTP stack; no test written against it could raise an
`APIStatusError` until `FakeChat.faults` existed. That is Session 5's rule
(structurally-invisible-to-a-fake) landing a second time, in the same module, for the same
reason — so these tests are as much about fakes.py gaining fault injection as about llm.py
gaining a taxonomy.

Offline and free: no network, no key, no model.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from attest import llm as llm_module
from attest.api.app import app, get_service
from attest.api.service import AuditService
from attest.config import Step
from attest.graph import Pipeline
from attest.llm import (
    LLM,
    MalformedOutput,
    ProviderRefused,
    ProviderUnavailable,
)
from attest.report import RunStatus
from attest.store import AuditStore
from fakes import (
    FakeChat,
    FakeDataHub,
    claim_reply,
    dataset,
    explanation_reply,
    openai_connection_error,
    openai_status_error,
    openai_timeout,
    reply,
)

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
CAROL = "urn:li:corpuser:carol.davis"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

SAYS = f"The dataset {SF} is owned by {CAROL}."

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def call(chat: FakeChat) -> dict:
    """One structured call through the REAL llm.LLM, whatever the fake does to it."""
    return LLM(client=chat).json(
        step=Step.CLAIM_EXTRACTION,
        system="s",
        user="u",
        schema=SCHEMA,
        schema_name="probe",
    )


# A one-claim run the catalog SUPPORTS: decompose is call 1, explain is call 2, and there is
# no correction loop to add a third. That is what makes "put the outage on call 2" mean
# "the outage is in the explain step" without counting anything by hand.
SUPPORTED = (
    claim_reply(
        [
            {
                "claim_type": "ownership",
                "target_urn": SF,
                "raw_text": SAYS,
                "owner_urn": CAROL,
            }
        ]
    ),
    explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
)


def build(tmp_path, *replies: str, faults: dict[int, Exception] | None = None):
    """A service wired to a fake model that can fail like the real one, and a real database."""
    catalog = FakeDataHub(
        {
            SF: dataset(
                SF,
                last_modified=NOW - timedelta(hours=6),
                owners=(CAROL,),
                tags=(),
                terms=(),
                custom_properties={},
            )
        }
    )
    chat = FakeChat(replies=list(replies), faults=dict(faults or {}))
    service = AuditService(
        pipeline=Pipeline(llm=LLM(client=chat), client=catalog, now=NOW),
        store=AuditStore(tmp_path / "attest.db"),
        client=catalog,
        write_back=False,
    )
    app.dependency_overrides[get_service] = lambda: service
    return service, chat


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# --- the classification ------------------------------------------------------


def test_a_transient_provider_500_becomes_ProviderUnavailable_and_attest_never_retries_it():
    """The SDK already retried it. A second loop here would multiply, not help.

    Measured: the SDK's default is max_retries=2 and `_should_retry` covers every >=500, so
    the 500 that reaches this code has ALREADY cost three requests and ~1.5s of backoff.
    Attest retrying it again would make six requests and ~20s of backoff for one dead
    provider — a hang dressed as resilience. So the count is the assertion.
    """
    chat = FakeChat(replies=[reply({"ok": True})], faults={1: openai_status_error(500)})

    with pytest.raises(ProviderUnavailable) as caught:
        call(chat)

    assert len(chat.calls) == 1, "Attest retried a call the SDK had already retried"
    # The provider's own words survive: a diagnosis needs the status, not a paraphrase.
    assert "500" in str(caught.value)
    # It is still an LLMError, which is what lets explain and revise degrade on it.
    assert isinstance(caught.value, llm_module.LLMError)
    # ...but NOT the thing decompose degrades on. That distinction is the whole fix.
    assert not isinstance(caught.value, MalformedOutput)


def test_an_authentication_error_becomes_ProviderRefused_fast_and_is_never_retried():
    """A bad key is not a hiccup. Retrying it burns time to reach the same answer."""
    chat = FakeChat(replies=[reply({"ok": True})], faults={1: openai_status_error(401)})

    with pytest.raises(ProviderRefused) as caught:
        call(chat)

    assert len(chat.calls) == 1
    assert "401" in str(caught.value)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        (openai_status_error(500), ProviderUnavailable),
        (openai_status_error(503), ProviderUnavailable),
        (openai_status_error(429), ProviderUnavailable),  # rate limited: try later
        (openai_status_error(408), ProviderUnavailable),
        (openai_connection_error(), ProviderUnavailable),
        (openai_timeout(), ProviderUnavailable),  # a subclass of APIConnectionError
        (openai_status_error(400), ProviderRefused),  # malformed request: ours to fix
        (openai_status_error(401), ProviderRefused),
        (openai_status_error(403), ProviderRefused),
        (openai_status_error(404), ProviderRefused),  # a model name that does not exist
    ],
)
def test_the_transient_class_is_exactly_what_the_sdk_would_have_retried(fault, expected):
    """The line is drawn where the SDK draws it, so the two cannot drift apart.

    `ProviderUnavailable` means "already retried by the transport and still failed"; if this
    table disagreed with `_should_retry`, Attest would either promise a retry that never
    happened or refuse one that silently did.
    """
    with pytest.raises(expected):
        call(FakeChat(replies=[reply({"ok": True})], faults={1: fault}))


def test_malformed_output_is_still_retried_once_and_is_not_a_provider_failure():
    """The pre-existing behaviour, pinned: the new class must not swallow the old one.

    A provider failure and a model failure now share a base, so the risk introduced by this
    fix is that the schema-repair retry stops happening. It still does: two attempts, the
    error handed back, then MalformedOutput.
    """
    chat = FakeChat(replies=["not json at all", reply({"ok": True})])

    assert call(chat) == {"ok": True}
    assert len(chat.calls) == 2, "the malformed-output repair retry was lost"

    always_bad = FakeChat(replies=["still not json"])
    with pytest.raises(MalformedOutput):
        call(always_bad)
    assert len(always_bad.calls) == 2


# --- the win: a prose hiccup costs prose, not the run ------------------------


def test_a_provider_outage_during_explain_degrades_to_the_template_and_keeps_its_verdicts():
    """THE FIX'S REASON FOR EXISTING. The verdict is decided by code before explain runs.

    A 500 while phrasing an already-final verdict used to throw the whole run away —
    verdicts, evidence, receipts and all — when the deterministic template was one line
    away and is, by construction, both faithful and polarity-safe. The run must complete,
    and its explanation must be the TRUE template rather than anything the model half-said.
    """
    catalog = FakeDataHub(
        {
            SF: dataset(
                SF,
                last_modified=NOW - timedelta(hours=6),
                owners=(CAROL,),
                tags=(),
                terms=(),
                custom_properties={},
            )
        }
    )
    chat = FakeChat(replies=list(SUPPORTED), faults={2: openai_status_error(500)})
    report = Pipeline(llm=LLM(client=chat), client=catalog, now=NOW).run(SAYS)

    assert len(chat.calls) == 2, "the outage did not land on the explain step"
    # The run survived, and it survived with its verdict.
    assert report.status is not RunStatus.FLAGGED
    assert len(report.audits) == 1
    audit = report.audits[0]
    assert audit.verdict.value == "Supported"
    # ...phrased by code, not by a model that never answered.
    assert audit.explanation.source == "template"
    assert audit.explanation.text
    # The failure is recorded rather than smoothed over: a reader can see WHY it is a
    # template. A silent downgrade would be the same defect one level down.
    assert any("500" in r for r in audit.explanation.rejected)
    # And the run still certifies honestly — a degraded explanation is not a violation.
    assert report.trajectory.ok


# --- the anti-silence rule: decompose does NOT degrade -----------------------


def test_a_provider_outage_during_decompose_is_a_503_and_never_a_zero_claim_success(tmp_path):
    """THE ANTI-SILENCE RULE. Three claims went in; "no claims" must not come out.

    decompose's existing degradation — an empty Decomposition carrying `extraction-failed`
    in `dropped` — is right for a model that produced garbage and WRONG for a provider that
    produced nothing. Degrading here would return `status=complete`, `trajectory_ok=true`,
    and a UI showing an audit that found nothing: Attest rendering silence as an answer, the
    cardinal sin of its own thesis, in its own output.
    """
    _, chat = build(tmp_path, *SUPPORTED, faults={1: openai_status_error(500)})

    with TestClient(app) as c:
        response = c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

    assert response.status_code == 503
    assert len(chat.calls) == 1
    # Retryable, and it says so in the way an HTTP client can act on.
    assert "Retry-After" in response.headers
    # The banner must name the provider, not blame the caller and not blame Attest.
    detail = response.json()["detail"]
    assert "provider" in detail.lower()
    assert "500" in detail
    # It is emphatically NOT a 201 with an empty verdict list.
    assert response.status_code != 201


def test_a_refused_provider_request_during_decompose_is_a_502_not_a_retryable_503(tmp_path):
    """A bad key must not be dressed up as "try again in a moment"."""
    build(tmp_path, *SUPPORTED, faults={1: openai_status_error(401)})

    with TestClient(app) as c:
        response = c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

    assert response.status_code == 502
    assert "Retry-After" not in response.headers
    assert "401" in response.json()["detail"]


def test_a_failed_run_is_not_stored_as_an_audit_and_leaks_no_ledger(tmp_path):
    """Nothing to inherit, nothing to resume, and nothing left behind.

    The run produced no verdicts, so there is no record to write — inventing a 0-claim
    AuditRecord for it would be the same fabrication the store refuses everywhere else. What
    it must NOT do is leak: a crashed run used to keep its ledger and its checkpoint rows for
    the life of the process, because `forget` was only ever reached on the success path.
    """
    service, _ = build(tmp_path, *SUPPORTED, faults={1: openai_status_error(500)})

    with TestClient(app) as c:
        c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})

    assert service.store.find_claims() == ()
    assert service.pipeline._ledgers == {}


# --- the client is built with a bounded read ---------------------------------


def test_the_provider_client_is_built_with_a_read_timeout_it_can_actually_hit(monkeypatch):
    """A wedged socket must fail in under a minute, not in ten.

    The SDK's default read timeout is 600 SECONDS, so one dead socket stalls an audit for
    ten minutes and looks exactly like a slow audit — the same failure CLAUDE.md recorded
    for the calibration labeler, on a path that never got the same fix. This pins the
    constant's ROLE, not its number: change the value and this still passes, remove the
    timeout and it goes red.
    """
    from openai._constants import DEFAULT_TIMEOUT

    monkeypatch.setattr(llm_module.settings, "openai_api_key", "sk-test-not-a-real-key")
    client = LLM()._build_openai()

    assert client.timeout.read is not None
    assert client.timeout.read < DEFAULT_TIMEOUT.read
    # A connect failure is knowable much sooner than a slow generation, so it stays shorter.
    assert client.timeout.connect <= client.timeout.read
    # Bounded end to end: attempts x read + backoff has to stay demo-survivable.
    assert (llm_module.settings and client.timeout.read * 3) < 300


# --- the vacuity check -------------------------------------------------------


def test_reverting_the_translation_loses_the_verdicts_and_restores_the_raw_500(tmp_path):
    """THE VACUITY CHECK. Prove the translation is what is holding the two fixes up.

    It runs in the suite rather than in a command someone has to remember — the same
    discipline as `test_breaking_a_checker_collapses_the_benchmark`. `_as_provider_error`
    returning None IS the pre-fix code: the SDK's exception re-raises untranslated, walks
    past every `except LLMError`, and takes the run with it.

    If this ever passes with the translation still in place, the two tests above are green
    lights wired to nothing.
    """
    monkeypatch_target = "_as_provider_error"
    original = getattr(llm_module, monkeypatch_target)
    setattr(llm_module, monkeypatch_target, lambda exc: None)
    try:
        # 1. EXPLAIN: the run no longer completes. Its verdicts die with it.
        catalog = FakeDataHub(
            {
                SF: dataset(
                    SF,
                    last_modified=NOW - timedelta(hours=6),
                    owners=(CAROL,),
                    tags=(),
                    terms=(),
                    custom_properties={},
                )
            }
        )
        chat = FakeChat(replies=list(SUPPORTED), faults={2: openai_status_error(500)})
        with pytest.raises(Exception) as caught:
            Pipeline(llm=LLM(client=chat), client=catalog, now=NOW).run(SAYS)
        assert not isinstance(caught.value, llm_module.LLMError), (
            "untranslated, this must NOT be an LLMError — that is the whole bug"
        )

        # 2. DECOMPOSE: the clean 503 collapses back into a bare 500.
        build(tmp_path, *SUPPORTED, faults={1: openai_status_error(500)})
        with TestClient(app, raise_server_exceptions=False) as c:
            response = c.post("/audit", json={"agent_output": SAYS, "source_agent": "t"})
        assert response.status_code == 500
        assert "Retry-After" not in response.headers
    finally:
        setattr(llm_module, monkeypatch_target, original)
