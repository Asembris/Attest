"""Offline pins for the MCP parity probe's client compatibility and its census.

Two things here that no other test can reach, and each of them is a way the parity
measurement could quietly stop being a measurement:

  1. **The probe must read the tool error flag under BOTH names.** `mcp` 2.0.0 renamed
     `CallToolResult.isError` to `is_error` and `InitializeResult.serverInfo` to
     `server_info`, and `pyproject`'s floor (`mcp>=1.2`) admits both. The probe used bare
     attribute access, which under a 2.x client raises `AttributeError` at the FIRST tool
     call -- it never reaches a comparison, and it blames a missing attribute rather than
     the server refusing the call. The fix is to delegate to the shipped, already-tested
     pair in `attest.discovery.mcp`; this pins that it stays delegated.

  2. **The dataset count must be DERIVED from the seed manifest.** Session 17 measured 16
     datasets and the seed now holds 17. A probe that hardcoded a count would compare a
     subset and report a smaller total that reads like the transport improving.

**TWO TIERS IN ONE FILE, and the split is where the evidence lives.** The compatibility and
census-code pins read the probe's SOURCE, which is committed, so they are offline and gate
CI. The two pins that read `seed/ground_truth.json` are marked `live`, because that file is
**generated state** -- `seed/*.json` is gitignored and written by `just seed`, which needs a
live DataHub. `test_fixture_drift.py` reads the same manifest for the same reason and is
marked the same way. Getting this wrong is not theoretical: these two were written unmarked
and CI failed on them with `FileNotFoundError` on a bare runner, which is the offline tier's
one promise -- that it never reaches for something a bare runner does not have -- broken by
a test about reaching for things.

The code properties stay offline because they are where the regressions are: a probe that
reads the wrong field name, or hardcodes a dataset count, is a source defect and is visible
in the source. Whether today's seed happens to contain a group-owned dataset is a fact about
the environment, and it belongs where the environment is.

**Why this parses the source instead of importing it.** `spikes/mcp_reader_probe.py` does
`from mcp import ClientSession` at module scope, and `mcp` is an optional extra that CI
deliberately does not install -- the offline tier is RUN with the module unimportable to
prove it (CLAUDE.md §22). Importing the probe here would pass on a developer machine and
fail in CI. `pytest.importorskip` is not the answer either: at module scope it reports a
SKIP into the tier whose whole claim is that it never skips, and whose skips are made loud
on purpose (§14). So the checks are AST-level, which is the same instrument
`test_discovery_boundary.py` already uses on the real import graph -- and, like that one,
its limit is stated: this is a STATIC property. A probe that reached the legacy field
through `getattr` or a computed name would evade it. What it catches is the regression that
actually happened, written the way it was actually written.

No DataHub, no MCP server, no model, no network, and nothing here imports `mcp` or
`src/attest`. The two `live`-marked tests need only the seed ARTIFACT on disk, not a running
catalog -- but that artifact is produced by a live seed run, so they answer to the same mark.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PROBE = REPO / "spikes" / "mcp_reader_probe.py"
SEED = REPO / "seed" / "ground_truth.json"

# The four spellings of the two renamed fields. ANY of them reached as an attribute is a
# violation in this file, because the probe is supposed to delegate both lookups entirely:
# reading either name directly is how it ends up handling one client and not the other.
RENAMED_FIELDS = ("isError", "is_error", "serverInfo", "server_info")

# The shipped readers the probe must delegate to, and where they must come from.
SHARED_READERS = ("server_identity", "tool_reported_error")
SHARED_MODULE = "attest.discovery.mcp"


def legacy_attribute_reads(source: str) -> list[str]:
    """Every place `source` reaches one of the renamed fields as an attribute.

    AST rather than a substring search, and that distinction is load-bearing: this file's
    own module docstring and several of its comments NAME `isError` and `serverInfo` while
    explaining why they must not be read, and a `grep` would flag the explanation as the
    defect. `ast` sees expressions and never sees a comment.
    """
    tree = ast.parse(source)
    return [
        f"{node.attr} (line {node.lineno})"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in RENAMED_FIELDS
    ]


def imported_names(source: str, module: str) -> set[str]:
    """What `source` imports from `module`, by name."""
    tree = ast.parse(source)
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == module
        for alias in node.names
    }


def function_def(source: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from the probe")


@pytest.fixture(scope="module")
def probe_source() -> str:
    return PROBE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def manifest() -> dict:
    """The seed manifest, or a loud failure naming how to produce it.

    Never a skip. A pin with nothing to pin against is a failure, not a pass -- the rule
    `test_fixture_drift.py` states for the same file. The `live` mark is what keeps this off
    a bare runner; reaching here with no artifact means the LIVE tier was run unseeded, and
    that is worth being told about rather than quietly passing over.
    """
    if not SEED.exists():
        raise AssertionError(
            f"{SEED} is missing. It is generated state (`seed/*.json` is gitignored) and is "
            f"written by `just seed`, which needs a live DataHub. Run `just seed` first."
        )
    return json.loads(SEED.read_text(encoding="utf-8"))


# --- the compatibility pin -------------------------------------------------------------


def test_the_probe_reaches_no_renamed_field_directly(probe_source: str) -> None:
    """Neither spelling of either renamed field is read as an attribute in the probe.

    This is the whole regression in one assertion. `result.isError` answers correctly on a
    1.x client and raises `AttributeError` on a 2.x one; `result.is_error` does the reverse.
    Reading either directly means handling one client and not the other, and the probe has
    no business choosing.
    """
    found = legacy_attribute_reads(probe_source)
    assert found == [], (
        f"{PROBE.name} reaches a renamed MCP field directly: {found}. "
        f"Both spellings must go through {SHARED_MODULE}'s readers, which handle the "
        f"1.x/2.x rename in one tested place."
    )


def test_the_probe_delegates_to_the_shipped_readers(probe_source: str) -> None:
    """It imports the shared pair rather than carrying its own copy.

    The absence check above is satisfied by a probe that reads neither field AND does no
    error checking at all, which would silently turn every refused tool call into a parse
    error. This is the other half: the delegation must actually be there.
    """
    assert set(SHARED_READERS) <= imported_names(probe_source, SHARED_MODULE)


def test_a_legacy_only_probe_is_caught_by_this_detector() -> None:
    """VACUITY, the direction that matters: the detector finds the code that shipped.

    A check that reports "clean" is indistinguishable from a check looking for the wrong
    string. This is the probe's real pre-fix body, and it must be flagged.
    """
    legacy = (
        "result = await session.call_tool('get_entities', {'urns': [urn]})\n"
        "if result.isError:\n"
        "    raise SystemExit(f'get_entities failed for {urn}')\n"
        "print(f'{init.serverInfo.name} v{init.serverInfo.version}')\n"
    )
    found = legacy_attribute_reads(legacy)
    assert "isError (line 2)" in found
    assert any(f.startswith("serverInfo") for f in found)


def test_a_modern_only_probe_is_caught_too() -> None:
    """VACUITY: the detector is not merely a check for the OLD spelling.

    Pinning only `isError` would let someone "fix" 2.x compatibility by hardcoding
    `is_error` -- which breaks the 1.x client the pyproject floor still admits, and which
    is the same defect facing the other way.
    """
    modern = "if result.is_error:\n    raise SystemExit('nope')\n"
    assert legacy_attribute_reads(modern) == ["is_error (line 1)"]


def test_the_detector_passes_the_delegated_form() -> None:
    """VACUITY: it is not a check that flags everything.

    A detector that cannot pass has nothing to say when the probe is correct.
    """
    assert legacy_attribute_reads("if tool_reported_error(result):\n    pass\n") == []


# --- the census pin --------------------------------------------------------------------


def countable_literals(node: ast.AST) -> list[int]:
    """Integer literals in `node` that could stand in for a dataset count.

    `0` and `1` are excluded, and the exclusion is deliberate rather than convenient: they
    are structural (an index, an emptiness test, the `> 1` in a duplicate check) and a seed
    census is never either of them. Everything from `2` up is a count-shaped literal and
    has no business in a function whose entire job is to derive the count.
    """
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant)
        and isinstance(n.value, int)
        and not isinstance(n.value, bool)
        and n.value > 1
    ]


def test_the_census_hardcodes_no_dataset_count(probe_source: str) -> None:
    """`census()` carries no count-shaped literal, so the count can only be derived.

    Session 17 compared 16 datasets; the seed holds 17. The failure this forbids is a
    literal written to match today's seed, which silently drops tomorrow's dataset and
    reports a smaller mismatch total that reads exactly like the transport getting better.
    """
    literals = countable_literals(function_def(probe_source, "census"))
    assert literals == [], (
        f"census() contains count-shaped literal(s) {literals}. The dataset count must "
        f"come from the manifest -- there is deliberately nothing here to fall out of date."
    )


def test_a_hardcoded_census_count_is_caught_by_this_detector() -> None:
    """VACUITY: the count detector finds the literal it exists to forbid.

    Without this, `countable_literals` returning `[]` would be indistinguishable from a
    detector that never looks at anything.
    """
    pinned = ast.parse(
        "def census(manifest):\n"
        "    urns = [d['urn'] for d in manifest['datasets']]\n"
        "    assert len(urns) == 17\n"
        "    return urns\n"
    )
    assert countable_literals(pinned) == [17]


@pytest.mark.live
def test_the_seed_manifest_holds_a_group_owned_dataset(manifest: dict) -> None:
    """The fact `census()` refuses to run without.

    CorpGroup ownership is the one shape Attest's own reader was blind to until an external
    catalog found it (CLAUDE.md §23). A parity run that does not include a group-owned
    dataset cannot say whether this transport is blind to it too, so the probe aborts and
    this pins that the seed can satisfy it.
    """
    group_owned = [d for d in manifest["datasets"] if d.get("owner_groups")]
    assert group_owned, "no group-owned dataset in the seed manifest; re-seed"


@pytest.mark.live
def test_every_manifest_urn_is_present_and_unique(manifest: dict) -> None:
    """The two census preconditions, pinned against the manifest itself.

    A duplicate URN would be compared twice and inflate both the numerator and the
    denominator; a missing one would be a `KeyError` deep in the run rather than a refusal
    before the network is touched.
    """
    urns = [d.get("urn") for d in manifest["datasets"]]
    assert all(urns), "a manifest entry has no urn"
    assert len(set(urns)) == len(urns), "duplicate URNs in the manifest"


def test_the_frozen_methodology_is_versioned(probe_source: str) -> None:
    """The comparison rules travel with the receipt under a version.

    Two parity receipts are only comparable if they agree on what they were measuring. The
    version is what lets a later reader tell -- without diffing the probe -- whether a
    refresh changed the rules or only the catalog.
    """
    tree = ast.parse(probe_source)
    block = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "METHODOLOGY" for t in node.targets)
    )
    assert block["version"] == "parity-v1"
    assert block["reference"] == "graphql"
    assert block["measured"] == "mcp"
    # Absence, emptiness and malformation stay three states. If this rule ever relaxes, the
    # receipts either side of the change are not comparable and the version must move.
    assert "None != () != {}" in block["absent_empty_rule"]
