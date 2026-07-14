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
loop stays free. (The service serializes audits internally anyway — see service.py for why
that is about token accounting, not about throughput.)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status

from attest import __version__
from attest.api.schemas import (
    ApprovalRequest,
    ApprovalResponse,
    AuditRequest,
    HealthResponse,
    WriteBackView,
)
from attest.api.service import AuditService, NotResumable, RunNotFound
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
    """
    client = DataHubClient()
    return AuditService(
        pipeline=Pipeline(client=client),
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
    """
    return service.audit(request.agent_output, source_agent=request.source_agent)


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
    except NotResumable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return ApprovalResponse(
        audit=settled,
        writebacks=tuple(
            WriteBackView(target_urn=w.target_urn, ok=w.ok, detail=w.detail)
            for w in writebacks
        ),
    )
