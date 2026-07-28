"""Attest as a service.

    POST /audit                      submit an agent's output; get a report back
    GET  /audit/{run_id}             retrieve a stored report
    POST /audit/{run_id}/approve     the human checkpoint: settle the proposals
    POST /audit/{run_id}/writeback   repair a partial catalog write. Approves nothing
    GET  /claims                     claim artifacts, FROM DATAHUB. What a reader inherits
    GET  /claims/{claim_urn}         one claim, with its whole verdict history
    GET  /health                     liveness

**The two halves of the thesis are the approve endpoint and the claims endpoints.**
`/audit/{id}/approve` is where a human publishes a verdict to the catalog; `/claims` is
where anyone reads it back. Challenge 1's claim is "writes results back so the next person
or agent inherits the knowledge", and inheriting means retrieving — an artifact nobody can
query is only half of it.

**`/claims` answers from DATAHUB, not from Attest's store.** That is deliberate and it is
load-bearing: an endpoint that answered from SQLite would prove nothing about what a second
agent inherits, because a second agent cannot open Attest's database. The store is consulted
for exactly one thing — explaining an absent verdict, which the catalog cannot do — and it
can never add a claim, remove one, or change a verdict. See retrieval.py.

The interesting one is still the approve.

**The checkpoint does not soften at the API layer.** `POST /audit` returns a report whose
proposed corrections are PENDING, and they stay PENDING until a person names them in an
approve call. There is no `?auto_approve=true`, no "approve all", and no default that
accepts. An unattended caller — a script, a cron job, another agent — can run audits all
day and change nothing in the catalog. That is the Session 3 accountability decision, and
an HTTP surface is exactly the place it would have quietly been traded away for
convenience.

**The endpoints are `def`, not `async def`, on purpose.** An audit is seconds of blocking
work: network calls to DataHub, network calls to OpenAI, and a synchronous LangGraph
invoke. Declared `async`, it would run ON the event loop and stall every other request in
the process, including `/health`. Declared `def`, Starlette runs it in a threadpool and the
loop stays free — and since Session 5 removed the service's lock, two audits submitted at
once really do run at once (service.py).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from attest import __version__
from attest.api.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    AuditRequest,
    ClaimsResponse,
    ClaimView,
    HealthResponse,
    RetrievalView,
    VerdictEventView,
    WriteBackResponse,
    WriteBackView,
)
from attest.api.service import (
    AuditService,
    ClaimNotFound,
    NotResumable,
    RunNotFound,
    TargetNotCovered,
    TrajectoryViolation,
)
from attest.config import settings
from attest.datahub import DataHubClient
from attest.graph import Pipeline
from attest.llm import LLMError, ProviderRefused, ProviderUnavailable
from attest.record import AuditRecord, EvidenceView
from attest.retrieval import ClaimQuery, RetrievedClaim
from attest.store import AuditStore

log = logging.getLogger(__name__)

# What `Retry-After` says on a transient provider failure. Short, because the transport has
# already spent its own bounded backoff getting here (llm.py) and a caller who waits a minute
# on a hiccup that lasted two seconds has been told something untrue about the outage.
RETRY_AFTER_SECONDS = 10


@lru_cache(maxsize=1)
def get_service() -> AuditService:
    """The service, built once for the process.

    A FastAPI dependency, so a test overrides it with `app.dependency_overrides` and gets a
    service wired to a fake model and a temp database. Nothing in the routes reaches for a
    global.

    The checkpointer is SQLite and its file is NOT the audit history's. LangGraph owns those
    tables and their shape moves with its release cadence; the audit history is Attest's own
    schema and the evidence trail is in it. Sharing one file would put a dependency's
    migrations in the same blast radius as the thing this product exists to keep.
    """
    client = DataHubClient()
    # check_same_thread=False because the endpoints are `def`, so Starlette runs them in a
    # threadpool and the saver is legitimately touched from several threads. SqliteSaver
    # holds its own lock; sqlite3 serializes writes underneath it.
    checkpoints = sqlite3.connect(settings.checkpoint_path, check_same_thread=False)
    return AuditService(
        pipeline=Pipeline(client=client, saver=SqliteSaver(checkpoints)),
        store=AuditStore(settings.store_path),
        client=client,
    )


# The service, as a type. FastAPI resolves it per request; a test swaps it wholesale
# with app.dependency_overrides.
Service = Annotated[AuditService, Depends(get_service)]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Replay any settlement a previous process death left unfinished, ONCE, at startup.

    This is the recovery trigger for the write-ahead intent (Session 22, service.py). A
    process that dies mid-settlement — after the human's decision was consumed by the graph,
    during or after the three-step catalog write, before the store commit — leaves a durable
    intent flagged unsettled. A fresh process picks it up here and replays it idempotently,
    so `just serve`/`just demo` recover on their own rather than needing someone to call the
    repair endpoint.

    **Override-aware on purpose.** The offline and API test suites swap `get_service` for a
    fake wired to a temp store; reading the override here means those runs recover over their
    own empty intents table (a no-op) instead of reaching for the real catalog. Defensive:
    a recovery failure is logged and the service still starts — a stuck intent is retried on
    the next boot, and refusing to serve because one old settlement will not replay would be
    a worse outage than the one it is recovering from.

    Single-settler: recovery assumes one process at a time (service.py).
    """
    factory = app.dependency_overrides.get(get_service, get_service)
    try:
        recovered = factory().recover_settlements()
        if recovered:
            log.info("startup: recovered %d unfinished settlement(s)", recovered)
    except Exception:  # noqa: BLE001 - startup must not fail on a recovery hiccup
        log.exception("startup settlement recovery failed; the service still starts")
    yield


