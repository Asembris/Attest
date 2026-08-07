"""Catalog discovery: the taxonomy, the transport, and the failures a real one has.

**THE RULE THIS FILE EXISTS FOR.** A search that matched nothing and a search that never
happened are different facts, and the DataHub MCP server sends them in shapes that are one
key apart:

    {"start": 0, "count": 5, "total": 0, "facets": [...]}          zero matches. A real answer.
    {"start": 0, "count": 5, "total": 4, "facets": [...]}          BROKEN. The results are gone.

Both are missing `searchResults` — the server strips empty arrays — so the discriminator is
`total`, and the obvious implementation (`payload.get("searchResults", [])`) collapses them
into "nothing found". That is §20's collapse at a third transport, in a project that has
already shipped it at the model provider (§18) and at the catalog read (§20), and whose whole
thesis is that absence is not an answer.

So the guard has a vacuity check, and it is load-bearing rather than decorative:
`test_the_naive_parse_reports_a_broken_response_as_an_empty_catalog` swaps in the one-line
naive version and demands the malformed case start reading as empty. If that ever passes with
the real parser in place, this file is proving nothing.

Everything here is OFFLINE: no DataHub, no key, no `mcp`, no subprocess. The fake is a
transport, never a stand-in for the code under test — `McpDiscovery` runs its real loop
thread, its real single-task supervisor, its real lock, its real timeouts and its real
parsing against it.
"""

from __future__ import annotations

import json

import pytest

from attest.discovery import (
    ADVISORY_NOTE,
    DiscoveryNotConfigured,
    DiscoveryUnavailable,
)
from attest.discovery.mcp import (
    DATASET_FILTER,
    MAX_RESULTS,
    SEARCH_TOOL,
    UNKNOWN_SERVER,
    McpDiscovery,
    build_query,
    parse_search_payload,
    server_identity,
    tool_reported_error,
)
from fakes import FakeMcpServer, FakeToolResult, search_payload

PROFILE = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)"
CONTACT = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_contact,PROD)"

# The response the real server sent for `/q customer`, verbatim from the probe. Kept whole —
# facets and all — so the parser is exercised against what arrives, not against a tidied
# version of it.
MEASURED = {
    "start": 0,
    "count": 5,
    "total": 4,
    "searchResults": [
        {"entity": {"urn": CONTACT, "properties": {"name": "customer_contact"}}},
        {"entity": {"urn": PROFILE, "properties": {"name": "customer_profile"}}},
    ],
    "facets": [
        {
            "field": "platform",
            "displayName": "Platform",
            "aggregations": [
                {"value": "urn:li:dataPlatform:snowflake", "count": 8},
            ],
        }
    ],
}

# What the real server sends when nothing matches: `total: 0`, and NO `searchResults` key,
# because `clean_gql_response` drops empty arrays. Measured, not constructed.
MEASURED_EMPTY = {
    "start": 0,
    "count": 5,
    "total": 0,
    "facets": [{"field": "_entityType", "aggregations": [{"value": "DATASET", "count": 0}]}],
}


def discovery(server: FakeMcpServer) -> McpDiscovery:
    return McpDiscovery(session_factory=server.session)


# --- the query, as the server's own syntax -----------------------------------


def test_a_single_word_is_a_prefix_search_and_anything_else_is_passed_through() -> None:
    """MEASURED: `/q custo` matches 4 seeded datasets and `/q custo*` matches 6.

    A picker whose results only appear once the whole name is typed is a picker nobody uses,
    so a bare word gets the wildcard. Anything that already looks like a query — an explicit
    `/q`, several words, or an operator — is left exactly as written rather than rewritten
    into something the caller did not ask for.
    """
    assert build_query("custo") == "/q custo*"
    assert build_query("  custo  ") == "/q custo*"
    assert build_query("") == "*"
    assert build_query("   ") == "*"
    assert build_query("customer profile") == "/q customer profile"
    assert build_query("/q tag:PII") == "/q tag:PII"
    assert build_query("revenue*") == "/q revenue*"
    assert build_query("(sales OR revenue)") == "/q (sales OR revenue)"


