"""A scripted stand-in for OpenAI's chat client.

The semantic layer is tested offline, and not because a key is inconvenient. The things
worth testing here are the model's *worst* behaviours — a hallucinated owner, an invented
column, a verdict it was told not to reach — and a real model produces those only by
luck. A fake produces them on demand, which is the only way to prove the guard catches
them. The suite spends no tokens and cannot flake.

What the fake does NOT do is stub out the code under test. Every reply goes through the
real llm.LLM, the real schema handling, the real faithfulness guard, and real CheckResults
built from the live catalog.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import httpx
import openai

from attest.datahub import (
    CatalogUnavailable,
    DataHubError,
    DatasetSnapshot,
    EntityNotFoundError,
)


@dataclass
class FakeChat:
    """Replays canned completions and records exactly what it was asked.

    `replies` are consumed in order; the last one repeats if the caller asks again, so a
    test that only cares about one reply does not have to pad the list.

    `tokens` is off by default and the default is meaningful: a client that reports no usage
    leaves Usage at zero and cost_usd at None, which is honest — nothing was spent because
    nothing was called. Set it to make the fake BILL, which is what the tests about billing
    (test_concurrency.py) and about unpriced models (test_resume.py) need. A receipt cannot
    be tested against a client that never charges for anything.

    **`faults` is how this fake can fail the way the REAL client fails, and its absence is
    why a transport bug shipped past 23 green sessions.** A fake is a Python object: it opens
    no socket and negotiates no TLS, so nothing it does can raise an `openai.APIStatusError`
    on its own — the entire transport failure class is invisible to it BY CONSTRUCTION
    (CLAUDE.md, Session 5). The DataHub fake below has had fault injection on nearly every
    method since Session 4; this one had none, and that asymmetry is exactly the cover an
    OpenAI 500 walked through. `faults` maps a 1-BASED call number to the exception that
    call raises instead of replying, so a test can put an outage on the decompose call and
    not on the explain call — which is the difference between a run that dies and a run that
    degrades. The call is RECORDED before it raises: a test asserting "Attest did not retry
    this" is asserting a count, and a fault that never reached `calls` would make every such
    assertion pass vacuously.
    """

    replies: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: tuple[int, int] | None = None
    faults: dict[int, Exception] = field(default_factory=dict)

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        temperature: float,
    ) -> Any:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "temperature": temperature,
            }
        )
        fault = self.faults.get(len(self.calls))
        if fault is not None:
            raise fault
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        content = self.replies[index]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=self._usage(),
        )

    def _usage(self) -> Any:
        if self.tokens is None:
            return None
        prompt, completion = self.tokens
        return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)

    @property
    def prompts(self) -> list[str]:
        """Every message body sent, flattened — for asserting what the model could see."""
        return [m["content"] for call in self.calls for m in call["messages"]]


# --- the provider, failing ---------------------------------------------------
#
# REAL openai SDK exception objects, constructed the way the SDK constructs them — with an
# httpx response carrying the status code, because that is where `status_code` is read from
# and a hand-rolled stand-in with the attribute bolted on would prove the classification
# works against a shape the SDK does not produce. The point of these is to reach code that
# the fake chat client can otherwise never reach.

_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")

_BY_STATUS: dict[int, type[openai.APIStatusError]] = {
    400: openai.BadRequestError,
    401: openai.AuthenticationError,
    403: openai.PermissionDeniedError,
    404: openai.NotFoundError,
    422: openai.UnprocessableEntityError,
    429: openai.RateLimitError,
    500: openai.InternalServerError,
}


def openai_status_error(status: int, message: str = "") -> openai.APIStatusError:
    """The SDK's own exception for an HTTP status. 500 is the one that started this."""
    cls = _BY_STATUS.get(status, openai.APIStatusError)
    return cls(
        message or f"Error code: {status}",
        response=httpx.Response(status, request=_REQUEST),
        body=None,
    )


def openai_connection_error() -> openai.APIConnectionError:
    """What the SDK raises when the socket never produced a response."""
    return openai.APIConnectionError(request=_REQUEST)


