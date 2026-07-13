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