# --- the taxonomy: empty is an answer, malformed is not ----------------------


def test_the_measured_response_becomes_hits_carrying_only_a_urn_and_a_name() -> None:
    found = parse_search_payload(MEASURED, server="datahub v3.4.5")

    assert [h.urn for h in found.hits] == [CONTACT, PROFILE]
    assert [h.name for h in found.hits] == ["customer_contact", "customer_profile"]
    assert found.total == 4
    assert found.server == "datahub v3.4.5"
    # Every response says what it is. Not a docs caveat: a field, on every call.
    assert found.advisory is True
    assert found.note == ADVISORY_NOTE
    # `total` exceeds what was returned, so this is a PAGE. Round-tripped rather than
    # discarded, for the same reason `Retrieval.total` is (Session 21 gap 1).
    assert found.total > len(found.hits)


def test_zero_matches_is_a_real_empty_answer_and_not_an_error() -> None:
    """The catalog was asked and said nothing matched. That is an answer, and it is a 200."""
    found = parse_search_payload(MEASURED_EMPTY)

    assert found.hits == ()
    assert found.total == 0


def test_a_response_that_lost_its_results_is_UNAVAILABLE_and_never_an_empty_catalog() -> None:
    """`total > 0` with no `searchResults`: the transport failed, and it must SAY so.

    This is the whole file. Reported as an empty list, a broken response tells a human their
    catalog holds no dataset by that name — which is Attest committing its own cardinal sin,
    in its own output, at the one transport §12 predicted it would happen at.
    """
    broken = {"start": 0, "count": 5, "total": 4, "facets": []}

    with pytest.raises(DiscoveryUnavailable) as caught:
        parse_search_payload(broken)

    assert "4 matches" in str(caught.value)
    assert "not an empty catalog" in str(caught.value)


def _naive_parse(payload: object) -> list[str]:
    """The one-line version anyone would write. Kept HERE so the guard can be falsified."""
    return [
        row["entity"]["urn"]
        for row in (payload or {}).get("searchResults", [])  # type: ignore[union-attr]
    ]


def test_the_naive_parse_reports_a_broken_response_as_an_empty_catalog() -> None:
    """THE VACUITY CHECK, and it runs in the suite rather than in a command to remember.

    Same precedent as `test_breaking_a_checker_collapses_the_benchmark`: a guard whose
    falsification lives in a script someone has to run is a guard that rots. This proves the
    two payloads are genuinely one key apart, that the naive parse cannot tell them apart,
    and therefore that the test above is testing something.
    """
    broken = {"start": 0, "count": 5, "total": 4, "facets": []}

    # The naive parse gives the SAME answer for both — an empty catalog — which is exactly
    # the collapse. The strict parse gives two different answers.
    assert _naive_parse(broken) == []
    assert _naive_parse(MEASURED_EMPTY) == []

    assert parse_search_payload(MEASURED_EMPTY).hits == ()
    with pytest.raises(DiscoveryUnavailable):
        parse_search_payload(broken)


def test_a_body_that_is_not_a_search_result_is_unavailable() -> None:
    for payload in ([], "nope", None, 3):
        with pytest.raises(DiscoveryUnavailable):
            parse_search_payload(payload)


def test_a_response_with_no_total_is_unavailable_because_nothing_could_be_told_apart() -> None:
    """Without `total`, "empty" and "broken" are indistinguishable — so neither is claimed."""
    with pytest.raises(DiscoveryUnavailable) as caught:
        parse_search_payload({"start": 0, "count": 5})
    assert "`total`" in str(caught.value)


def test_a_result_row_with_no_urn_is_unavailable_rather_than_quietly_skipped() -> None:
    """The URN is the ONE field discovery passes on. A row without it is the transport
    failing at the only thing it is being asked for."""
    with pytest.raises(DiscoveryUnavailable) as caught:
        parse_search_payload(
            {
                "start": 0,
                "count": 1,
                "total": 1,
                "searchResults": [{"entity": {"properties": {"name": "orphan"}}}],
            }
        )
    assert "no entity URN" in str(caught.value)