def openai_timeout() -> openai.APITimeoutError:
    """A read timeout. A subclass of APIConnectionError, which the classifier must honour."""
    return openai.APITimeoutError(request=_REQUEST)


def reply(payload: dict[str, Any]) -> str:
    """A well-formed JSON completion."""
    return json.dumps(payload)


def explanation_reply(
    text: str,
    implied_verdict: str,
    cited_fields: list[str] | None = None,
) -> str:
    return reply(
        {
            "explanation": text,
            "implied_verdict": implied_verdict,
            "cited_fields": cited_fields if cited_fields is not None else [],
        }
    )


def claim_reply(claims: list[dict[str, Any]]) -> str:
    """A decomposition completion. Fills the flat schema's nulls so callers state only
    the fields their claim type actually owns — exactly what a real model must do."""
    blank = {
        "max_age_hours": None,
        "owner_urn": None,
        "labels": None,
        "present": None,
        "field_path": None,
        "columns": None,
    }
    return reply({"claims": [{**blank, **c} for c in claims]})


def revision_reply(claim: dict[str, Any], unchanged: bool = False) -> str:
    """A self-correction completion. Same flat shape, plus the stand-firm flag."""
    blank = {
        "max_age_hours": None,
        "owner_urn": None,
        "labels": None,
        "present": None,
        "field_path": None,
        "columns": None,
        "unchanged": unchanged,
    }
    return reply({**blank, **claim})


# --- the catalog, faked ------------------------------------------------------
#
# The rest of the suite reads the LIVE seeded catalog on purpose: the checkers have to
# agree with a real DataHub server, and a fixture would happily agree with a query that
# does not exist. That argument does not extend to the GRAPH tests, and pretending it did
# would be cargo-culting it. What they exercise is routing, the retry cap, the human
# checkpoint, and the trajectory invariants — control flow, none of which is a statement
# about DataHub's wire format. The checkers they call are the real ones, already pinned to
# the live catalog by test_coverage.py, so nothing here is trusted that is not tested
# elsewhere. In exchange the graph tests run with no server, no key, and no flakiness.


