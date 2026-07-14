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
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from attest.datahub import DatasetSnapshot, EntityNotFoundError


@dataclass
class FakeChat:
    """Replays canned completions and records exactly what it was asked.

    `replies` are consumed in order; the last one repeats if the caller asks again, so a
    test that only cares about one reply does not have to pad the list.
    """

    replies: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)

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
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

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


def dataset(urn: str, **aspects: Any) -> DatasetSnapshot:
    """A snapshot with only the aspects you name. Everything else is None: ABSENT.

    The default matters. An unset aspect here is the catalog being SILENT, not empty —
    the distinction the whole Insufficient-Coverage verdict rests on (see snapshot.py).
    """
    return DatasetSnapshot(urn=urn, **aspects)
