"""Capture serialized `DatasetSnapshot` fixtures from the live catalog.

Run by `just capture`, and — the load-bearing part — by `just seed` as its last step, so
the fixtures and the catalog they describe are regenerated together and cannot drift apart.
A manual regen you have to remember is the weaker guarantee; coupling it to seeding is the
stronger one.

The dataset list is `seed/ground_truth.json`'s `datasets` — the same manifest the seed
wrote — so capturing exactly tracks what was seeded, with nothing to keep in sync by hand.

The snapshots this writes are only as honest as `test_fixture_drift.py`, which re-fetches
every one of these from real GMS in the live tier and asserts equality. Capture without
that pin would be the drift trap; the pin ships in the same session.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from _snapshots import FIXTURE_DIR, dump_snapshot  # noqa: E402
from attest.datahub import DataHubClient, DataHubError, EntityNotFoundError  # noqa: E402

GROUND_TRUTH = ROOT / "seed" / "ground_truth.json"


def main() -> int:
    if not GROUND_TRUTH.exists():
        print(f"{GROUND_TRUTH} is missing — run the seed first.", file=sys.stderr)
        return 1

    urns = [d["urn"] for d in json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["datasets"]]

    with DataHubClient() as client:
        try:
            client.execute("query { appConfig { appVersion } }")
        except DataHubError as exc:
            print(
                f"DataHub is not reachable ({exc}). Start and seed it, then capture. "
                "See docs/datahub-setup.md.",
                file=sys.stderr,
            )
            return 1

        written = 0
        for urn in urns:
            try:
                snap = client.fetch_dataset(urn)
            except EntityNotFoundError:
                print(
                    f"  MISSING from catalog, not captured: {urn}\n"
                    "  The fixture list comes from the seed manifest; a URN the catalog "
                    "does not have means the seed did not fully ingest. Re-run `just seed`.",
                    file=sys.stderr,
                )
                return 1
            path = dump_snapshot(snap)
            print(f"  captured {path.relative_to(ROOT)}")
            written += 1

    print(f"\n{written} snapshots captured to {FIXTURE_DIR.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