class FakeCatalog:
    """A DataHubClient stand-in. Serves snapshots by URN; raises on anything else.

    `read_faults` maps a 1-BASED fetch index to the exception that fetch raises, so a test
    can say "the catalog goes down on the second claim" without a live server. It exists for
    the same reason `FakeChat.faults` does, and it is the same asymmetry landing a second
    time: the write side of this fake has had fault injection since Session 4, while the READ
    side — the one that feeds every verdict — had none, so no offline test could distinguish
    an unreachable catalog from a URN that does not exist. That is what let a transport
    failure be filed as a malformed question for twenty-odd green sessions.

    The fetch is RECORDED before it raises. An assertion that Attest did not retry a dead
    catalog is a count, and a fault that never reached `fetched` would pass vacuously.
    """

    def __init__(
        self,
        snapshots: dict[str, DatasetSnapshot],
        read_faults: dict[int, Exception] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.fetched: list[str] = []
        self.read_faults = dict(read_faults or {})

    def fetch_dataset(self, urn: str) -> DatasetSnapshot:
        self.fetched.append(urn)
        fault = self.read_faults.get(len(self.fetched))
        if fault is not None:
            raise fault
        if urn not in self.snapshots:
            raise EntityNotFoundError(urn)
        return self.snapshots[urn]

    def close(self) -> None:  # pragma: no cover — the Pipeline never closes what it is given
        pass


class FakeDataHub(FakeCatalog):
    """A catalog that can also be WRITTEN to, and remembers what was written.

    The read side is FakeCatalog. The write side records structured-property upserts
    instead of performing them, so a test can assert exactly what would have reached the
    real catalog — which for the write-back path is the whole question. `fail` makes the
    server refuse, because an approval whose catalog write failed must be reported as
    failed rather than as a quiet success.

    `fail` raises the class the REAL client would raise for each shape: `CatalogUnavailable`
    where the message says the server could not be reached, and a plain `DataHubError` where
    the server answered and refused the write. A fake that raised one class for both would
    make the distinction the read path now rests on untestable here.
    """

    def __init__(
        self,
        snapshots: dict[str, DatasetSnapshot],
        fail: bool = False,
        read_faults: dict[int, Exception] | None = None,
    ) -> None:
        super().__init__(snapshots, read_faults=read_faults)
        self.fail = fail
        self.defined: list[str] = []
        # property urn -> its definition, so a test can see what the CATALOG says about a
        # property rather than what the code says. Those drifted apart for real.
        self.definitions: dict[str, dict[str, Any]] = {}
        self.updated: list[str] = []
        # asset urn -> {property urn: values}. Last write wins, exactly like DataHub.
        self.written: dict[str, dict[str, list[Any]]] = {}
        # Claim artifacts. `assertions` is keyed by the CALLER's urn and `run_events` by
        # (urn, timestampMillis) — the two key choices the retry guarantee rests on.
        self.assertions: dict[str, dict[str, Any]] = {}
        self.run_events: dict[str, dict[int, dict[str, Any]]] = {}
        self.upserts: list[dict[str, Any]] = []
        self.tags_defined: set[str] = set()
        self.tagged: dict[str, set[str]] = {}
        # WHAT WAS ASKED OF THE SERVER, verbatim. The retrieval path REPORTS which
        # predicates it pushed down to DataHub, and a report nobody checks is a claim
        # nobody checks -- so these two logs are what `test_retrieval` holds that report
        # to. A predicate reported as pushed down must be findable in here; one reported
        # as filtered locally must NOT be.
        self.dataset_reads: list[dict[str, Any]] = []
        self.searches: list[dict[str, Any]] = []

    def execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        return {"appConfig": {"appVersion": "v1.5.0.6"}}

    def get_structured_property(self, urn: str) -> dict[str, Any] | None:
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        if urn not in self.defined:
            return None
        # The DEFINITION comes back, description and all — because that is what the real
        # read returns, and because a fake that omitted it would make a stale description
        # unreachable in tests. Which is exactly how one survived in the live catalog
        # saying "test" for several sessions.
        return {"urn": urn, "definition": self.definitions.get(urn, {})}

    def create_structured_property(
        self, qualified_name: str, display_name: str = "", description: str = "", **kwargs: Any
    ) -> dict[str, Any]:
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        urn = f"urn:li:structuredProperty:{qualified_name}"
        self.defined.append(urn)
        self.definitions[urn] = {
            "qualifiedName": qualified_name,
            "displayName": display_name,
            "description": description,
        }
        return {"urn": urn}

    def update_structured_property(
        self, urn: str, display_name: str = "", description: str = ""
    ) -> dict[str, Any]:
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        if urn not in self.defined:
            # createStructuredProperty's mirror image: the real mutation has nothing to
            # update, and a fake that invented the property would hide the ordering bug.
            raise DataHubError(f"GraphQL errors: {urn} is not defined")
        self.definitions.setdefault(urn, {}).update(
            {k: v for k, v in
             (("displayName", display_name), ("description", description)) if v}
        )
        self.updated.append(urn)
        return {"urn": urn}

    def set_structured_properties(
        self, asset_urn: str, properties: dict[str, list[Any]]
    ) -> dict[str, Any]:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        self.written.setdefault(asset_urn, {}).update(properties)
        return {"properties": []}

    # --- claim artifacts ---------------------------------------------------
    #
    # Modelled on what the REAL server was measured to do, not on what would be convenient:
    #
    #   * an assertion is keyed by the URN THE CALLER supplies, so a second write at the
    #     same URN updates rather than appends — which is what makes a retry safe;
    #   * a run event is keyed by (assertion urn, timestampMillis), because DataHub's
    #     timeseries aspect is, so re-reporting one verdict at one timestamp collapses to a
    #     single row. A fake that appended blindly would pass the duplicate-history test
    #     while the real server failed it, and vice versa.
    #
    # It cannot model the index lag or the fieldPath trap — those are server machinery a
    # fake does not have, which is the Session 5 rule and why `just live` is the evidence
    # for this feature.

    def upsert_custom_assertion(
        self,
        urn: str,
        entity_urn: str,
        custom_type: str,
        description: str,
        platform_urn: str,
        logic: str,
        external_url: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        # Record the call verbatim, so a test can assert what WOULD have reached the server —
        # notably that `fieldPath` is never among it.
        self.upserts.append(
            {
                "urn": urn,
                "entityUrn": entity_urn,
                "type": custom_type,
                "description": description,
                "logic": logic,
                "externalUrl": external_url,
                **extra,
            }
        )
        self.assertions[urn] = {
            "urn": urn,
            "info": {
                "description": description,
                "externalUrl": external_url,
                "customAssertion": {
                    "type": custom_type,
                    "entityUrn": entity_urn,
                    "logic": logic,
                    "field": None,
                },
            },
            "tags": {"tags": []},
        }
        return self.assertions[urn]

    def report_assertion_result(
        self,
        urn: str,
        result_type: str,
        timestamp_millis: int,
        properties: dict[str, str] | None = None,
        external_url: str = "",
        retry_seconds: float = 30.0,
    ) -> bool:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        if urn not in self.assertions:
            raise DataHubError(
                f"Failed to report Assertion Run Event. Assertion with urn {urn} does not "
                f"exist or is not associated with any entity."
            )
        # Keyed by timestamp, exactly like the timeseries aspect. The SAME verdict reported
        # twice at the same timestamp is ONE event, not two.
        self.run_events.setdefault(urn, {})[timestamp_millis] = {
            "timestampMillis": timestamp_millis,
            "result": {
                "type": result_type,
                "externalUrl": external_url,
                "nativeResults": [
                    {"key": k, "value": v} for k, v in (properties or {}).items()
                ],
            },
        }
        return True

    def get_assertion(self, urn: str) -> dict[str, Any] | None:
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        node = self.assertions.get(urn)
        if node is None:
            return None
        return self._with_events(node)

    def list_dataset_assertions(
        self, dataset_urn: str, limit: int | None = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """Returns (nodes-up-to-limit, TOTAL) — the same contract the real client now has.

        The `total` is the catalog's own count, round-tripped so a truncated page is never
        read as complete (Session 21 gap 1). The real client PAGINATES to reach it; the fake
        has every assertion in memory and slices directly — modelling the CONTRACT (a total
        beside the nodes), not the loop, which is server machinery a fake does not have and
        `just live` is the evidence for.
        """
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        self.dataset_reads.append({"dataset_urn": dataset_urn, "limit": limit})
        # SCOPES, AND FILTERS NOTHING — modelled on the real entry point rather than made
        # convenient. A fake that quietly accepted a verdict filter here would let the
        # push-down report claim a server-side filter this server does not have.
        matching = [
            self._with_events(node)
            for node in self.assertions.values()
            if (node["info"]["customAssertion"]["entityUrn"] == dataset_urn)
        ]
        nodes = matching if limit is None else matching[:limit]
        return nodes, len(matching)

    def search_assertions(
        self,
        custom_types: Sequence[str] = (),
        tags: Sequence[str] = (),
        limit: int | None = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """FILTERS, AND SCOPES TO NOTHING. The mirror image of the read above.

        The two entry points are disjoint on the real server (measured: no assertee field
        is indexed, so search cannot restrict to a dataset), and they are disjoint here for
        the same reason — a fake that let one do both would make the retrieval path's whole
        honesty argument untestable. Returns (nodes-up-to-limit, total), same as above.
        """
        if self.fail:
            raise CatalogUnavailable("the catalog at http://fake could not be reached")
        self.searches.append(
            {"custom_types": list(custom_types), "tags": list(tags), "limit": limit}
        )
        found = []
        for node in self.assertions.values():
            if custom_types and node["info"]["customAssertion"]["type"] not in custom_types:
                continue
            # The tag filter is over the tags the artifact CARRIES, which is what makes a
            # stale tag miss here and be found by a dataset read — the asymmetry the
            # retrieval note warns about.
            if tags and not (self.tagged.get(node["urn"], set()) & set(tags)):
                continue
            found.append(self._with_events(node))
        nodes = found if limit is None else found[:limit]
        return nodes, len(found)

    def _with_events(self, node: dict[str, Any]) -> dict[str, Any]:
        events = sorted(
            self.run_events.get(node["urn"], {}).values(),
            key=lambda e: e["timestampMillis"],
            reverse=True,
        )
        return {
            **node,
            "runEvents": {
                "total": len(events),
                "succeeded": sum(1 for e in events if e["result"]["type"] == "SUCCESS"),
                "failed": sum(1 for e in events if e["result"]["type"] == "FAILURE"),
                "runEvents": events,
            },
        }

    def create_tag(self, tag_id: str, name: str, description: str = "") -> str:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        urn = f"urn:li:tag:{tag_id}"
        self.tags_defined.add(urn)
        return urn

    def add_tag(self, tag_urn: str, resource_urn: str) -> bool:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        self.tagged.setdefault(resource_urn, set()).add(tag_urn)
        node = self.assertions.get(resource_urn)
        if node is not None:
            node["tags"] = {
                "tags": [{"tag": {"urn": u}} for u in sorted(self.tagged[resource_urn])]
            }
        return True

    def remove_tag(self, tag_urn: str, resource_urn: str) -> bool:
        if self.fail:
            raise DataHubError("GraphQL errors: could not write")
        self.tagged.setdefault(resource_urn, set()).discard(tag_urn)
        node = self.assertions.get(resource_urn)
        if node is not None:
            node["tags"] = {
                "tags": [{"tag": {"urn": u}} for u in sorted(self.tagged[resource_urn])]
            }
        return True


# --- the MCP server, faked ---------------------------------------------------
#
# FAULT INJECTION FROM DAY ONE, and the reason is written above twice already. `FakeChat`
# shipped without it and an OpenAI 500 walked past 23 green sessions (§18); `FakeCatalog`'s
# READ side shipped without it and an unreachable catalog was filed as a malformed question
# for twenty-odd more (§20). Both times the fake could not fail the way the real thing fails,
# so no offline test could have caught it. A third transport arriving without fault injection
# would be the same lesson going unlearned in the same file.
#
# What this fake CANNOT do, stated so nobody reads more into it: it opens no pipe, spawns no
# subprocess and speaks no JSON-RPC, so it cannot reproduce a child that ignores SIGTERM, a
# half-written frame, or a uvx resolve that hangs. That is the Session 5 rule, and it is why
# `tests/test_discovery_live.py` and the orphan measurement in docs/deployment.md exist.


class FakeToolResult:
    """What `ClientSession.call_tool` hands back: text content, and an error flag.

    An error flag with a text body is how the REAL server reports a bad filter or an unknown
    tool — measured, not guessed: it does not raise. A fake that raised instead would leave
    the branch that turns it into `DiscoveryUnavailable` untested.

    `shape` names the FIELD, and it defaults to `modern` for the same reason
    `FakeMcpServer.shape` does: `mcp` 2.0.0 renamed `CallToolResult.isError` to `is_error`,
    and a fake that keeps setting the old attribute keeps a dead product branch looking
    tested. That is how this one survived — the offline test asserting a refused search
    becomes `DiscoveryUnavailable` passed throughout, against a reader that could no longer
    see the flag at all.
    """

    def __init__(
        self, text: str = "", is_error: bool = False, shape: str = "modern"
    ) -> None:
        self.content = [SimpleNamespace(text=text)]
        if shape == "modern":
            self.is_error = is_error
        elif shape == "legacy":
            self.isError = is_error
        else:
            raise ValueError(f"unknown CallToolResult shape: {shape!r}")


class FakeMcpServer:
    """A scripted MCP session, and the transport failures a real one has.

    `replies` are consumed in order and the last repeats, exactly like `FakeChat`. Each is a
    dict (serialized as the JSON body the server sends), a raw string (for a body that is not
    JSON at all), or a `FakeToolResult` (for `isError`).

    `faults` maps a 1-BASED call number to the exception that call raises; `start_faults`
    maps a 1-based SESSION number to an exception raised while starting, which is how a
    server that will not launch is reproduced without a subprocess. Calls and starts are both
    RECORDED BEFORE they raise: "Attest did not retry this" is an assertion about a count,
    and a fault that never reached the log would make it pass vacuously.

    `starts` is what proves the self-healing claim — a failed session must be REBUILT on the
    next search rather than cached as a permanent outage.

    `shape` is the `initialize()` result's SHAPE, and it defaults to `modern` for a reason
    that cost a live regression. `mcp` 2.0.0 renamed `InitializeResult.serverInfo` to
    `server_info`; this fake emitted the LEGACY name only, so the offline tier stayed green
    while every real session came back unidentified. A fake that keeps sending a shape the
    real library stopped sending is Session 5's rule at the handshake — so the default is
    what the installed client sends, and the older name is a shape you must ask for:

        modern       server_info=...       mcp >= 2
        legacy       serverInfo=...        mcp < 2
        missing      neither field         a server that says nothing about itself
        empty        the field is None
        nameless / versionless             present, but half a name
    """

    def __init__(
        self,
        replies: Sequence[Any] = (),
        faults: dict[int, Exception] | None = None,
        start_faults: dict[int, Exception] | None = None,
        server: tuple[str, str] = ("datahub", "3.4.5"),
        delay: float = 0.0,
        shape: str = "modern",
    ) -> None:
        self.replies = list(replies)
        self.faults = dict(faults or {})
        self.start_faults = dict(start_faults or {})
        self.server = server
        self.delay = delay
        self.shape = shape
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.starts = 0
        self.stops = 0

    @asynccontextmanager
    async def session(self) -> AsyncIterator[FakeMcpServer]:
        """The `session_factory` seam. Entered and exited by ONE task, like the real one."""
        self.starts += 1
        fault = self.start_faults.get(self.starts)
        if fault is not None:
            raise fault
        try:
            yield self
        finally:
            self.stops += 1

    async def initialize(self) -> Any:
        name, version = self.server
        info = SimpleNamespace(name=name, version=version)
        if self.shape == "modern":
            return SimpleNamespace(server_info=info)
        if self.shape == "legacy":
            return SimpleNamespace(serverInfo=info)
        if self.shape == "empty":
            return SimpleNamespace(server_info=None)
        if self.shape == "nameless":
            return SimpleNamespace(server_info=SimpleNamespace(version=version))
        if self.shape == "versionless":
            return SimpleNamespace(server_info=SimpleNamespace(name=name))
        if self.shape == "missing":
            return SimpleNamespace()
        raise ValueError(f"unknown initialize() shape: {self.shape!r}")

    async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        fault = self.faults.get(len(self.calls))
        if fault is not None:
            raise fault
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.replies:
            return FakeToolResult(json.dumps({"start": 0, "count": 0, "total": 0}))
        reply_ = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply_, FakeToolResult):
            return reply_
        if isinstance(reply_, str):
            return FakeToolResult(reply_)
        return FakeToolResult(json.dumps(reply_))


def search_payload(*entities: tuple[str, str], total: int | None = None) -> dict[str, Any]:
    """A `search` response in the shape the real server was MEASURED to send.

    Note what is deliberately absent: a Dataset's search fragment returns `urn` and
    `properties.name` and nothing else (`gql/search.gql`), so a fake that also handed back a
    platform or a description would let Attest depend on fields the transport never sends.
    """
    results = [
        {"entity": {"urn": urn, "properties": {"name": name}}} for urn, name in entities
    ]
    payload: dict[str, Any] = {
        "start": 0,
        "count": max(len(results), 1),
        "total": len(results) if total is None else total,
    }
    if results:
        # The server STRIPS empty arrays, so a zero-result response carries no
        # `searchResults` key at all. Reproduced here rather than smoothed over: that
        # absence, read against `total`, is the whole zero-results rule.
        payload["searchResults"] = results
    return payload


def dataset(urn: str, **aspects: Any) -> DatasetSnapshot:
    """A snapshot with only the aspects you name. Everything else is None: ABSENT.

    The default matters. An unset aspect here is the catalog being SILENT, not empty —
    the distinction the whole Insufficient-Coverage verdict rests on (see snapshot.py).
    """
    return DatasetSnapshot(urn=urn, **aspects)
