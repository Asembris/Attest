"""The anti-drift pin. The captured fixtures are exactly as honest as this test.

The offline tier reads serialized `DatasetSnapshot`s (`tests/fixtures/snapshots/`) instead
of the live catalog. That trade — run anywhere, never skip — is only sound if the fixtures
still match what real GMS returns. This test is what makes it sound: it re-fetches every
seeded dataset from the live server and asserts, per URN, that the captured snapshot equals
the freshly normalized one.

It runs in the LIVE tier (`just live` / `just preflight`), so it is the cadence rule that
keeps the fixtures honest: change the seed, or take a GMS version bump that reshapes the
normalized model, and this fails loudly the next time the live tier runs — with the exact
URN that drifted and the instruction to recapture. Skipping it (DataHub down) is announced,
not silent.

The dataset list is `ground_truth.json`'s `datasets` — the same manifest `capture` reads —
so the pin covers exactly what was captured, with nothing to keep in sync by hand. Fixtures
without this pin are the drift trap Session 8 set out to avoid; it ships in the same session.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from _snapshots import load_snapshot
from attest.datahub import DataHubClient

pytestmark = pytest.mark.live

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "seed" / "ground_truth.json"
SEEDED_URNS = [d["urn"] for d in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["datasets"]]


@pytest.mark.parametrize("urn", SEEDED_URNS, ids=lambda u: u.split(",")[1])
def test_the_captured_fixture_still_equals_what_gms_returns(
    client: DataHubClient, urn: str
) -> None:
    live = client.fetch_dataset(urn)
    captured = load_snapshot(urn)
    assert captured == live, (
        f"the captured snapshot for {urn} no longer matches the live catalog. "
        "The seed changed or GMS drifted the normalized shape. Recapture: `just capture`."
    )
