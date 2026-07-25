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


# The read timeout, and the reason it is not the SDK's. `DEFAULT_TIMEOUT.read` is **600
# seconds**, so one wedged socket stalls an audit for ten minutes and looks exactly like a
# slow audit — which is the failure CLAUDE.md already recorded for the calibration labeler
# ("ONE wedged socket stalls a 40-case run for half an hour and looks exactly like a slow
# one"). The labeler got a timeout; this path, which is the one a demo runs, never did.
#
# 30s is roughly ten times a healthy call: a whole 16-step audit — four model calls and three
# catalog fetches — measures ~23s end to end. A socket that has produced nothing for 30s is
# wedged, not thinking. The ceiling that matters is the compound one: the SDK makes three
# attempts, so a dead provider now costs at most ~92s instead of ~30 minutes.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 30.0

# What to call the upstream in a message a human reads. Attest talks to exactly one provider
# and the default does not move (CLAUDE.md), so this is a noun, not a config value.
PROVIDER = "the model provider"


class LLMError(RuntimeError):
    """The model could not be made to produce usable output."""


class MalformedOutput(LLMError):
    """The model returned something that is not valid JSON for the schema it was given."""


class ProviderError(LLMError):
    """The PROVIDER failed. There is no output to be malformed — the call never landed.

    Kept apart from `MalformedOutput` because the honest response to the two differs. A model
    that returns garbage twice against a strict schema has said something, and "no claims" is
    a reasonable thing to make of it. A provider that returned nothing has said NOTHING, and
    reporting that as "no claims" would be Attest rendering silence as an answer — the exact
    collapse this product exists to refuse, committed in its own output.

    It is still an `LLMError`, and that is deliberate: `explain` and `revise` already degrade
    on `LLMError`, and degrading is right for them. A verdict is decided by deterministic code
    BEFORE either runs, so a provider outage there costs prose, not correctness. `decompose`
    catches the narrower `MalformedOutput` precisely so this class reaches the caller.
    """


class ProviderUnavailable(ProviderError):
    """Transient, AND ALREADY RETRIED BY THE TRANSPORT. Do not retry it again here.

    The SDK's default is `max_retries=2` with exponential backoff and jitter, and its
    `_should_retry` covers 408, 409, 429 and every 5xx as well as connection failures — so
    an exception of this class has already cost three requests. A second loop in Attest would
    make six requests and ~20s of backoff for one dead provider: a hang wearing resilience's
    clothes. The bounded retry is the SDK's; ours is the honest report that it lost.
    """


class ProviderRefused(ProviderError):
    """The provider rejected the request itself. Retrying cannot change the answer.

    A bad key, a model name that does not exist, a request the API will not accept. These
    must surface fast and loudly rather than being waited out — they are configuration or a
    bug on Attest's side, and the one thing that does not help is patience.
    """