def test_an_entity_with_no_name_keeps_its_urn_and_reports_an_empty_name() -> None:
    """A missing display name is not a missing entity, and the URN is never invented from it."""
    found = parse_search_payload(
        {"start": 0, "count": 1, "total": 1, "searchResults": [{"entity": {"urn": PROFILE}}]}
    )
    assert found.hits[0].urn == PROFILE
    assert found.hits[0].name == ""


def test_a_non_dataset_candidate_is_dropped_and_the_drop_is_NAMED() -> None:
    """Attest audits datasets, so any other URN could never be checked — but a shorter list
    with no explanation is the silent absence this project refuses. It is counted out loud."""
    found = parse_search_payload(
        {
            "start": 0,
            "count": 2,
            "total": 2,
            "searchResults": [
                {"entity": {"urn": PROFILE, "properties": {"name": "customer_profile"}}},
                {"entity": {"urn": "urn:li:chart:(looker,dashboard_1)"}},
            ],
        }
    )
    assert [h.urn for h in found.hits] == [PROFILE]
    assert "1 non-dataset candidate(s) were dropped" in found.note


# --- the transport, with the failures a real one has -------------------------


def test_a_search_asks_for_datasets_only_and_never_more_than_the_cap() -> None:
    server = FakeMcpServer(replies=[MEASURED])
    with discovery(server) as found:
        found.search("custo", limit=500)

    tool, args = server.calls[0]
    assert tool == SEARCH_TOOL
    assert args == {
        "query": "/q custo*",
        # Offering a Chart URN would offer one `BaseClaim.target_urn` refuses, so the scope
        # is server-side and not a hopeful filter afterwards.
        "filter": DATASET_FILTER,
        "num_results": MAX_RESULTS,
    }


def test_the_session_is_started_once_and_reused() -> None:
    """3.33s to spawn, 125-344ms to call (measured). A session per keystroke is unusable."""
    server = FakeMcpServer(replies=[MEASURED])
    with discovery(server) as found:
        found.search("a")
        found.search("b")
        found.search("c")

    assert server.starts == 1
    assert len(server.calls) == 3


def test_nothing_is_spawned_until_the_first_search() -> None:
    """Lazy, so a deployment that never opens the picker never runs an MCP server."""
    server = FakeMcpServer(replies=[MEASURED])
    with discovery(server) as found:
        assert server.starts == 0
        assert found.status() == "not-contacted"
        found.search("a")
        assert server.starts == 1
        assert found.status() == "up: datahub v3.4.5"


def test_a_server_that_will_not_start_is_unavailable_and_the_next_search_tries_again() -> None:
    """Self-healing without a retry loop: a dead session is dropped, not cached.

    A transient failure that became a permanent property of the process would be the
    cross-run snapshot mistake (§2c) in a new place — a fact about one moment, remembered as
    a fact about the world.
    """
    server = FakeMcpServer(replies=[MEASURED], start_faults={1: OSError("uvx: not found")})
    with discovery(server) as found:
        with pytest.raises(DiscoveryUnavailable) as caught:
            found.search("custo")
        assert "uvx: not found" in str(caught.value)
        assert found.status().startswith("unreachable:")

        # The SECOND attempt builds a new session — and the fake recorded the first start, so
        # this count is not vacuous.
        got = found.search("custo")
        assert [h.urn for h in got.hits] == [CONTACT, PROFILE]
    assert server.starts == 2


def test_a_tool_that_reports_isError_is_unavailable_and_never_an_empty_result() -> None:
    """The real server reports a bad filter as `isError: True` with text, not as an
    exception (measured). It is still a failure, and it is still not an empty catalog."""
    server = FakeMcpServer(
        replies=[FakeToolResult("Error calling tool 'search': Expected =", is_error=True)]
    )
    with discovery(server) as found, pytest.raises(DiscoveryUnavailable) as caught:
        found.search("custo")
    assert "refused the search" in str(caught.value)


