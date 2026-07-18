"""The deployment smoke test: the stack is up, and the demo path answers.

This is the falsifiable version of the README's "one command runs everything" claim. It
asserts, in order:

  1. DataHub GMS is reachable — and it fails HERE, at the wire, in ~3s, not as a downstream
     90s timeout waiting for something that was never coming. "A container failed to come up"
     must read as exactly that, immediately, not as a mystery hang (the Session 19 lesson,
     applied to bring-up).
  2. Attest can see the catalog (`/health` reports datahub up).
  3. The demo audit answers: POST /audit on the sample produces real verdicts and resolves
     every seeded URN it names (no ClaimErrors).

It is LIVE tier — it needs a real DataHub and a real model — so `just live`/`just preflight`
pick it up and `just check` never does. `just smoke` brings the stack up first (via the `up`
recipe) and then runs this, so the whole "one command up + the demo answers" claim is one
command.

spikes/smoke_sabotage.py is the vacuity check: it points this at a DOWN GMS (must go red at
step 1, fast) and feeds it an UNSEEDED URN (must go red at step 3), and fails if either does
not. An assertion that only ever passes is a green light wired to nothing.
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from attest.api.app import app
from attest.config import settings

pytestmark = pytest.mark.live

# The demo audit, mirroring the frontend sample (frontend/src/data/mockData.ts): three real
# seeded URNs across three claim types. The freshness window is fixed here (the frontend
# varies it per page load for a fresh artifact URN; a test wants determinism) and sits in the
# Supported band so this never drags in the correction loop — the smoke test is about whether
# the path ANSWERS, not about any one verdict. Overridable by the sabotage via env.
_HR = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD)"
_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD)"
_RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD)"

_DEMO_SAMPLE = f"""Findings from the data platform review:

The dataset {_HR} contains no PII.

The dataset {_USERS} is owned by dana.wu.

The dataset {_RAW} was updated within the last 5000 hours."""


def _sample() -> str:
    # The sabotage swaps this for prose naming an UNSEEDED URN, so every claim ClaimErrors and
    # step 3 goes red — the "demo path 500s / does not answer" case, without touching product
    # code.
    return os.environ.get("ATTEST_SMOKE_SAMPLE", _DEMO_SAMPLE)


def _gms_reachable() -> bool:
    """Is GMS answering /config? A 3s cap so a down stack fails fast, not on a long timeout."""
    try:
        httpx.get(f"{settings.datahub_gms_url}/config", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def test_the_stack_is_up_and_the_demo_path_answers():
    # 1. The stack. Fail AT THE WIRE with the URL, not downstream.
    assert _gms_reachable(), (
        f"DataHub GMS is not reachable at {settings.datahub_gms_url}/config. "
        "Bring the stack up first: `just up`."
    )

    with TestClient(app) as client:
        # 2. Attest sees the catalog.
        health = client.get("/health").json()
        assert health["datahub"] == "up", f"Attest cannot reach the catalog: {health['datahub']}"

        # 3. The demo audit answers.
        response = client.post(
            "/audit", json={"agent_output": _sample(), "source_agent": "smoke"}
        )
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["claims"], (
            "the demo audit produced no verdicts — the demo path does not answer "
            f"(errors: {record.get('errors')})"
        )
        assert not record["errors"], (
            f"the demo audit could not resolve every URN it named: {record['errors']}"
        )