app = FastAPI(
    title="Attest",
    version=__version__,
    summary="A groundedness auditor: verifies an agent's claims about data against "
    "DataHub's catalog as ground truth.",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health(service: Service) -> HealthResponse:
    """Is Attest up, and can it see the catalog? Two questions, two answers."""
    state = service.health()
    return HealthResponse(
        status=state["status"],
        version=__version__,
        model=state["model"],
        datahub=state["datahub"],
        datahub_ui_url=state["datahub_ui_url"],
    )


@app.post("/audit", response_model=AuditRecord, status_code=status.HTTP_201_CREATED)
def submit_audit(
    request: AuditRequest, service: Service
) -> AuditRecord:
    """Audit an agent's output.

    Returns the complete report: a verdict, its evidence, and an explanation for every
    claim, plus the receipts. If the run produced corrections, its status is
    `awaiting-review` and they are PENDING — proposed, never applied. Nothing has been
    written to DataHub, and nothing will be until someone approves it.

    `target_urns`, if given, is a precondition: every URN named in it must be quoted in
    `agent_output` (checked before the run) AND must have produced a claim (checked after
    it). An audit that covered less than the caller required is a 422 rather than a 201 —
    the run is still stored, and the error names it.

    **This is the only route that calls a model**, so it is the only one that can fail on the
    provider. A failure there is reported as three different things because it IS three
    different things — retryable, refused, misconfigured — and a caller can act on exactly
    one of them.
    """
    try:
        return service.audit(
            request.agent_output,
            source_agent=request.source_agent,
            target_urns=request.target_urns,
        )
    except TargetNotCovered as exc:
        # 422, the same as the request-time half of this precondition (schemas.py): the
        # request is well-formed and cannot be processed as asked. A 201 here would tell a
        # caller who said "audit these" that Attest did, when it did not.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ProviderUnavailable as exc:
        # 503 + Retry-After: the upstream is transiently down and the transport has ALREADY
        # exhausted its bounded retry (llm.py), so the next attempt belongs to whoever is
        # holding the request — with a number rather than a shrug. Nothing was audited and
        # nothing was stored; there is no run to point at, which is why this says so plainly
        # instead of handing back a run id that would resolve to an audit of nothing.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"No audit was run: {exc}. Attest's verdicts are deterministic, but extracting "
            f"claims from the agent's text needs the model, and the provider is temporarily "
            f"unavailable. Nothing was written to the catalog. Try again in a moment.",
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        ) from exc
    except ProviderRefused as exc:
        # 502, and pointedly NOT 503: the provider rejected the request itself, so waiting
        # changes nothing. Telling a caller to retry a bad key would waste their time on
        # Attest's behalf, which is the failure the Retry-After above exists to avoid.
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No audit was run: {exc}. The provider rejected the request, so retrying will "
            f"not help — check the API key and the configured model name.",
        ) from exc
    except LLMError as exc:
        # Attest's own configuration, most often a missing key. A 500 is right — it is our
        # fault, not the caller's and not the provider's — but it must CARRY the message.
        # A bare 500 renders as "Internal Server Error" in the browser (the body is
        # text/plain, so the client's JSON parse falls back to statusText), which tells the
        # person who can fix it nothing at all.
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc)) from exc


@app.get("/audit/{run_id}", response_model=AuditRecord)
def get_audit(run_id: str, service: Service) -> AuditRecord:
    """A stored audit, whole: verdicts, evidence, trajectory, and receipts."""
    try:
        return service.get(run_id)
    except RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@app.post("/audit/{run_id}/approve", response_model=ApprovalResponse)
