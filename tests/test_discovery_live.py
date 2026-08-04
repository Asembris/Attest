"""The REAL DataHub MCP Server, the real catalog, and the handoff between them.

`just discover`. Live tier: it launches the actual `mcp-server-datahub` over stdio (uvx
downloads it on first run) and talks to the seeded catalog. No model, so no money — but it
needs DataHub up and it spawns a subprocess.

**WHAT THIS PROVES THAT NOTHING ELSE CAN.** The offline tier fakes the transport, and a fake
is a Python object: it spawns no subprocess, speaks no JSON-RPC, negotiates no TLS through
uv's Rust root store, and cannot fail the way the real thing fails (CLAUDE.md, Session 5).
So the offline tests prove the taxonomy and the boundary; this proves the pipe exists and
that the server still answers the way `docs/mcp-evaluation.md` measured it answering.

**AND IT PROVES THE HANDOFF, which is the whole architecture in one test:**

    MCP discovers  ->  a human resolves  ->  GraphQL verifies  ->  code decides

Every URN the MCP server hands back is fetched over GraphQL into a real `DatasetSnapshot` —
the read Attest actually audits against. If the identifier the lossy transport returns were
not byte-identical to the catalog's own, that fetch would fail, and the one value discovery
is allowed to pass on would be worthless. It does not fail, and that is the measured basis
for §12's finding not applying to this path.
"""

from __future__ import annotations

import json
import shutil

import pytest

from attest.config import settings
from attest.discovery import DiscoveryUnavailable
from attest.discovery.mcp import McpDiscovery

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def discovery():
    """The REAL thing: a stdio session to a real `mcp-server-datahub`.

    Skips LOUDLY and by name when its two out-of-tree requirements are missing. `mcp` is an
    optional extra and uvx is not a Python package, so neither is guaranteed by `pip install`
    — and a silent skip here would leave the only test of the real transport quietly unrun,
    which is the shape this repo keeps closing.
    """
    pytest.importorskip("mcp", reason="the `mcp` client is not installed — pip install -e '.[mcp]'")
    launcher = settings.mcp_command.split()[0]
    if shutil.which(launcher) is None:
        pytest.skip(f"{launcher!r} is not on PATH, so the MCP server cannot be launched")

    with McpDiscovery() as found:
        try:
            found.search("customer", limit=1)
        except DiscoveryUnavailable as exc:
            pytest.skip(f"the MCP server did not start: {exc}")
        yield found


def test_the_real_mcp_server_finds_seeded_datasets_by_prefix(discovery, capsys):
    """A partial name, typed into the picker, matches the datasets a human is looking for."""
    from tests.conftest import DOCUMENTED

    found = discovery.search("custo", limit=10)

    assert found.hits, "the real MCP server matched nothing for 'custo' on a seeded catalog"
    assert found.advisory is True
    assert found.server.startswith("datahub"), found.server
    urns = [h.urn for h in found.hits]
    assert DOCUMENTED in urns, (
        f"the seeded customer_profile dataset is not among {urns}. Either the catalog is not "
        "seeded (`just seed`) or the server's keyword syntax has changed."
    )
    # DATASETS ONLY. Attest can audit nothing else, and the filter is server-side.
    assert all(u.startswith("urn:li:dataset:") for u in urns), urns

    with capsys.disabled():
        print(f"\n\n  REAL MCP SEARCH: '/q custo*' -> {found.total} matches")
        print(f"    server: {found.server}")
        for h in found.hits[:6]:
            print(f"    {h.name:24} {h.urn.split(',')[1]}")


def test_every_urn_the_mcp_server_returns_is_verifiable_over_graphql(discovery, client, capsys):
    """THE HANDOFF. MCP discovers; GraphQL verifies. In one test, against one catalog.

    The URN is the ONLY value that crosses the discovery boundary, and this is why it can:
    it survives the transport byte-for-byte, so the read Attest audits against resolves it
    exactly. §12's finding is about lossy FIELD CONTENT — tags flattened to display names,
    `type` dropped, `lastModified` never requested — and none of that content is here to be
    lost, because none of it is passed on.

    A failure here would mean the identifier itself had become unreliable, and the whole
    integration would have to go.
    """
    found = discovery.search("customer", limit=10)
    assert found.hits

    verified = []
    for hit in found.hits:
        snapshot = client.fetch_dataset(hit.urn)
        assert snapshot.urn == hit.urn, "the catalog resolved a different URN than was picked"
        verified.append((hit, snapshot))

    with capsys.disabled():
        print("\n\n  MCP DISCOVERS -> GRAPHQL VERIFIES")
        for hit, snap in verified:
            fields = len(snap.fields or ())
            print(
                f"    {hit.urn.split(',')[1]:44} -> snapshot: "
                f"{fields} columns, owners={len(snap.owners or ())}, tags={len(snap.tags or ())}"
            )


def test_the_search_response_still_has_the_shape_this_integration_was_built_against(
    discovery, capsys
):
    """A TRIPWIRE, in the spirit of `just spike-mcp`.

    Two things move independently here: the MCP server's own release cadence and DataHub's.
    The zero-results rule (`total` discriminating an empty answer from a broken one) is read
    off a measured response shape, so it is worth failing loudly the day that shape changes
    rather than silently reinterpreting whatever arrives.

    This asserts what the parser DEPENDS on and nothing more: a `total`, and results carrying
    an entity URN. It deliberately does not assert on names, ordering or facets — those are
    the server's business, and a test that pinned them would break on a harmless change.
    """
    import asyncio

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from attest.discovery.mcp import DATASET_FILTER, SEARCH_TOOL, stdio_parameters

    async def raw() -> dict:
        async with stdio_client(stdio_parameters()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    SEARCH_TOOL,
                    {"query": "/q custo*", "filter": DATASET_FILTER, "num_results": 5},
                )
                assert not result.isError, result.content
                return json.loads(result.content[0].text)

    payload = asyncio.run(raw())

    assert isinstance(payload.get("total"), int), (
        "the search response no longer carries `total`. The zero-results rule rests on it: "
        "without it, an empty answer cannot be told from a response that lost its results, "
        "and Attest would have to guess — which is the one thing it does not do."
    )
    assert payload["total"] > 0 and payload.get("searchResults"), payload
    for row in payload["searchResults"]:
        assert row["entity"]["urn"].startswith("urn:li:dataset:"), row

    with capsys.disabled():
        print("\n\n  SHAPE TRIPWIRE: total + searchResults[].entity.urn — still as measured")
        print(f"    keys: {sorted(payload)}")