def test_a_body_that_is_not_json_is_unavailable() -> None:
    server = FakeMcpServer(replies=["<html>502 Bad Gateway</html>"])
    with discovery(server) as found, pytest.raises(DiscoveryUnavailable) as caught:
        found.search("custo")
    assert "not JSON" in str(caught.value)


def test_a_call_that_hangs_times_out_and_drops_the_session(monkeypatch) -> None:
    """A timed-out JSON-RPC leaves a session nobody can vouch for, so it is not kept."""
    from attest.config import settings

    monkeypatch.setattr(settings, "mcp_call_timeout_seconds", 0.2)
    server = FakeMcpServer(replies=[MEASURED], delay=5.0)
    with discovery(server) as found:
        with pytest.raises(DiscoveryUnavailable) as caught:
            found.search("custo")
        assert "did not answer within" in str(caught.value)
        assert found._session is None, "a timed-out session must be dropped, not reused"


def test_a_call_that_raises_mid_flight_is_unavailable_and_attest_adds_no_retry() -> None:
    """ONE failure is ONE call. There is no transport-level retry underneath to complement
    (unlike the OpenAI SDK's, §18), so a loop here would be Attest inventing patience nobody
    asked for. The fake records the call BEFORE raising, so this count cannot pass
    vacuously."""
    server = FakeMcpServer(replies=[MEASURED], faults={1: RuntimeError("pipe closed")})
    with discovery(server) as found, pytest.raises(DiscoveryUnavailable):
        found.search("custo")

    assert len(server.calls) == 1


def test_discovery_switched_off_is_not_configured_rather_than_unavailable(monkeypatch) -> None:
    """Retrying cannot switch a feature back on. Same split as ProviderRefused vs
    ProviderUnavailable, and the route turns it into a 501 rather than a 503."""
    from attest.config import settings

    monkeypatch.setattr(settings, "discovery_enabled", False)
    server = FakeMcpServer(replies=[MEASURED])
    with discovery(server) as found:
        with pytest.raises(DiscoveryNotConfigured):
            found.search("custo")
        assert found.status() == "disabled"
    assert server.starts == 0


def test_a_missing_client_library_is_not_configured_and_is_not_an_outage() -> None:
    """`mcp` is an optional extra CI never installs. A deployment without it does not have
    this feature; it is not having a bad day."""
    def refuse():
        raise DiscoveryNotConfigured("the `mcp` client is not installed")

    found = McpDiscovery(session_factory=refuse)
    with found, pytest.raises(DiscoveryNotConfigured):
        found.search("custo")


def test_close_is_idempotent_and_stops_the_session() -> None:
    server = FakeMcpServer(replies=[MEASURED])
    found = discovery(server)
    found.search("custo")
    assert server.starts == 1

    found.close()
    found.close()

    assert server.stops == 1, "the session's context must be exited exactly once"
    assert found._loop is None
    # A closed handle does not silently start a new server behind the caller's back.
    with pytest.raises(DiscoveryNotConfigured):
        found.search("custo")


def test_a_json_string_reply_is_parsed_exactly_as_the_dict_would_be() -> None:
    """The wire carries text; the fake can send either. Same answer, or the fake is lying."""
    server = FakeMcpServer(replies=[json.dumps(MEASURED)])
    with discovery(server) as found:
        got = found.search("custo")
    assert [h.urn for h in got.hits] == [CONTACT, PROFILE]


def test_an_empty_search_lists_the_catalog_rather_than_refusing() -> None:
    server = FakeMcpServer(replies=[search_payload((PROFILE, "customer_profile"))])
    with discovery(server) as found:
        got = found.search("")
    assert server.calls[0][1]["query"] == "*"
    assert [h.urn for h in got.hits] == [PROFILE]


