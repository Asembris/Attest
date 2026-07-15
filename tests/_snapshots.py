"""Serialized catalog snapshots — the boundary that makes the offline tier honest.

A fixture here is a `DatasetSnapshot` (Attest's own frozen read model), captured from
the live catalog and written to disk. Two files in this directory pair up:

  - `tests/_snapshots.py` (this file): the naming, load, and dump helpers, shared by the
    `snapshot` fixture (which READS them) and `seed/capture_snapshots.py` (which WRITES them).
  - `tests/fixtures/snapshots/*.json`: the captured snapshots, one per seeded dataset.

**Why capture at THIS boundary and not the GraphQL one.** A fixture of a raw GraphQL
response freezes GMS's wire format: it drifts on every DataHub version bump, and a stale
wire-format fixture still parses cleanly and still passes — a green tick about a shape the
server no longer sends. `DatasetSnapshot` is Attest's normalized type; a fixture of it is
tested against exactly the model the checkers consume. The wire format is exercised
elsewhere, live, by `test_client.py` (`DatasetSnapshot.from_graphql`) — the offline tier
deliberately does NOT cover parsing, and says so.

**Fixtures are exactly as honest as the anti-drift pin.** `test_fixture_drift.py` re-fetches
every seeded URN from real GMS and asserts it equals the captured snapshot; it runs in
`just live` / `just preflight`. Without that pin these files would be free to lie. With it,
a seed change or a GMS shape drift fails loudly the next time the live tier runs.
"""

from __future__ import annotations

import re
from pathlib import Path

from attest.datahub import DatasetSnapshot

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "snapshots"

# urn:li:dataset:(urn:li:dataPlatform:<platform>,<name>,<env>)
_DATASET_URN = re.compile(
    r"urn:li:dataset:\(urn:li:dataPlatform:(?P<platform>[^,]+),(?P<name>.+),(?P<env>[^,)]+)\)"
)


def fixture_path(urn: str) -> Path:
    """The on-disk path for a dataset URN's captured snapshot.

    Named `<platform>__<name>.json` — readable in a diff (a reviewer sees which dataset a
    change touches) and unique (platform + name identify a seeded dataset). The naming lives
    in ONE place so the writer (`capture`) and the reader (the `snapshot` fixture) cannot
    disagree about where a file goes; if they did, a test would fail loudly at load, never
    silently read the wrong catalog.
    """
    m = _DATASET_URN.fullmatch(urn)
    if m is None:
        raise ValueError(f"not a dataset URN: {urn!r}")
    return FIXTURE_DIR / f"{m['platform']}__{m['name']}.json"


def load_snapshot(urn: str) -> DatasetSnapshot:
    """Read a captured snapshot. A missing file is a hard error, never a skip.

    The whole point of this tier is that it does not skip when the catalog is absent. So a
    missing fixture is a build error with a fix attached, not a silent green.
    """
    path = fixture_path(urn)
    if not path.exists():
        raise FileNotFoundError(
            f"no captured snapshot for {urn} at {path}. "
            "The fixtures are generated from the live catalog: run `just capture` "
            "(or `just seed`, which captures as its last step)."
        )
    return DatasetSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def dump_snapshot(snap: DatasetSnapshot) -> Path:
    """Write a snapshot to its fixture path, pretty-printed for a reviewable diff."""
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = fixture_path(snap.urn)
    path.write_text(snap.model_dump_json(indent=2), encoding="utf-8")
    return path
