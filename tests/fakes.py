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

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from attest.datahub import DataHubError, DatasetSnapshot, EntityNotFoundError


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
    """

    replies: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    tokens: tuple[int, int] | None = None

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
    """A DataHubClient stand-in. Serves snapshots by URN; raises on anything else."""

    def __init__(self, snapshots: dict[str, DatasetSnapshot]) -> None:
        self.snapshots = snapshots
        self.fetched: list[str] = []

    def fetch_dataset(self, urn: str) -> DatasetSnapshot:
        self.fetched.append(urn)
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
    """

    def __init__(self, snapshots: dict[str, DatasetSnapshot], fail: bool = False) -> None:
        super().__init__(snapshots)
        self.fail = fail
        self.defined: list[str] = []
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
            raise DataHubError("GraphQL transport error: the catalog is down")
        return {"appConfig": {"appVersion": "v1.5.0.6"}}

    def get_structured_property(self, urn: str) -> dict[str, Any] | None:
        if self.fail:
            raise DataHubError("GraphQL transport error: the catalog is down")
        return {"urn": urn} if urn in self.defined else None

    def create_structured_property(self, qualified_name: str, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise DataHubError("GraphQL transport error: the catalog is down")
        self.defined.append(f"urn:li:structuredProperty:{qualified_name}")
        return {"urn": f"urn:li:structuredProperty:{qualified_name}"}

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
            raise DataHubError("GraphQL transport error: the catalog is down")
        node = self.assertions.get(urn)
        if node is None:
            return None
        return self._with_events(node)

    def list_dataset_assertions(
        self, dataset_urn: str, start: int = 0, count: int = 50
    ) -> list[dict[str, Any]]:
        if self.fail:
            raise DataHubError("GraphQL transport error: the catalog is down")
        self.dataset_reads.append({"dataset_urn": dataset_urn})
        # SCOPES, AND FILTERS NOTHING — modelled on the real entry point rather than made
        # convenient. A fake that quietly accepted a verdict filter here would let the
        # push-down report claim a server-side filter this server does not have.
        return [
            self._with_events(node)
            for node in self.assertions.values()
            if (node["info"]["customAssertion"]["entityUrn"] == dataset_urn)
        ][start : start + count]

    def search_assertions(
        self,
        custom_types: Sequence[str] = (),
        tags: Sequence[str] = (),
        start: int = 0,
        count: int = 50,
    ) -> list[dict[str, Any]]:
        """FILTERS, AND SCOPES TO NOTHING. The mirror image of the read above.

        The two entry points are disjoint on the real server (measured: no assertee field
        is indexed, so search cannot restrict to a dataset), and they are disjoint here for
        the same reason — a fake that let one do both would make the retrieval path's whole
        honesty argument untestable.
        """
        if self.fail:
            raise DataHubError("GraphQL transport error: the catalog is down")
        self.searches.append(
            {"custom_types": list(custom_types), "tags": list(tags)}
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
        return found[start : start + count]

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


def dataset(urn: str, **aspects: Any) -> DatasetSnapshot:
    """A snapshot with only the aspects you name. Everything else is None: ABSENT.

    The default matters. An unset aspect here is the catalog being SILENT, not empty —
    the distinction the whole Insufficient-Coverage verdict rests on (see snapshot.py).
    """
    return DatasetSnapshot(urn=urn, **aspects)