# --- the handshake: two field names, one identity (Session 33) ----------------
#
# `mcp` 2.0.0 renamed `InitializeResult.serverInfo` to `server_info`. Attest read only the
# legacy name, so against the current client every session came back UNIDENTIFIED and
# `/health` dropped from `up: datahub v3.4.5` to a bare `up` — live, silently, with the
# offline tier green throughout.
#
# THE OFFLINE TIER WAS GREEN BECAUSE THE FAKE EMITTED THE OLD SHAPE. That is Session 5's
# rule at the handshake: a fake cannot fail the way the real thing fails when the fake was
# written against the shape the real thing stopped sending. `FakeMcpServer` now defaults to
# the MODERN shape and can be asked for either, which is what makes these tests mean
# something.


def test_the_modern_server_info_shape_identifies_the_server() -> None:
    """`mcp>=2` — the shape the installed client actually sends."""
    server = FakeMcpServer(replies=[MEASURED], server=("datahub", "3.4.5"), shape="modern")
    with discovery(server) as found:
        got = found.search("custo")
        assert found.status() == "up: datahub v3.4.5"
    assert got.server == "datahub v3.4.5"


def test_the_legacy_server_info_shape_is_still_identified() -> None:
    """`mcp<2` — still supported, because the floor in pyproject still admits it.

    Spanning both shapes beats pinning to one: a pin has to be bumped by hand and still
    breaks at the next rename, while this keeps working across the boundary in both
    directions.
    """
    server = FakeMcpServer(replies=[MEASURED], server=("datahub", "3.4.5"), shape="legacy")
    with discovery(server) as found:
        got = found.search("custo")
        assert found.status() == "up: datahub v3.4.5"
    assert got.server == "datahub v3.4.5"


@pytest.mark.parametrize(
    "shape",
    ["missing", "empty", "nameless", "versionless"],
    ids=["no-field-at-all", "field-is-None", "no-name", "no-version"],
)
def test_a_server_that_will_not_identify_itself_is_UNKNOWN_never_empty(shape: str) -> None:
    """The collapse this refuses: `""` ALREADY MEANS "no handshake has happened".

    Reporting a session that IS up with the same empty string as one that was never
    contacted is `ReadState.UNKNOWN` vs `INCOMPLETE` one transport out, and `Trace.cost`
    reporting None rather than 0 — a value Attest cannot state must not be spelled the same
    way as a state it has not reached. So there are THREE values, and the third is explicit.

    It is a degraded state rather than a raise, deliberately: search is unaffected, and
    taking a working discovery service down over a provenance string would be this file
    adding a failure mode to the one place §22 promised not to.
    """
    server = FakeMcpServer(replies=[MEASURED], shape=shape)
    with discovery(server) as found:
        got = found.search("custo")
        assert found.status() == "up: unknown"
    assert got.server == UNKNOWN_SERVER
    assert got.server != ""
    # Degraded provenance, and NOTHING else. The hits are the hits.
    assert [h.urn for h in got.hits] == [CONTACT, PROFILE]
    assert got.total == 4
    assert got.advisory is True


def test_never_contacted_and_could_not_identify_itself_stay_different_answers() -> None:
    """The three states, at the one surface a human reads."""
    server = FakeMcpServer(replies=[MEASURED], shape="missing")
    with discovery(server) as found:
        assert found.status() == "not-contacted"  # no handshake yet
        found.search("custo")
        assert found.status() == "up: unknown"  # handshake, no identity

    server = FakeMcpServer(replies=[MEASURED], shape="modern")
    with discovery(server) as found:
        found.search("custo")
        assert found.status() == "up: datahub v3.4.5"  # handshake, identified


def test_the_identity_does_not_disturb_the_search_it_travels_with() -> None:
    """Whatever the handshake said, the tool call and its parse are byte-for-byte the same."""
    results = {}
    for shape in ("modern", "legacy", "missing"):
        server = FakeMcpServer(replies=[MEASURED], shape=shape)
        with discovery(server) as found:
            got = found.search("custo")
        results[shape] = (
            [h.urn for h in got.hits],
            [h.name for h in got.hits],
            got.total,
            got.note,
            got.advisory,
            server.calls[0],
        )
    assert results["modern"] == results["legacy"] == results["missing"]


