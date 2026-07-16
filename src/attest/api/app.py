"""Attest as a service.

    POST /audit               submit an agent's output; get a report back
    GET  /audit/{run_id}      retrieve a stored report
    POST /audit/{run_id}/approve   the human checkpoint: settle the proposed corrections
    GET  /health              liveness

Four endpoints, and the interesting one is the third.

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
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver

from attest import __version__
from attest.api.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    AuditRequest,
    HealthResponse,
    WriteBackView,
)
from attest.api.service import (
    AuditService,
    NotResumable,
    RunNotFound,
    TargetNotCovered,
    TrajectoryViolation,
)
from attest.config import settings
from attest.datahub import DataHubClient
from attest.graph import Pipeline
from attest.record import AuditRecord
from attest.store import AuditStore

log = logging.getLogger(__name__)


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


app = FastAPI(
    title="Attest",
    version=__version__,
    summary="A groundedness auditor: verifies an agent's claims about data against "
    "DataHub's catalog as ground truth.",
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

    An accepted verdict is written back to the catalog as a structured property
    (writeback.py). A rejected one writes nothing. The write-backs are reported separately
    from the audit, because a decision is a human act that stands whatever the network then
    did with it — an approval whose catalog write failed says so.
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

    return ApprovalResponse(
        audit=settled,
        writebacks=tuple(
            WriteBackView(target_urn=w.target_urn, ok=w.ok, detail=w.detail)
            for w in writebacks
        ),
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