def approve(
    run_id: str,
    request: ApprovalRequest,
    service: Service,
) -> ApprovalResponse:
    """The human checkpoint. Settle the proposed corrections; accepted ones reach DataHub.

    Only what you name is settled. A proposal you do not mention stays PENDING — including
    when `decisions` is empty, which is a person having looked and decided nothing, and is
    a legitimate outcome rather than an error.

    Leave any proposal undecided and the run stays `awaiting-review` and stays resumable:
    post again with the rest. The run becomes `complete` on the call that settles the last
    proposal, and only then are its checkpoints released.

    A published verdict is written back to the catalog as its own **claim artifact** — a
    custom DataHub Assertion plus an append-only verdict event, the authoritative,
    subject-bearing record the next agent inherits (writeback.py). The dataset-level
    structured property is written too, as a best-effort glance BADGE that points at the
    artifact; it is not the record and its failure does not fail the settlement. A withheld
    verdict writes nothing. The write-backs are reported separately from the audit, because a
    decision is a human act that stands whatever the network then did with it — an approval
    whose catalog write failed says so, and the settlement itself is crash-recoverable
    (Session 22): a process death mid-write is replayed from a durable intent on the next
    start.
    """
    try:
        settled, writebacks = service.approve(
            run_id, [d.to_decision() for d in request.decisions]
        )
    except RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TrajectoryViolation as exc:
        # A flagged run is un-approvable. 409: the run's state conflicts with the request,
        # and no evidence in it can be signed off — the same shape as a run whose pause is
        # gone, and refused for the same reason: the one path that writes to a catalog must
        # not be reachable from a report the pipeline could not vouch for.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except NotResumable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return ApprovalResponse(audit=settled, writebacks=_views(writebacks))


@app.post("/audit/{run_id}/writeback", response_model=WriteBackResponse)
def retry_writeback(run_id: str, service: Service) -> WriteBackResponse:
    """Re-run the catalog write for claims this run's reviewers ALREADY accepted.

    The recovery path for a partial write. A claim artifact takes three writes — the claim,
    its verdict, then the verdict tag — and they cannot be atomic, so a failure can leave a
    claim with no verdict, or a verdict that is correct but not yet findable by search. The
    approve call reports exactly which step failed; this repairs it.

    **This approves nothing, and cannot.** It re-executes the side effect of decisions that
    are already recorded — a claim nobody accepted is not reachable from here, and a rejected
    one writes nothing, exactly as at the checkpoint. There is still one path to approving
    something, and this is not it. It exists because re-running `approve` cannot repair a
    write: a settled claim is no longer awaiting a human, so it is skipped, and a fully
    decided run is complete and no longer resumable. Without this a failed write-back simply
    stranded.

    Safe to call repeatedly, and it needs no state: every write is idempotent. The claim's
    URN is derived from the claim's own content, so a re-run lands on the same artifact and
    never mints a second one, and the verdict is keyed by the audit run's timestamp, so
    re-reporting it collapses onto the same row rather than appending a duplicate to the
    history.
    """
    try:
        results = service.retry_writeback(run_id)
    except RunNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TrajectoryViolation as exc:
        # The same 409 as approve, for the same reason: a run that violated its own
        # architecture may not reach the catalog by any path, and a recovery endpoint is
        # exactly where that gate would quietly be re-opened.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return WriteBackResponse(run_id=run_id, writebacks=_views(results))


