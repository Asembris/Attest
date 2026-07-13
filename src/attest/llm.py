"""The only place Attest talks to a model.

Everything above this module deals in typed objects; everything below is the OpenAI
SDK. Three rules are enforced here rather than at each call site, because a rule that
lives at a call site is a rule that the next call site forgets.

**Structured output only.** Every step sends a JSON schema and gets back parsed JSON,
via OpenAI's strict `json_schema` response format — the model cannot return prose where
a verdict was expected. Free-text completion is not exposed by this module at all.

**temperature=0.** An auditor that returns a different answer to the same question on
Tuesday is not an auditor. This is not tuneable per step; it is the point.

**The model is a per-step config value.** `settings.model_for(step)` resolves it, so the
entailment step can be moved to a stronger model without touching a call site or
dragging the cheap steps up with it. Nothing here hardcodes a model name.

The client is injected, which is what lets the entire semantic layer be tested offline
against a scripted fake — see tests/fakes.py. Attest's own test suite never spends a
token, and the faithfulness guard is tested against hallucinations that a real model
would only produce by luck.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from attest.config import Step, settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model could not be made to produce usable output."""


class MalformedOutput(LLMError):
    """The model returned something that is not valid JSON for the schema it was given."""


class ChatClient(Protocol):
    """The slice of the OpenAI SDK Attest uses. Implemented by a fake in the tests."""

    def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
        temperature: float,
    ) -> Any: ...


@dataclass
class Usage:
    """What a step cost. Recorded so a run can be audited after the fact."""

    step: Step
    model: str
    attempts: int = 1
    repaired: bool = False


@dataclass
class LLM:
    """Structured-JSON calls to OpenAI, with the model resolved per step."""

    client: ChatClient | None = None
    max_attempts: int = 2
    usage: list[Usage] = field(default_factory=list)

    def _chat(self) -> ChatClient:
        if self.client is not None:
            return self.client
        # Imported lazily so the deterministic core never pulls in the SDK, and so a
        # missing key is an error at call time rather than at import time.
        from openai import OpenAI

        if not settings.openai_api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. The deterministic checkers do not need it; "
                "the semantic layer does. Copy .env.example to .env and fill it in."
            )

        # Trust the OS certificate store, not just certifi's bundle. Behind a
        # TLS-inspecting corporate network the proxy presents a certificate signed by a
        # root CA that certifi has never heard of, and every OpenAI call dies with
        # CERTIFICATE_VERIFY_FAILED — which the SDK surfaces as a generic
        # APIConnectionError and reads like an outage or a bad key. It is neither. The
        # same landmine takes out `datahub docker quickstart`; see docs/datahub-setup.md.
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:  # pragma: no cover — truststore is a declared dependency
            log.debug("truststore not installed; falling back to certifi's CA bundle")

        return OpenAI(api_key=settings.openai_api_key).chat.completions  # type: ignore[return-value]

    def json(
        self,
        step: Step,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
    ) -> dict[str, Any]:
        """One structured call. Retries once on malformed output, then gives up.

        The retry hands the model its own error back, which is the only thing that
        makes a second attempt worth more than the first. Two attempts, not five: if a
        temperature=0 model has produced unparseable output twice against a strict
        schema, the schema or the prompt is wrong, and burning tokens will not fix
        either. The caller decides what a failure means — for explanations it means
        falling back to a deterministic template, never to an unverified sentence.
        """
        model = settings.model_for(step)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": schema},
        }

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self._chat().create(
                    model=model,
                    messages=messages,
                    response_format=response_format,
                    temperature=0,
                )
                content = response.choices[0].message.content or ""
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise MalformedOutput(f"expected a JSON object, got {type(parsed).__name__}")

                self.usage.append(
                    Usage(step=step, model=model, attempts=attempt, repaired=attempt > 1)
                )
                return parsed

            except (json.JSONDecodeError, MalformedOutput) as exc:
                last_error = exc
                log.warning(
                    "malformed output from %s on attempt %d/%d: %s",
                    model,
                    attempt,
                    self.max_attempts,
                    exc,
                )
                if attempt < self.max_attempts:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous reply was not valid JSON for the schema: "
                                f"{exc}. Reply with JSON matching the schema and nothing else."
                            ),
                        }
                    )

        raise MalformedOutput(
            f"{model} failed to produce schema-valid JSON for {step.value} after "
            f"{self.max_attempts} attempts: {last_error}"
        )
