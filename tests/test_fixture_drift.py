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

# The sentinel emitted when there is no seed artifact to enumerate. See pytest_generate_tests.
_NO_SEED = "__no_seed_artifact__"


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize the pin over the seeded URNs -- reading the seed artifact at COLLECTION,
    but only when it EXISTS.

    `ground_truth.json` is written by `just seed`, which needs a live DataHub. CI has neither.
    This read used to live at module scope, so importing this module read the file -- and
    IMPORT happens during collection, for every session, BEFORE the `-m 'not live'` filter can
    deselect a live-marked test. With no seed on a bare runner the read raised FileNotFoundError
    and took down collection of the whole OFFLINE tier: a live-only test breaking the tier that
    is supposed to run with DataHub stopped (Session 8's honesty guarantee).

    Deferring the read into this hook keeps the module importable with no catalog. When the
    file is absent we emit ONE sentinel param, which `-m 'not live'` deselects without ever
    running -- so the offline tier collects clean. When the LIVE tier is run against no seed,
    the sentinel makes the test FAIL loudly (below) rather than skip: a pin with nothing to pin
    against is a failure, not a pass. When the file IS present the parametrization is byte-for-
    byte what it was -- same URNs, same ids -- so the live tier is unchanged.
    """
    if "urn" not in metafunc.fixturenames:
        return
    if GROUND_TRUTH.exists():
        urns = [
            d["urn"]
            for d in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["datasets"]
        ]
        metafunc.parametrize("urn", urns, ids=lambda u: u.split(",")[1])
    else:
        metafunc.parametrize("urn", [_NO_SEED], ids=["no-seed-artifact"])


def test_the_captured_fixture_still_equals_what_gms_returns(
    client: DataHubClient, urn: str
) -> None:
    if urn == _NO_SEED:
        pytest.fail(
            f"{GROUND_TRUTH} is missing -- run `just seed` before the live pin. "
            "(Offline runs never reach here: this live test is deselected by `-m 'not live'`.)"
        )
    live = client.fetch_dataset(urn)
    captured = load_snapshot(urn)
    assert captured == live, (
        f"the captured snapshot for {urn} no longer matches the live catalog. "
        "The seed changed or GMS drifted the normalized shape. Recapture: `just capture`."
    )