@app.get("/claims", response_model=ClaimsResponse)
def list_claims(
    service: Service,
    target_urn: Annotated[str, Query(
        description="Scope to one dataset. PUSHED DOWN to DataHub "
        "(`dataset.assertions`) — and when you name one, it is the ONLY predicate that "
        "can be: that entry point filters nothing, and search cannot scope to a dataset "
        "at all. This is the thesis query: what the next agent inherits about a dataset.",
    )] = "",
    verdict: Annotated[str, Query(
        description="Latest verdict: Supported | Contradicted | Insufficient-Coverage. "
        "Pushed down as a TAG when no `target_urn` is given, otherwise applied by Attest.",
    )] = "",
    claim_type: Annotated[str, Query(
        description="freshness | ownership | classification | schema. Pushed down as "
        "`customType` when no `target_urn` is given, otherwise applied by Attest.",
    )] = "",
    reviewer: Annotated[str, Query(
        description="Who signed a verdict off. ALWAYS applied by Attest: an assertion "
        "indexes only `customType` and `tags`, so there is no filter to push this into. "
        "Matches a claim ANY of whose verdicts this person signed, not just the latest.",
    )] = "",
    since: Annotated[datetime | None, Query(
        description="Claims with a verdict at or after this time. ALWAYS applied by "
        "Attest, for the same reason as `reviewer`.",
    )] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ClaimsResponse:
    """Claim artifacts, READ FROM DATAHUB. What the next agent inherits.

    **This answers from the catalog, never from Attest's store**, and that is the whole
    point rather than an implementation detail: an API that answered from SQLite would
    prove nothing about what a second agent can retrieve. The store is consulted for
    exactly one thing — explaining why a verdict is absent — and it cannot add, remove, or
    change one.

    **Read `retrieval` in the response.** It says which predicates DataHub's index applied
    and which Attest applied over the response. The honest line is *DataHub does the
    scoping; Attest does most of the filtering*, and the response says so on every call
    rather than letting a reader assume the catalog answered the whole question.

    Every claim carries a `state`, and it is not decoration: an absent verdict means three
    different things (`pending-lag`, `incomplete`, `unknown`) with three different
    responses, and only one of them is a bug.
    """
    page = service.claims(
        ClaimQuery(
            target_urn=target_urn,
            verdict=verdict,
            claim_type=claim_type,
            reviewer=reviewer,
            since=since,
        ),
        limit=limit,
    )
    return ClaimsResponse(
        claims=tuple(_claim_view(c) for c in page.claims),
        retrieval=RetrievalView(
            entry_point=page.retrieval.entry_point,
            pushed_down=page.retrieval.pushed_down,
            filtered_locally=page.retrieval.filtered_locally,
            considered=page.retrieval.considered,
            total=page.retrieval.total,
            note=page.retrieval.note,
        ),
    )


@app.get("/claims/{claim_urn:path}", response_model=ClaimView)
def get_claim(claim_urn: str, service: Service) -> ClaimView:
    """One claim artifact and its whole verdict history, from DataHub.

    The history is append-only and it is real: `assertionRunEvent` is a timeseries aspect,
    so a claim that was Supported in March and Contradicted in July carries both, each with
    the run that reached it and the human who signed it off. That is the question CLAUDE.md
    named as unanswerable from a last-write-wins field — *"was this ever contradicted before
    someone fixed the tag"* — answered from the catalog alone.

    `{claim_urn:path}` because an assertion URN is not path-safe: it contains colons, and a
    dataset URN inside it contains commas and parentheses. `:path` takes the rest of the
    path verbatim rather than making every caller double-encode it.
    """
    try:
        return _claim_view(service.claim(claim_urn))
    except ClaimNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _claim_view(claim: RetrievedClaim) -> ClaimView:
    """One retrieved claim, as the wire type.

    `failed_step` and `audit_run` come off the STORE's record (`wrote`), not off the
    artifact: DataHub cannot know which step of Attest's write failed, and pretending the
    catalog told us would misattribute the one field that makes `incomplete` actionable.
    """
    a = claim.artifact
    return ClaimView(
        claim_urn=a.claim_urn,
        target_urn=a.target_urn,
        claim_type=a.claim_type,
        grain=a.grain,
        description=a.description,
        asserted=a.logic.get("asserted") or {},
        verdict=a.verdict,
        state=claim.state.value,
        failed_step=claim.wrote.failed_step if claim.wrote else None,
        audit_run=claim.wrote.run_id if claim.wrote else "",
        tags=a.tags,
        # Catalog-derived (the artifact's tag vs its latest verdict), so it is set even for a
        # store=None reader. See RetrievedClaim.stale_tag.
        stale_tag=claim.stale_tag,
        history=tuple(
            VerdictEventView(
                at=datetime.fromtimestamp(e.at / 1000, tz=UTC),
                verdict=e.verdict,
                audit_run=e.audit_run,
                reviewer=e.reviewer,
                decision=e.decision,
                # Structured and absence-preserving: value=None survives as null.
                evidence=tuple(
                    EvidenceView(field=it.field, value=it.value, note=it.note)
                    for it in e.evidence
                ),
                snapshot_id=e.snapshot_id,
                native_type=e.native_type,
            )
            for e in a.history
        ),
        # The catalog's own verdict-event total, so a history truncated at the 50-event read
        # cap is named rather than silently short. See ClaimArtifact.history_truncated.
        history_total=a.history_total,
        history_truncated=a.history_truncated,
    )


def _views(results) -> tuple[WriteBackView, ...]:
    return tuple(
        WriteBackView(
            claim_index=w.claim_index,
            target_urn=w.target_urn,
            ok=w.ok,
            detail=w.detail,
            claim_urn=w.claim_urn,
            failed_step=w.failed_step,
        )
        for w in results
    )


# The frontend, static-mounted at the root so the whole demo is one origin and one process
# on :8003. This mount is registered LAST and at "/", so it is the fallback: the explicit
# API routes above (and FastAPI's own /docs, /openapi.json) are matched first, and only paths
# no route claims fall through to the static files. `html=True` serves index.html for the
# app's own client-side routes.
#
# It is mounted ONLY if the build exists. A bare checkout or a test import has no
# frontend/dist, and the API must come up regardless — the UI is a client of this service,
# not a dependency of it. Build it with `just ui`.
_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="frontend")
else:  # pragma: no cover - depends on whether the UI has been built
    log.info("frontend not built (%s absent); serving API only", _FRONTEND_DIST)
