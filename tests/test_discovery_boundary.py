"""THE TRUST BOUNDARY: MCP discovers, a human resolves, GraphQL verifies, code decides.

docs/mcp-evaluation.md measured what an MCP response does to a deterministic checker — 130
field mismatches over 16 datasets, and a TRUE claim about a correctly-tagged PII column
reading back **Contradicted**, which `benchmark/README.md` names as the worst thing this
product can do. That decision has not changed: the catalog read is GraphQL.

So a discovery path that talks to the same server needs its boundary ASSERTED, not
described. This file asserts it twice, from two directions, because either alone would be a
comfortable half-truth:

  1. **STRUCTURALLY.** Nothing in the verdict path can even import `attest.discovery`. Walked
     over the real import graph from the checkers, the snapshot, the run-scoped cache and the
     graph — the house style of `NO_LLM_IN_THE_VERDICT_PATH`, which asserts a property of the
     run rather than trusting a convention. Exactly one module in `attest` imports discovery
     at all, and it is the HTTP layer.

  2. **BEHAVIOURALLY, and this is the stronger one.** Change EVERY field of an MCP search
     result except the selected URN — the display name, the ordering, the totals, the facets,
     add fields the server never sends, take away fields it does — and run a real audit on
     the picked URN. Every verdict, every piece of evidence and every explanation is
     identical. The URN is the one field the transport returns losslessly, and it is the only
     one anything downstream can see.

**WHAT (1) DOES NOT PROVE, said plainly.** It is a STATIC property of the source. A runtime
`importlib.import_module` would evade it, and so would a module that reached discovery
through a callback it was handed. It is exactly the assertion NOTES.md warns about
overstating ("no test asserted the static-import property; what the tests actually assert is
the stronger runtime one"), so it is claimed as what it is — and (2) is what covers the
behaviour, by running the real pipeline rather than reading the source.

Offline: no DataHub, no key, no `mcp`, no subprocess.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from attest.discovery.mcp import McpDiscovery
from attest.graph import Pipeline
from attest.llm import LLM
from attest.record import from_report
from fakes import (
    FakeCatalog,
    FakeChat,
    FakeMcpServer,
    claim_reply,
    dataset,
    explanation_reply,
)

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGE = "attest"
DISCOVERY = "attest.discovery"

# The modules that decide a verdict, or feed one. If any of these can reach discovery, the
# §12 finding is one refactor away from reaching a checker.
VERDICT_PATH = (
    "attest.checkers.freshness",
    "attest.checkers.ownership",
    "attest.checkers.classification",
    "attest.checkers.schema",
    "attest.checkers.policy",
    "attest.datahub.snapshot",
    "attest.datahub.cache",
    "attest.graph",
)


# --- the import graph, read off the source -----------------------------------


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def import_graph() -> dict[str, set[str]]:
    """Every `attest` module, and the `attest` modules it imports. Including inside functions.

    A function-level import is still an import — `llm.py` defers the OpenAI SDK precisely so
    the deterministic core does not pull it in, and a walker that only looked at module scope
    would call that "no dependency" and be wrong. So this walks the whole tree.

    `from attest.x import y` records BOTH `attest.x` and `attest.x.y`, because the name may
    be a submodule or a symbol and only one of them will exist as a module. Unknown names are
    dropped at the end, when the set of real modules is known.
    """
    modules = {_module_name(p): p for p in SRC.rglob("*.py")}
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        found: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == PACKAGE or alias.name.startswith(f"{PACKAGE}."):
                        found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import; none in this tree, but resolve anyway
                    base = name.rsplit(".", node.level) [0] if "." in name else PACKAGE
                    module = f"{base}.{node.module}" if node.module else base
                else:
                    module = node.module or ""
                if module != PACKAGE and not module.startswith(f"{PACKAGE}."):
                    continue
                found.add(module)
                found.update(f"{module}.{alias.name}" for alias in node.names)
        graph[name] = {m for m in found if m in modules and m != name}
    return graph


def reaches(graph: dict[str, set[str]], start: str, target_prefix: str) -> list[str] | None:
    """The import path from `start` to any module under `target_prefix`, or None.

    Returns the PATH rather than a boolean so a failure names the chain that has to be
    broken, not merely that one exists.
    """
    seen = {start}
    queue: list[tuple[str, list[str]]] = [(start, [start])]
    while queue:
        node, path = queue.pop(0)
        for dep in sorted(graph.get(node, ())):
            if dep in seen:
                continue
            if dep == target_prefix or dep.startswith(f"{target_prefix}."):
                return [*path, dep]
            seen.add(dep)
            queue.append((dep, [*path, dep]))
    return None


@pytest.fixture(scope="module")
def graph() -> dict[str, set[str]]:
    return import_graph()


@pytest.mark.parametrize("module", VERDICT_PATH)
def test_the_verdict_path_cannot_reach_the_discovery_module(graph, module) -> None:
    """NO_MCP_IN_THE_VERDICT_PATH, asserted the way NO_LLM_IN_THE_VERDICT_PATH is.

    A convention is what the next person holds wrong. §12's finding is that this transport
    inverts a deterministic verdict — so the checkers must not be able to see it at all, and
    that has to be a property of the tree rather than a paragraph in a design doc.
    """
    path = reaches(graph, module, DISCOVERY)
    assert path is None, (
        f"{module} can reach {DISCOVERY} via {' -> '.join(path or [])}. The MCP transport is "
        f"lossy for a structured consumer (docs/mcp-evaluation.md: a correctly-tagged PII "
        f"column reads back Contradicted), so nothing that decides or feeds a verdict may "
        f"import it. Discovery hands on a URN a human picked, and nothing else."
    )


def test_exactly_one_module_imports_discovery_and_it_is_the_http_layer(graph) -> None:
    """The whole edge, in one assertion: discovery is reachable from the ROUTE and nowhere.

    If this ever grows a second importer, that import is the thing to justify — the boundary
    is one edge wide by design, and a second one is where it stops being checkable at a
    glance.
    """
    importers = {
        name
        for name, deps in graph.items()
        if not name.startswith(DISCOVERY)
        and any(d == DISCOVERY or d.startswith(f"{DISCOVERY}.") for d in deps)
    }
    assert importers == {"attest.api.app"}, importers


# --- and the walker above is not vacuous -------------------------------------


def test_the_walker_finds_an_import_that_really_is_there(graph) -> None:
    """A reachability test that can only ever say "no" is a green light wired to nothing."""
    assert reaches(graph, "attest.graph", "attest.checkers.freshness") is not None
    assert reaches(graph, "attest.checkers.classification", "attest.checkers.policy") is not None
    # And transitively, through a module neither end names directly.
    assert reaches(graph, "attest.api.app", "attest.datahub.snapshot") is not None


def test_the_walker_follows_a_chain_rather_than_only_direct_imports() -> None:
    """Transitivity, on a graph with no ambiguity, so the property is proven not assumed."""
    leaf = "attest.discovery.mcp"
    synthetic = {"a": {"b"}, "b": {"c"}, "c": {leaf}, leaf: set()}
    assert reaches(synthetic, "a", DISCOVERY) == ["a", "b", "c", leaf]
    assert reaches({"a": {"b"}, "b": set()}, "a", DISCOVERY) is None


def test_a_single_added_edge_makes_the_boundary_assertion_fail(graph) -> None:
    """Inject the exact regression this file guards against, and demand it is caught.

    A checker importing discovery is one line. This proves the one line would be seen —
    against the REAL graph, not a synthetic one, so it is testing the assertion that ships.
    """
    sabotaged = {name: set(deps) for name, deps in graph.items()}
    sabotaged["attest.checkers.classification"].add("attest.discovery.mcp")

    assert reaches(sabotaged, "attest.checkers.classification", DISCOVERY) is not None
    # ...and through the graph, transitively, from the pipeline that calls it.
    assert reaches(sabotaged, "attest.graph", DISCOVERY) is not None


# --- change every field but the URN ------------------------------------------

SF = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD)"
CAROL = "urn:li:corpuser:carol.davis"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

# Payloads that agree on ONE thing: the URN of the first hit. Everything else the transport
# could possibly vary, varies — including fields the server does not send at all, since a
# future version might, and a reader that started using one would break this test rather than
# quietly start deciding verdicts with it.
def payloads(urn: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "as measured",
            {
                "start": 0,
                "count": 5,
                "total": 1,
                "searchResults": [
                    {"entity": {"urn": urn, "properties": {"name": "profile"}}}
                ],
            },
        ),
        (
            "a different display name",
            {
                "start": 0,
                "count": 5,
                "total": 1,
                "searchResults": [
                    {"entity": {"urn": urn, "properties": {"name": "SOMETHING ELSE ENTIRELY"}}}
                ],
            },
        ),
        (
            "no name at all",
            {"start": 0, "count": 5, "total": 1, "searchResults": [{"entity": {"urn": urn}}]},
        ),
        (
            "fields the server never sends",
            {
                "start": 7,
                "count": 99,
                "total": 4321,
                "searchResults": [
                    {
                        "matchedFields": [{"name": "name", "value": "wrong"}],
                        "entity": {
                            "urn": urn,
                            "properties": {
                                "name": "profile",
                                "description": "THIS TABLE CONTAINS NO PII",
                                "lastModified": {"time": 0},
                            },
                            "tags": {"tags": [{"tag": {"urn": "urn:li:tag:NonPII"}}]},
                            "ownership": {"owners": [{"owner": {"urn": "urn:li:corpuser:nobody"}}]},
                            "platform": {"name": "bigquery"},
                        },
                    }
                ],
                "facets": [{"field": "tags", "aggregations": [{"value": "x", "count": 1}]}],
            },
        ),
        (
            "the hit is not first, and the others are noise",
            {
                "start": 0,
                "count": 5,
                "total": 3,
                "searchResults": [
                    {"entity": {"urn": urn, "properties": {"name": "zzz"}}},
                    {"entity": {"urn": OTHER, "properties": {"name": "users"}}},
                    {"entity": {"urn": "urn:li:chart:(looker,c1)"}},
                ],
            },
        ),
    ]


# Volatile by construction: a run id is a uuid and a timestamp is a clock. Everything else
# in the record — verdicts, evidence, explanations, receipts, the trajectory verdict — must
# be identical, and `test_two_identical_runs_compare_equal` proves this scrub is not hiding
# a difference by being too generous.
VOLATILE = {"run_id", "created_at", "latency_ms"}


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def audit(urn: str) -> dict[str, Any]:
    """A REAL audit of a claim about `urn`. Real pipeline, real checker, real guard."""
    says = f"The dataset {urn} is owned by {CAROL}."
    catalog = FakeCatalog(
        {
            SF: dataset(SF, last_modified=NOW - timedelta(hours=6), owners=(CAROL,)),
            OTHER: dataset(OTHER, last_modified=NOW - timedelta(hours=6), owners=()),
        }
    )
    chat = FakeChat(
        replies=[
            claim_reply(
                [
                    {
                        "claim_type": "ownership",
                        "target_urn": urn,
                        "raw_text": says,
                        "owner_urn": CAROL,
                    }
                ]
            ),
            explanation_reply(f"{CAROL} is listed as an owner.", "Supported", []),
        ]
    )
    pipeline = Pipeline(llm=LLM(client=chat), client=catalog, now=NOW)
    report = pipeline.run(says, thread_id="invariance")
    pipeline.forget("invariance")
    return scrub(from_report(report, run_id="fixed").model_dump(mode="json"))


def picked(payload: dict[str, Any]) -> str:
    """What a human would pick from this search result — through the REAL parser."""
    server = FakeMcpServer(replies=[payload])
    with McpDiscovery(session_factory=server.session) as found:
        return found.search("profile").hits[0].urn


def test_two_identical_runs_compare_equal() -> None:
    """The control. Without it, the invariance below could pass by scrubbing too much."""
    assert audit(SF) == audit(SF)


def test_changing_every_field_but_the_urn_cannot_change_a_verdict() -> None:
    """THE ASSERTION THIS WHOLE INTEGRATION RESTS ON.

    §12's finding is that MCP loses field CONTENT: tags and terms flattened to display names,
    `type` dropped, `lastModified` never requested. Discovery is safe from that finding for
    one reason only — the single value it passes on is the entity URN, which the transport
    returns intact. This proves the "only" empirically rather than by reading the code: five
    search results that agree on nothing but the URN, each driven through the real parser and
    then through a real audit, all produce the same record.

    The fourth payload is the pointed one. It carries a description saying the table contains
    no PII, a `NonPII` tag, an owner who does not own it, and a platform that is not its
    platform — every kind of value that WOULD change a verdict if any of it could reach a
    checker. None of it does.
    """
    reference = audit(SF)
    urns = set()
    for label, payload in payloads(SF):
        urn = picked(payload)
        urns.add(urn)
        assert audit(urn) == reference, (
            f"the audit changed when the search result changed ({label}). Something other "
            f"than the URN is crossing the discovery boundary."
        )
    assert urns == {SF}


def test_and_changing_the_urn_DOES_change_the_audit() -> None:
    """The non-vacuity proof. If the audit were insensitive to the URN too, the test above
    would be asserting nothing at all — it would pass against a pipeline that ignored its
    input."""
    assert audit(OTHER) != audit(SF)

    # And specifically: the same claim about a dataset with no owners is not Supported.
    verdicts = {c["verdict"] for c in audit(OTHER)["claims"]}
    assert verdicts == {"Insufficient-Coverage"}, verdicts