def _legacy_only_identity(init: object) -> str:
    """The implementation as it stood before Session 33. Kept HERE so it can be falsified."""
    info = getattr(init, "serverInfo", None)
    return f"{info.name} v{info.version}" if info is not None else ""


def test_the_legacy_only_reader_goes_blind_on_the_modern_shape() -> None:
    """THE VACUITY CHECK, in the suite rather than in a command someone has to remember.

    Same precedent as `test_the_naive_parse_...` above and
    `test_breaking_a_checker_collapses_the_benchmark`. It proves three things at once: the
    two shapes are genuinely different objects, the old reader cannot see the modern one,
    and it collapses to exactly the `""` that made the regression invisible. Without this,
    the tests above would pass just as well against a reader that had never been fixed.
    """
    from types import SimpleNamespace

    modern = SimpleNamespace(server_info=SimpleNamespace(name="datahub", version="3.4.5"))
    legacy = SimpleNamespace(serverInfo=SimpleNamespace(name="datahub", version="3.4.5"))

    # The old reader: right about the shape it was written for, blind to the other.
    assert _legacy_only_identity(legacy) == "datahub v3.4.5"
    assert _legacy_only_identity(modern) == "", "this is the regression, reproduced"

    # The shipped reader: both, and never the silent empty string.
    assert server_identity(modern) == "datahub v3.4.5"
    assert server_identity(legacy) == "datahub v3.4.5"
    assert server_identity(SimpleNamespace()) == UNKNOWN_SERVER


# --- the same rename, at the error flag (Session 33) --------------------------


@pytest.mark.parametrize("shape", ["modern", "legacy"], ids=["mcp>=2", "mcp<2"])
def test_a_refused_search_is_seen_under_either_error_field_name(shape: str) -> None:
    """`CallToolResult.isError` -> `is_error` in the same release as the handshake rename.

    This site was the dangerous one. Reading only the old name answered False for EVERY
    response under the new client, so the branch that turns a refused search into
    `DiscoveryUnavailable` was dead — and an error stopped being recognized as an error,
    which is this project's cardinal sin at the third transport (§22).
    """
    server = FakeMcpServer(
        replies=[
            FakeToolResult(
                "Error calling tool 'search': Expected =", is_error=True, shape=shape
            )
        ]
    )
    with discovery(server) as found, pytest.raises(DiscoveryUnavailable) as caught:
        found.search("custo")
    # The DIAGNOSIS, not merely the exception type: falling through to the JSON branch also
    # raises DiscoveryUnavailable, and would have looked like this test passing.
    assert "refused the search" in str(caught.value)
    assert "Expected =" in str(caught.value)


def test_the_legacy_only_error_reader_cannot_see_a_modern_refusal() -> None:
    """THE VACUITY CHECK for the error flag.

    Without this, the test above passes just as well against the old reader — because a
    refused search still fails, only via the JSON branch and with the wrong diagnosis.
    Absence of a crash is not the guard working, so the two routes are told apart here.
    """
    modern = FakeToolResult("Error calling tool 'search': Expected =", is_error=True)
    legacy = FakeToolResult(
        "Error calling tool 'search': Expected =", is_error=True, shape="legacy"
    )

    def _legacy_only(result: object) -> bool:
        """The implementation as it stood before Session 33."""
        return bool(getattr(result, "isError", False))

    assert _legacy_only(legacy) is True
    assert _legacy_only(modern) is False, "this is the regression, reproduced"

    assert tool_reported_error(modern) is True
    assert tool_reported_error(legacy) is True
    # A successful call is still not an error under either name.
    assert tool_reported_error(FakeToolResult("{}")) is False
    assert tool_reported_error(FakeToolResult("{}", shape="legacy")) is False
