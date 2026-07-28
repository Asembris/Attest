"""The deployment smoke test: real DataHub, real uvicorn, and the built UI answer.

This is the falsifiable version of the README's "one command runs everything" claim. It
asserts, in order:

  1. DataHub GMS is reachable, failing at the wire in ~3s rather than downstream.
  2. A separately launched uvicorn process answers `/health` over a real socket.
  3. That process serves the built frontend at `/` and its referenced JavaScript asset.
  4. POST `/audit` on the sample produces verdicts and resolves every seeded URN.

`spikes/smoke_runner.py` owns the deployed process lifecycle. The test deliberately knows
only its HTTP base URL: importing the ASGI app or TestClient here would erase the boundary
this file exists to prove. `spikes/smoke_sabotage.py` is the vacuity check.
"""

from __future__ import annotations

import os
import re

import httpx
import pytest

from attest.config import settings

pytestmark = pytest.mark.live

_HR = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD)"
_USERS = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD)"
_RAW = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD)"

_DEMO_SAMPLE = f"""Findings from the data platform review:

The dataset {_HR} contains no PII.

The dataset {_USERS} is owned by dana.wu.

The dataset {_RAW} was updated within the last 5000 hours."""


def _sample() -> str:
    return os.environ.get("ATTEST_SMOKE_SAMPLE", _DEMO_SAMPLE)


def _gms_reachable() -> bool:
    """Is GMS answering /config? A 3s cap keeps a down stack diagnostic, not a hang."""
    try:
        httpx.get(f"{settings.datahub_gms_url}/config", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def _javascript_asset(index_html: str) -> str:
    override = os.environ.get("ATTEST_SMOKE_ASSET_PATH")
    if override:
        return override
    match = re.search(r'<script[^>]+src="([^"]+\.js)"', index_html)
    assert match, "the built frontend index references no JavaScript asset"
    return match.group(1)


def test_the_stack_is_up_and_the_deployed_demo_path_answers():
    assert _gms_reachable(), (
        f"DataHub GMS is not reachable at {settings.datahub_gms_url}/config. "
        "Bring the stack up first: `just up`."
    )

    base = os.environ.get("ATTEST_SMOKE_BASE_URL")
    assert base, (
        "the real deployed Attest server was not provided; run through `just smoke`, "
        "which launches uvicorn rather than TestClient"
    )

    with httpx.Client(base_url=base, timeout=120, follow_redirects=True) as client:
        health_response = client.get("/health")
        assert health_response.status_code == 200, (
            f"the deployed Attest server is not reachable: {health_response.text}"
        )
        health = health_response.json()
        assert health["datahub"] == "up", f"Attest cannot reach the catalog: {health['datahub']}"

        index = client.get("/")
        assert index.status_code == 200, (
            f"the built frontend is not reachable at /: HTTP {index.status_code}"
        )
        assert "text/html" in index.headers.get("content-type", ""), index.headers

        asset_path = _javascript_asset(index.text)
        asset = client.get(asset_path)
        assert asset.status_code == 200, (
            f"the built frontend asset is not reachable at {asset_path}: HTTP {asset.status_code}"
        )
        assert "javascript" in asset.headers.get("content-type", ""), asset.headers
        assert asset.content, f"the built frontend asset at {asset_path} is empty"

        response = client.post(
            "/audit", json={"agent_output": _sample(), "source_agent": "smoke"}
        )
        assert response.status_code == 201, response.text
        record = response.json()
        assert record["claims"], (
            "the demo audit produced no verdicts - the demo path does not answer "
            f"(errors: {record.get('errors')})"
        )
        assert not record["errors"], (
            f"the demo audit could not resolve every URN it named: {record['errors']}"
        )
