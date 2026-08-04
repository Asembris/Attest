"""Load the EXTERNAL trial catalog: DataHub's own `showcase-ecommerce` datapack.

    just external-ingest            download, verify, filter, ingest
    just external-ingest --plan     report what Core would refuse; ingest nothing

This exists so the external trial is reproducible from a named command rather than from a
paragraph of instructions. It selects no metadata values: everything ingested is the pack's
own content, byte for byte.

Three things it does that `datahub docker ingest-sample-data --pack showcase-ecommerce`
does not, each for a stated reason.

**It injects truststore.** The CLI resolves the pack registry over `requests`, which trusts
certifi and nothing else, so on a TLS-inspecting network it dies `CERTIFICATE_VERIFY_FAILED`
— and the bundled fallback `registry.json` is not shipped inside the wheel, so there is no
offline path. This is the FOURTH runtime to need the OS trust store in this repo (after
Python's OpenAI/httpx, Node, and uvx); CLAUDE.md's rule is to reach for the system-CA
opt-in before debugging an "outage", and that is what this is.

**It preserves the pack's ORIGINAL timestamps.** The CLI's `--pack` path sets
`no_time_shift: false`, which rewrites every timestamp to the moment of ingest. That would
make every freshness verdict in the trial an artifact of the ingest clock — a "stale" table
reading fresh because it was loaded today — and the receipt would be unreproducible by
construction. So the pack is ingested through the ordinary `file` source with its recorded
times intact.

**It filters against the SERVER's own aspect registry** — `DataHubGraph.get_entity_aspect_specs`,
the same call `datahub datapack load` makes. The showcase pack carries DataHub Cloud aspects
(`entityInferenceMetadata`, `lineageFeatures`, `usageFeatures`, `storageFeatures`,
`assertionsSummary`, `proposals`) that Core v1.5.0.6 has no place for. Measured: 248 of 3809
MCPs refused. None of them is an aspect Attest reads. This is a mechanical compatibility
filter driven by the live server, not a hand-picked subset.

`03-context.json` (54 `document` entities) is deliberately NOT loaded: Attest reads no
`document`, and the filter passes unknown entity types straight through to the server
untested. Out of scope, said out loud.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import truststore

truststore.inject_into_ssl()

# AFTER the injection, deliberately: truststore rewires how new SSL contexts are built, and
# a client constructed before it would hold a pre-injection context. That is the exact shape
# of the Session 5 TLS bug — a repair that logs success and changes nothing.
import httpx  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

BASE = "https://raw.githubusercontent.com/datahub-project/static-assets/main/datapacks/showcase-ecommerce/"

# Pinned by CONTENT, not by trust in a URL. `main` moves; a checksum mismatch means the
# trial's catalog is not the catalog the receipt was measured against, and that must be a
# loud failure rather than a silently different run.
FILES = {
    "01-definitions.json": "3ecaf19324736331d6edb80c4fd7363d5b15fd41e2f9daf3ac68506aedf86608",
    "02-data.json": "0b3aeaa8941acc79f7d08db370ec092912ca77c15c2c2cdba2bf243225630c90",
}
GMS = os.getenv("DATAHUB_GMS_URL", "http://localhost:8080")


def fetch(cache: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, want in FILES.items():
        path = cache / name
        if not path.exists():
            print(f"  downloading {name} …")
            r = httpx.get(BASE + name, timeout=300, follow_redirects=True)
            r.raise_for_status()
            path.write_bytes(r.content)
        got = hashlib.sha256(path.read_bytes()).hexdigest()
        if got != want:
            raise SystemExit(
                f"{name} checksum mismatch.\n  expected {want}\n  got      {got}\n"
                f"The upstream pack has changed, so this is NOT the catalog "
                f"docs/external-trial/results.json was measured against."
            )
        print(f"  {name}: {path.stat().st_size:>9,} bytes, sha256 ok")
        out[name] = path
    return out


def filter_for_core(paths: dict[str, Path], workdir: Path) -> tuple[dict[str, Path], dict]:
    """Drop the MCPs this server has no aspect for, using the server's own registry."""
    from datahub.cli.datapack.loader import _is_mcp_compatible
    from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph

    specs = DataHubGraph(DatahubClientConfig(server=GMS)).get_entity_aspect_specs()
    if specs is None:
        raise SystemExit(f"could not read the entity/aspect registry from {GMS}; is it up?")

    filtered: dict[str, Path] = {}
    report: dict = {"skipped": {}, "kept": 0, "total": 0}
    for name, path in paths.items():
        data = json.loads(path.read_text(encoding="utf-8"))
        keep = []
        for mcp in data:
            et, an = mcp.get("entityType", ""), mcp.get("aspectName", "")
            if _is_mcp_compatible(specs, et, an):
                keep.append(mcp)
            else:
                key = f"{et}/{an}"
                report["skipped"][key] = report["skipped"].get(key, 0) + 1
        report["kept"] += len(keep)
        report["total"] += len(data)
        dest = workdir / f"core-{name}"
        dest.write_text(json.dumps(keep), encoding="utf-8")
        filtered[name] = dest
    return filtered, report


def ingest(path: Path, workdir: Path) -> None:
    """Ingest one file through the ordinary file -> datahub-rest recipe.

    The recipe's path is RELATIVE and the pipeline runs from `workdir`: an absolute Windows
    path hits a drive-letter parsing bug in the DataHub CLI, which reads `D:\\...` as a URI
    scheme and fails with "Did not find a registered class for d". See CLAUDE.md.
    """
    from datahub.ingestion.run.pipeline import Pipeline as IngestPipeline

    recipe = {
        "source": {"type": "file", "config": {"path": f"./{path.name}"}},
        "sink": {"type": "datahub-rest", "config": {"server": GMS}},
    }
    cwd = Path.cwd()
    try:
        os.chdir(workdir)
        pipeline = IngestPipeline.create(recipe)
        pipeline.run()
        pipeline.raise_from_status()
    finally:
        os.chdir(cwd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--plan",
        action="store_true",
        help="report what this server would refuse, and ingest nothing",
    )
    args = ap.parse_args()

    # The CLI posts usage telemetry to Mixpanel on startup. A tool that loads someone's
    # catalog should not also report to a third party that it ran. Same reasoning as
    # spikes/mcp_reader_probe.py.
    os.environ.setdefault("DATAHUB_TELEMETRY_ENABLED", "false")

    cache = Path(tempfile.gettempdir()) / "attest-external-pack"
    cache.mkdir(parents=True, exist_ok=True)
    print(f"\n=== showcase-ecommerce -> {GMS}\n    cache: {cache}\n")

    paths = fetch(cache)
    workdir = cache / "core"
    workdir.mkdir(exist_ok=True)
    filtered, report = filter_for_core(paths, workdir)

    print(f"\n  Core compatibility: {report['kept']}/{report['total']} MCPs accepted")
    for key, n in sorted(report["skipped"].items(), key=lambda kv: -kv[1]):
        print(f"    refused  {key:44} {n}")
    print("    (all DataHub Cloud aspects; none is an aspect Attest reads)")

    if args.plan:
        print("\n  --plan: nothing ingested.")
        return 0

    # Definitions FIRST: the structured-property definitions the dataset values reference.
    for name in ("01-definitions.json", "02-data.json"):
        print(f"\n  ingesting {name} …")
        ingest(filtered[name], workdir)

    print("\n  done. Now: just external-trial")
    return 0


if __name__ == "__main__":
    sys.exit(main())
