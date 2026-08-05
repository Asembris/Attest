"""The DataHub MCP Server, as a discovery transport. One tool, one field, one direction.

This is the only module in the package that imports `mcp`, and it imports it lazily, so
`attest.discovery` is importable with the optional extra uninstalled — which is what CI
runs. See the package docstring for the boundary this sits inside; this file is about the
transport.

--------------------------------------------------------------------------------
The tool, read off the server rather than assumed
--------------------------------------------------------------------------------

`mcp-server-datahub` 0.6.0 — the version `settings.mcp_command` PINS, for the reasons stated
there — against Core v1.5.0.6 registers six read tools, and the one used
here is `search` (`mcp_server.register_search_tools`; `enhanced_search` takes the same name
only when `SEMANTIC_SEARCH_ENABLED` is set, which is Cloud-only). Its declared input schema,
measured live, is `query` / `filter` / `num_results` / `sort_by` / `sort_order` / `offset`,
with `additionalProperties: false` and `readOnlyHint: true`. `num_results` is capped
server-side at 50 (`min(num_results, 50)`).

Its response is JSON in `content[0].text`, passed through `clean_gql_response` — which drops
`__typename`, `None`, `[]` and `{}` recursively. Measured, for `/q customer`:

    {"start": 0, "count": 5, "total": 4,
     "searchResults": [{"entity": {"urn": "urn:li:dataset:(...customer_profile,PROD)",
                                   "properties": {"name": "customer_profile"}}}, ...],
     "facets": [...]}

**A Dataset's search fragment returns `urn` and `properties.name` and NOTHING ELSE**
(`gql/search.gql`, `fragment SearchEntityInfo`). That is why `SearchHit` has two fields: a
field the transport does not return is a field Attest does not invent.

--------------------------------------------------------------------------------
THE ZERO-RESULTS RULE, which is the sharpest thing in this file
--------------------------------------------------------------------------------

The empty-array strip means a search that matches nothing comes back with **no
`searchResults` key at all**. Measured:

    {"start": 0, "count": 5, "total": 0, "facets": [...]}      <- zero matches, a real answer

So the key's absence is ambiguous on its own, and the discriminator is `total`:

    absent searchResults + total == 0   ->  the catalog matched nothing.  200, hits: []
    absent searchResults + total  > 0   ->  the transport is lying.       DiscoveryUnavailable

The tempting implementation is `payload.get("searchResults", [])`, which collapses both into
"nothing found". That is §20's collapse at a third transport, in a project that has now
shipped it twice — an outage rendered as an answer. `parse_search_payload` is written as a
taxonomy for that reason, and `tests/test_discovery.py` proves the naive parse fails the
test that guards it.

--------------------------------------------------------------------------------
Owning a stdio session under uvicorn, which is the risky part
--------------------------------------------------------------------------------

MEASURED on this machine: spawn + `initialize` costs **3.33s** (warm uvx cache) and a call
costs **125-344ms**. So a session per request is unusable for a debounced picker, and a
persistent one is nearly free. That decides the shape, and the shape has four constraints:

  * **The API routes are `def`, not `async def`** (app.py, deliberately — an audit is
    seconds of blocking work and must not sit on the event loop). So they cannot await an
    MCP session, and this owns a **private event loop on its own daemon thread**, driven
    from the routes with `run_coroutine_threadsafe`. A private loop also means the
    subprocess runs on Windows' default Proactor loop whatever else is going on.

  * **ONE task enters and exits the session's context.** `stdio_client` is anyio-based, and
    entering its cancel scope in one task and exiting it in another raises *"Attempted to
    exit cancel scope in a different task"*. So a single supervisor task owns the whole
    stack for the session's life and simply waits on a stop event; searches only take a lock
    and await `call_tool`, which owns no scope. This is the single easiest way to get this
    wrong, and it fails at teardown rather than at startup.

  * **Lazy.** Nothing spawns until the first search. A deployment that never opens the
    picker never runs the server, `/health` never starts one (it reports last-known state,
    and "not contacted" is an honest answer rather than a guess), and the offline test tier
    never comes near it.

  * **A failure tears the session down and the next call rebuilds it**, so a transient
    outage self-heals without a retry loop. Attest adds **no retry of its own**: unlike the
    OpenAI SDK (§18) there is no transport-level retry underneath to complement, so one
    failure is one honest failure and the human at the keyboard decides whether to type
    again.

Teardown is wired into FastAPI's `lifespan` (app.py). Exiting the stack closes the child's
stdin, which is how a stdio MCP server is asked to exit; if it does not, it is killed. See
docs/deployment.md for what happens when the PARENT is killed instead.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import importlib.util
import json
import logging
import os
import shlex
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from attest.config import settings
from attest.discovery import (
    DiscoveryError,
    DiscoveryNotConfigured,
    DiscoveryResult,
    DiscoveryUnavailable,
    SearchHit,
)

log = logging.getLogger(__name__)

# How Attest names this transport on the wire. A noun for a human reading a response, not a
# config value: there is one discovery transport and the evaluation that chose it is §12.
TRANSPORT = "DataHub MCP Server"

# The tool, and the entity filter. Attest audits DATASETS — `BaseClaim.target_urn` validates
# a dataset URN and refuses anything else — so offering a Chart or a Container would be
# offering a URN that can never be audited. Filtering server-side is both cheaper and the
# only honest scope for this picker.
SEARCH_TOOL = "search"
DATASET_FILTER = "entity_type = dataset"
DATASET_URN_PREFIX = "urn:li:dataset:"

# What Attest will ask for at most, under the server's own cap of 50. A picker is a list a
# human reads, not a page they scroll; `DiscoveryResult.total` carries the catalog's real
# count so a truncated list is never mistaken for a complete one (Session 21 gap 1).
MAX_RESULTS = 25

# Characters that mean the caller is writing the server's own keyword syntax (boolean
# operators, phrases, field searches, an explicit wildcard). Their presence switches off the
# prefix-wildcard convenience below rather than mangling a deliberate query.
_QUERY_OPERATORS = '*"():+'


def build_query(text: str) -> str:
    """A picker's keystrokes, as the server's keyword syntax.

    The server's own docs say to start a keyword query with `/q`. MEASURED on the seeded
    catalog: `/q custo` matches 4 datasets and `/q custo*` matches 6 — a bare token is a
    whole-token match, so a picker that does not append the wildcard hides results while a
    human is still typing the name they are looking for.

    So: a single bare word gets the wildcard, and anything that already looks like a query —
    an explicit `/q`, multiple words, or any operator character — is passed through
    untouched rather than being "helpfully" rewritten into something the caller did not ask
    for. An empty box lists the catalog (`*`), which is what the static picker did.
    """
    text = text.strip()
    if not text:
        return "*"
    if text.startswith("/q"):
        return text
    if " " in text or any(c in text for c in _QUERY_OPERATORS):
        return f"/q {text}"
    return f"/q {text}*"


def parse_search_payload(
    payload: Any, *, transport: str = TRANSPORT, server: str = ""
) -> DiscoveryResult:
    """The `search` tool's payload -> candidates. Strict, and empty is not malformed.

    Pure, so the taxonomy can be tested without a transport — and so the transport can be
    faked without faking the part that decides what an answer means.

    Every `DiscoveryUnavailable` raised here is a response that did not have the shape the
    server documents and that this file measured. None of them is "the catalog matched
    nothing", which is a 200 with an empty list. See the module docstring.
    """
    if not isinstance(payload, dict):
        raise DiscoveryUnavailable(
            f"the {transport} returned a {type(payload).__name__}, not a search result "
            f"object. Nothing was searched; this is not an empty catalog."
        )

    total = payload.get("total")
    if not isinstance(total, int):
        raise DiscoveryUnavailable(
            f"the {transport}'s search result carries no `total` ({total!r}). Without it, "
            f"an absent result list cannot be told from a broken response, and guessing "
            f"would report a transport failure as an empty catalog."
        )

    results = payload.get("searchResults")
    if results is None:
        # THE ZERO-RESULTS RULE. The server strips empty arrays, so a genuine no-match has
        # no `searchResults` key — and so does a response that lost its results on the way.
        # `total` is the only thing that tells them apart.
        if total == 0:
            return DiscoveryResult(
                hits=(), total=0, transport=transport, server=server
            )
        raise DiscoveryUnavailable(
            f"the {transport} reported {total} matches and returned no results with them. "
            f"That is a broken response, not an empty catalog, and reporting it as 'nothing "
            f"found' would be an outage wearing an answer's clothes."
        )
    if not isinstance(results, list):
        raise DiscoveryUnavailable(
            f"the {transport} returned `searchResults` as a {type(results).__name__}, not a "
            f"list."
        )

    hits: list[SearchHit] = []
    dropped = 0
    for n, row in enumerate(results):
        entity = (row or {}).get("entity") if isinstance(row, dict) else None
        urn = (entity or {}).get("urn") if isinstance(entity, dict) else None
        if not isinstance(urn, str) or not urn:
            # The URN is the ONE field discovery depends on and the one field this transport
            # returns losslessly. A row without it is the transport failing at the only thing
            # it is being asked for, so it is named rather than quietly skipped.
            raise DiscoveryUnavailable(
                f"the {transport} returned a search result (#{n}) with no entity URN. The "
                f"URN is the only value discovery passes on, so a result without one cannot "
                f"be shown or picked."
            )
        if not urn.startswith(DATASET_URN_PREFIX):
            # We asked for datasets. Anything else cannot be audited (`BaseClaim.target_urn`
            # refuses it), so it is dropped — and COUNTED, because a silently shorter list is
            # the absence this project refuses. The count rides back in the note.
            dropped += 1
            continue
        name = ((entity.get("properties") or {}) if isinstance(entity, dict) else {}).get(
            "name"
        )
        hits.append(SearchHit(urn=urn, name=name if isinstance(name, str) else ""))

    result = DiscoveryResult(
        hits=tuple(hits), total=total, transport=transport, server=server
    )
    if dropped:
        result = DiscoveryResult(
            hits=result.hits,
            total=result.total,
            transport=result.transport,
            server=result.server,
            note=(
                f"{result.note} {dropped} non-dataset candidate(s) were dropped: Attest "
                f"audits datasets, so a URN of any other kind could never be checked."
            ),
        )
    return result


def stdio_parameters():
    """The server, launched exactly as the Session 17 probe launched it.

    `--native-tls` is this machine's corporate-CA trap one runtime further out than Python's
    `truststore` and Node's `NODE_USE_SYSTEM_CA`: uv ships its own Rust root store and does
    not consult the OS one until told. Harmless when the CA is already trusted, so it is set
    unconditionally (CLAUDE.md's system-CA table).

    Telemetry is OFF. The server posts usage to Mixpanel on startup, before it is asked to do
    anything; a product that reads someone's catalog should not also report to a third party
    that it did.
    """
    from mcp import StdioServerParameters

    argv = shlex.split(settings.mcp_command)
    if not argv:
        raise DiscoveryNotConfigured(
            "ATTEST_MCP_COMMAND is empty, so there is no discovery server to launch."
        )
    env = {
        "DATAHUB_GMS_URL": settings.datahub_gms_url,
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "PATH": os.environ.get("PATH", ""),
    }
    if settings.datahub_token:
        env["DATAHUB_TOKEN"] = settings.datahub_token
    return StdioServerParameters(command=argv[0], args=argv[1:], env=env)


@asynccontextmanager
async def stdio_session() -> AsyncIterator[Any]:
    """The real transport: a stdio MCP session, held open for its caller's lifetime.

    The `mcp` import is HERE rather than at module scope so this module is importable with
    the optional extra uninstalled — which is what CI installs, and what a deployment that
    does not want discovery installs.
    """
    try:
        from mcp import ClientSession
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - exercised by the not-configured path
        raise DiscoveryNotConfigured(
            "the `mcp` client is not installed, so catalog discovery is unavailable. It is "
            "an optional extra, deliberately not part of `dev`: pip install -e '.[mcp]'."
        ) from exc

    async with stdio_client(stdio_parameters()) as (read, write):
        async with ClientSession(read, write) as session:
            yield session


class McpDiscovery:
    """A live MCP session, owned by a private loop thread, answering search from `def` routes.

    Not a cache and not a pool: exactly one session, started on first use, reused for every
    search, and rebuilt after any failure. See the module docstring for why each of those is
    the way it is.
    """

    def __init__(
        self,
        session_factory: Callable[[], Any] | None = None,
        *,
        transport: str = TRANSPORT,
    ) -> None:
        # INJECTABLE, exactly as `LLM`'s chat client and `cache.Reader` are. A test supplies a
        # fake session and the REAL loop, supervisor, lock, timeout and parsing code runs
        # against it — which is the whole point, because a fake that also replaced the
        # translation would be testing the test (Session 5's rule).
        self._factory = session_factory or stdio_session
        self._transport = transport

        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: Any = None
        self._stop: asyncio.Event | None = None
        self._call_lock: asyncio.Lock | None = None
        self._supervisor: concurrent.futures.Future | None = None
        self._server = ""
        self._last_error = ""
        self._closed = False

    # --- the public surface ---------------------------------------------------

    def search(self, text: str, limit: int = 10) -> DiscoveryResult:
        """Candidate datasets for what a human typed. Never an empty list for a failure."""
        if not settings.discovery_enabled:
            raise DiscoveryNotConfigured(
                "catalog discovery is switched off (ATTEST_DISCOVERY_ENABLED=false)."
            )
        if self._closed:
            raise DiscoveryNotConfigured("this discovery session has been closed.")

        session = self._ensure_started()
        args = {
            "query": build_query(text),
            "filter": DATASET_FILTER,
            "num_results": max(1, min(limit, MAX_RESULTS)),
        }
        raw = self._call_tool(session, SEARCH_TOOL, args)
        return parse_search_payload(raw, transport=self._transport, server=self._server)

    def status(self) -> str:
        """What `/health` reports. NEVER starts a session, and says so when it has not.

        A health probe that spawned a subprocess would spawn one on every browser load and
        every uptime check. "Not contacted" is the honest answer to a question nobody has
        asked yet — the same instinct as `ReadState.UNKNOWN` refusing to guess (§11).
        """
        if not settings.discovery_enabled:
            return "disabled"
        # The `mcp` package only decides anything for the DEFAULT transport. With a factory
        # injected, whether the client library happens to be installed on this machine is
        # irrelevant — and reporting "not-installed" for a session that is up would be a
        # health field describing a different program, which is what a test running on a
        # runner without the extra would have been shown.
        if self._factory is stdio_session and importlib.util.find_spec("mcp") is None:
            return "not-installed"
        if self._session is not None:
            return f"up: {self._server}" if self._server else "up"
        if self._last_error:
            return f"unreachable: {self._last_error}"
        return "not-contacted"

    def close(self) -> None:
        """Stop the session and the loop. Idempotent, and safe from any thread."""
        with self._guard:
            self._closed = True
            self._teardown_locked()
            loop, thread = self._loop, self._thread
            self._loop, self._thread = None, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=10)
        if loop is not None:
            with contextlib.suppress(Exception):
                loop.close()

    def __enter__(self) -> McpDiscovery:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- the session ----------------------------------------------------------

    def _ensure_started(self) -> Any:
        """The live session, starting one if there is not one. Serialized across threads."""
        with self._guard:
            if self._session is not None:
                return self._session
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._run_loop,
                    args=(self._loop,),
                    name="attest-discovery",
                    daemon=True,
                )
                self._thread.start()

            ready: concurrent.futures.Future = concurrent.futures.Future()
            self._supervisor = asyncio.run_coroutine_threadsafe(
                self._supervise(ready), self._loop
            )
            try:
                self._server = ready.result(timeout=settings.mcp_startup_timeout_seconds)
            except concurrent.futures.TimeoutError as exc:
                self._teardown_locked()
                self._last_error = (
                    f"it did not start within {settings.mcp_startup_timeout_seconds:.0f}s"
                )
                raise DiscoveryUnavailable(
                    f"the {self._transport} did not start within "
                    f"{settings.mcp_startup_timeout_seconds:.0f}s."
                ) from exc
            except DiscoveryNotConfigured:
                # A missing extra is not an outage: it must not be dressed as one, and it
                # must not be recorded as a transient failure to retry past.
                self._teardown_locked()
                raise
            except Exception as exc:
                self._teardown_locked()
                self._last_error = str(exc)
                raise DiscoveryUnavailable(
                    f"the {self._transport} could not be started: {exc}"
                ) from exc

            self._last_error = ""
            if self._session is None:  # pragma: no cover - the supervisor sets it first
                raise DiscoveryUnavailable(
                    f"the {self._transport} reported ready with no session."
                )
            return self._session

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _supervise(self, ready: concurrent.futures.Future) -> None:
        """Own the session's context for its whole life. ONE task, start to finish.

        Entering `stdio_client`'s cancel scope here and exiting it anywhere else raises
        *"Attempted to exit cancel scope in a different task"* — an anyio rule, and one that
        bites at teardown rather than at startup, which is the worst place to learn it. So
        this task enters the stack, hands the session out, waits, and exits the stack itself.
        """
        try:
            async with self._factory() as session:
                init = await session.initialize()
                info = getattr(init, "serverInfo", None)
                self._server = (
                    f"{info.name} v{info.version}" if info is not None else ""
                )
                self._call_lock = asyncio.Lock()
                self._stop = asyncio.Event()
                self._session = session
                ready.set_result(self._server)
                await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001 - reported to the waiter or logged
            if not ready.done():
                ready.set_exception(exc)
            else:
                # The session died after it was handed out. Nothing to raise at — the next
                # search finds no session and starts a new one, which is the self-healing
                # this class does instead of a retry loop.
                self._last_error = str(exc)
                log.warning("the discovery session ended: %s", exc)
        finally:
            self._session = None
            self._stop = None
            self._call_lock = None

    def _call_tool(self, session: Any, name: str, args: dict[str, Any]) -> Any:
        """One tool call, from a threadpool thread, with a bound and an honest failure."""
        loop = self._loop
        if loop is None:  # pragma: no cover - close() raced a search
            raise DiscoveryUnavailable("the discovery session was closed mid-search.")
        budget = settings.mcp_call_timeout_seconds
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._await_tool(session, name, args), loop
            )
            # A second, looser bound: the inner `wait_for` is what normally fires, and this
            # only catches a loop that is wedged rather than a call that is slow.
            result = future.result(timeout=budget + 5)
        except (concurrent.futures.TimeoutError, TimeoutError) as exc:
            self.restart()
            raise DiscoveryUnavailable(
                f"the {self._transport} did not answer within {budget:.0f}s. The session "
                f"was dropped; the next search starts a new one."
            ) from exc
        except DiscoveryError:
            # Already named and already honest — a re-wrap here would bury which failure it
            # was under a generic one.
            raise
        except Exception as exc:
            self.restart()
            raise DiscoveryUnavailable(
                f"the {self._transport} failed mid-search: {exc}"
            ) from exc

        if getattr(result, "isError", False):
            # The server reports a bad filter or an unknown tool as `isError: True` with a
            # text body, not as an exception (measured). It is still a failure, and it is
            # still not an empty catalog.
            raise DiscoveryUnavailable(
                f"the {self._transport} refused the search: {_first_text(result)}"
            )
        text = _first_text(result)
        try:
            return json.loads(text)
        except (TypeError, ValueError) as exc:
            raise DiscoveryUnavailable(
                f"the {self._transport} returned a body that is not JSON: {text[:200]!r}"
            ) from exc

    async def _await_tool(self, session: Any, name: str, args: dict[str, Any]) -> Any:
        """Serialize calls over the one session, and bound each of them."""
        lock = self._call_lock
        if lock is None:  # pragma: no cover - the session died between check and call
            raise DiscoveryUnavailable("the discovery session ended before the search ran.")
        async with lock:
            return await asyncio.wait_for(
                session.call_tool(name, args), settings.mcp_call_timeout_seconds
            )

    def restart(self) -> None:
        """Drop the session; the next search builds a new one.

        Called after any failure. A timed-out call leaves an outstanding request id on a
        session whose state nobody can vouch for, and a session nobody can vouch for is
        exactly the thing this project does not keep using.
        """
        with self._guard:
            self._teardown_locked()

    def _teardown_locked(self) -> None:
        """Stop the supervisor and wait for it to exit its own stack. Holds `_guard`."""
        loop, stop, supervisor = self._loop, self._stop, self._supervisor
        self._session = None
        self._supervisor = None
        if loop is not None and stop is not None:
            loop.call_soon_threadsafe(stop.set)
        if supervisor is not None:
            with contextlib.suppress(Exception):
                supervisor.result(timeout=10)
        self._stop = None
        self._call_lock = None


def _first_text(result: Any) -> str:
    """The text body of a tool result, however sparse the result turns out to be."""
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            return text
    return ""