def _as_provider_error(exc: Exception) -> ProviderError | None:
    """Which of Attest's two provider failures is this, if either? None means "not ours".

    **The transient line is drawn exactly where the SDK draws it** — `_should_retry`: 408,
    409, 429, any 5xx, and connection/timeout failures. That is not a coincidence to be
    tidied away; it is what makes `ProviderUnavailable` mean something specific ("the
    transport already retried this and still failed"). Draw the line anywhere else and Attest
    either promises a retry that never happened or refuses one that silently did.

    The SDK is imported here rather than at module scope for the same reason `_chat` does it:
    the deterministic core never pulls in the model client. A machine without it gets None
    and the original exception, which is the correct answer — nothing here can classify an
    exception type that does not exist.
    """
    try:
        from openai import APIConnectionError, APIStatusError
    except ImportError:  # pragma: no cover - the SDK is a declared dependency
        return None

    # APITimeoutError is a subclass, so this arm covers it. Both mean the same thing to a
    # caller: the request never produced a response, and the transport already tried again.
    if isinstance(exc, APIConnectionError):
        return ProviderUnavailable(f"{PROVIDER} could not be reached: {exc}")

    if isinstance(exc, APIStatusError):
        status = exc.status_code
        detail = f"{PROVIDER} returned {status}"
        # The provider's own request id, when it gave one. It is the only handle anyone has
        # on the other side of this, and a paraphrase of the error is not a substitute.
        request_id = getattr(exc, "request_id", None)
        if request_id:
            detail = f"{detail} (request {request_id})"
        if status >= 500 or status in (408, 409, 429):
            return ProviderUnavailable(f"{detail}: {exc}")
        return ProviderRefused(f"{detail}: {exc}")

    return None


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
        # Memoized: the SDK client is thread-safe and pools connections, so every run in
        # this process shares it. What must NOT be shared is `usage` — see the class
        # docstring — and that is per-handle.
        self._built = self._build_openai().chat.completions
        return self._built  # type: ignore[return-value]

    def _build_openai(self) -> Any:
        """The SDK client, configured. A named seam so a test can inspect what it was built with.

        The timeout is the whole reason this is not inline. It is the one transport setting
        Attest overrides, it is invisible from any call site, and the failure it prevents —
        a ten-minute stall that reads as a slow audit — is the kind nobody thinks to test for
        unless the thing that sets it can be reached and asked.
        """
        # Imported lazily so the deterministic core never pulls in the SDK, and so a
        # missing key is an error at call time rather than at import time.
        import httpx
        from openai import OpenAI

        if not settings.openai_api_key:
            raise LLMError(
                "OPENAI_API_KEY is not set. The deterministic checkers do not need it; "
                "the semantic layer does. Copy .env.example to .env and fill it in."
            )

        return OpenAI(
            api_key=settings.openai_api_key,
            # `max_retries` is deliberately LEFT AT THE SDK's DEFAULT (2). It is already a
            # bounded exponential backoff with jitter that honours `retry-after`, and
            # ProviderUnavailable is defined as "that already ran and lost". Raising it here
            # would lengthen every outage; adding a second loop above it would multiply.
            timeout=httpx.Timeout(
                READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS
            ),
        )

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

    def _create(
        self,
        model: str,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
    ) -> Any:
        """One request to the provider. The transport's failures are classified HERE.

        This is the transport-classification seam, and the TLS repair was already living on
        it — a certificate failure is likewise not a model failure and is likewise something
        only this layer can recognise. What it does is TRANSLATE, never retry: the retrying
        is the SDK's (see `_build_openai`), and what comes back out is Attest's own taxonomy,
        so the degradation paths above can tell "the model said something unusable" from "the
        provider said nothing at all".

        A `ProviderError` raised here is NOT caught by the schema-repair loop in `json` — it
        is an `LLMError` but not a `MalformedOutput` — so it leaves immediately without
        consuming an attempt. Handing the model an error message it never received would be
        two wasted requests and a nonsense prompt.
        """

        def once() -> Any:
            return self._chat().create(
                model=model,
                messages=messages,
                response_format=response_format,
                temperature=0,
            )

        try:
            try:
                return once()
            except Exception as exc:
                # A TLS failure is not a model failure, and it must not be repaired by
                # rewriting the prompt. Retry the same call once against the OS certificate
                # store; if that is not available, the original error stands.
                if not _is_tls_failure(exc) or not self._use_os_truststore():
                    raise
                return once()
        except Exception as exc:
            provider_error = _as_provider_error(exc)
            if provider_error is None:
                raise
            # `from exc` keeps the SDK's exception in the chain: the translation is for the
            # caller's benefit and must not cost a debugger the original traceback.
            raise provider_error from exc

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

        **This loop is for MALFORMED OUTPUT ONLY.** A provider failure (`ProviderError`,
        raised by `_create`) leaves immediately without consuming an attempt: the transport
        has already done the retrying that is worth doing, and re-prompting a model that
        never answered would be two wasted requests and a nonsense conversation.
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
                response = self._create(model, messages, response_format)
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
