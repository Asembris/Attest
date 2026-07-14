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
import ssl
from dataclasses import dataclass, field
from typing import Any, Protocol

from attest import cost
from attest.config import Step, settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model could not be made to produce usable output."""


class MalformedOutput(LLMError):
    """The model returned something that is not valid JSON for the schema it was given."""


def _is_tls_failure(exc: Exception) -> bool:
    """Is this a certificate-verification failure wearing a connection error's clothes?

    The OpenAI SDK wraps every transport problem as APIConnectionError, so a genuine
    outage and a missing root CA arrive looking identical. The distinguishing detail is
    only in the cause chain, which is why this walks it rather than matching on the type.
    """
    seen: Exception | BaseException | None = exc
    for _ in range(6):  # the SDK nests httpx -> httpcore -> ssl; six is generous
        if seen is None:
            return False
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(seen):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


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
    """What a step cost. Recorded so a run can be audited after the fact.

    Token counts come from the API's own `usage` block, never from a local estimate — a
    guessed token count printed as a receipt is not a receipt. A client that reports no
    usage (the scripted fake in the tests) leaves them at zero and `cost_usd` at None,
    which is honest: nothing was spent because nothing was called.
    """

    step: Step
    model: str
    attempts: int = 1
    repaired: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    # None means "we cannot price this", NOT "this was free". See cost.py.
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _tokens(response: Any) -> tuple[int, int]:
    """(input, output) token counts from a response, or (0, 0) if it reports none."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "prompt_tokens", 0) or 0),
        int(getattr(usage, "completion_tokens", 0) or 0),
    )


@dataclass
class LLM:
    """Structured-JSON calls to OpenAI, with the model resolved per step.

    **`usage` is the receipt, and it belongs to ONE run.** observe.py bills a step by
    slicing this list from a mark taken when the step opened, so every token appended to
    it while a step is in flight is charged to that step. Two audits sharing one handle
    would therefore bill each other: run A's decompose step would be charged with whatever
    run B spent while A was waiting on the API — and the receipts are the product.

    `for_run()` is what makes that structurally impossible. Each run gets its own handle
    with its own `usage`, forked from the one the pipeline was built with, sharing the
    transport underneath and nothing else. The billing is then per-run by construction
    rather than by a lock that a later refactor is free to remove.
    """

    # An INJECTED client — the scripted fake in the tests, or a preconfigured SDK client.
    # If it is set, nothing here builds anything.
    client: ChatClient | None = None
    max_attempts: int = 2
    usage: list[Usage] = field(default_factory=list)
    _truststore_injected: bool = False
    # The OpenAI client this handle built for itself, memoized. Kept SEPARATE from `client`
    # because it is ours to throw away: the TLS repair below has to rebuild it, and it must
    # never throw away a client the caller injected.
    _built: ChatClient | None = None
    # The handle this one was forked from. A fork owns its own `usage` and borrows the
    # parent's transport — including the OpenAI client the parent builds lazily, and the
    # truststore repair the parent may have had to make. Set only by `for_run`.
    _forked_from: LLM | None = None

    def for_run(self) -> LLM:
        """A handle for ONE audit run: this one's transport, its own token ledger.

        One HTTP client (thread-safe, connection-pooled, built once) and N usage lists,
        rather than one usage list shared by N runs. That is the whole difference between
        a receipt and a total.
        """
        return LLM(max_attempts=self.max_attempts, _forked_from=self)

    def _chat(self) -> ChatClient:
        if self._forked_from is not None:
            # One client for the process, built by the handle the pipeline holds. A fork
            # that built its own would open a connection pool per audit.
            return self._forked_from._chat()
        if self.client is not None:
            return self.client
        if self._built is not None:
            return self._built
        # Imported lazily so the deterministic core never pulls in the SDK, and so a
        # missing key is an error at call time rather than at import time.
        from openai import OpenAI

        if not settings.openai_api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. The deterministic checkers do not need it; "
                "the semantic layer does. Copy .env.example to .env and fill it in."
            )

        # Memoized: the SDK client is thread-safe and pools connections, so every run in
        # this process shares it. What must NOT be shared is `usage` — see the class
        # docstring — and that is per-handle.
        self._built = OpenAI(api_key=settings.openai_api_key).chat.completions  # type: ignore[assignment]
        return self._built  # type: ignore[return-value]

    def _use_os_truststore(self) -> bool:
        """Fall back to the OS certificate store after a TLS failure. Once, then never.

        Deliberately NOT done up front. Behind a TLS-inspecting network the proxy presents
        a certificate signed by a root CA that certifi has never heard of, and every call
        dies with CERTIFICATE_VERIFY_FAILED — which the SDK reports as a generic
        `APIConnectionError: Connection error`, reading like an outage or a bad key. The OS
        store has that CA, so switching to it fixes the call.

        But injecting it unconditionally would be a bad trade: on a machine whose OS trust
        store is empty or minimal — a slim container with no `ca-certificates` package —
        certifi is the thing that works, and pre-emptively replacing it would break the
        normal case to fix the unusual one. So certifi goes first, the OS store is a repair
        attempted only once TLS has actually failed, and if it is unavailable we say so and
        re-raise the original error rather than swallowing it.
        """
        if self._forked_from is not None:
            # The repair is a property of the transport, not of a run. Delegating it means
            # the first run to hit a TLS-inspecting proxy fixes it for every run after it,
            # rather than each run rediscovering the same broken network.
            return self._forked_from._use_os_truststore()
        if self._truststore_injected or not settings.ssl_os_truststore:
            return False
        try:
            import truststore

            truststore.inject_into_ssl()
        except Exception as exc:  # pragma: no cover — needs a machine without truststore
            log.warning("could not use the OS certificate store: %s", exc)
            return False

        self._truststore_injected = True

        # THROW THE BUILT CLIENT AWAY. `truststore.inject_into_ssl()` changes how new SSL
        # contexts are created; it cannot reach inside the one the existing client's
        # transport already holds. So the retry has to happen on a client built AFTER the
        # injection, or it repeats the same handshake against the same untrusting context
        # and fails identically — with the log line above cheerfully claiming a repair that
        # did nothing. `client` is the caller's and is never touched; `_built` is ours.
        self._built = None

        log.warning(
            "TLS verification failed against certifi's CA bundle — rebuilding the client "
            "against the OS certificate store. This is what a TLS-inspecting network looks "
            "like."
        )
        return True

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
                try:
                    response = self._chat().create(
                        model=model,
                        messages=messages,
                        response_format=response_format,
                        temperature=0,
                    )
                except Exception as exc:
                    # A TLS failure is not a model failure, and it must not be repaired by
                    # rewriting the prompt. Retry the same call once against the OS
                    # certificate store; if that is not available, the original error stands.
                    if not _is_tls_failure(exc) or not self._use_os_truststore():
                        raise
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

                input_tokens, output_tokens = _tokens(response)
                self.usage.append(
                    Usage(
                        step=step,
                        model=model,
                        attempts=attempt,
                        repaired=attempt > 1,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost.cost_usd(model, input_tokens, output_tokens)
                        if (input_tokens or output_tokens)
                        else None,
                    )
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
