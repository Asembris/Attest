"""Catalog DISCOVERY: which dataset did you mean?

**MCP discovers. A human resolves. GraphQL verifies. Deterministic code decides.**

That sentence is the whole boundary, and every rule in this package exists to hold one
clause of it. Attest reads the catalog over GraphQL because a groundedness auditor needs a
lossless read, and [docs/mcp-evaluation.md](../../../docs/mcp-evaluation.md) measured what
happens when it does not: 130 field mismatches over 16 datasets, and a TRUE claim about a
correctly-tagged PII column coming back **Contradicted**. That decision is unchanged. This
module does something else.

--------------------------------------------------------------------------------
What crosses the boundary, and why exactly one value is allowed to
--------------------------------------------------------------------------------

**The ONLY value that leaves this package and reaches a run is a dataset URN a human
selected.** Not a name, not a description, not a tag, not a platform, not an ordering.

That is not a discipline, it is the arithmetic of the MCP finding. Every mechanism §12
measured is a loss of FIELD CONTENT — `lastModified` never requested, field tags and terms
flattened from `urn:li:tag:PII` to the display string `"PII"`, `type` commented out of the
server's own fragment, absent and empty collapsed. The **entity URN is the one field the
transport does not touch**: `searchResults[].entity.urn` arrives byte-identical to the
canonical URN, measured. So the value discovery hands on is precisely the value the lossy
transport cannot corrupt, and the fields it does corrupt are precisely the ones nothing
here passes on.

And the URN a human picks does not get to walk into an audit either. It must appear
**verbatim in `agent_output`** (`api.schemas.AuditRequest`) and be **quoted, never minted**
by the decomposer (decompose.py). A wrong pick therefore produces claims about an
explicitly wrong URN — visible in the report, in the claim's `target_urn`, and in the
published artifact — rather than a resolution error laundered into catalog disagreement,
which is the failure CLAUDE.md §4 exists to prevent.

--------------------------------------------------------------------------------
Why this is not the "demonstrated-but-non-verdict context path" §12 refuses
--------------------------------------------------------------------------------

§12 refuses running the inverting transport as a second CATALOG READ that feeds an audit,
purely to be able to say MCP was touched. Three things make discovery a different act:

  1. **§12's mechanism has no purchase here.** It is a finding about lossy field content;
     discovery consumes an identifier that arrives intact and nothing else.
  2. **It is not a second read of an audited entity.** Search runs on a human's keystroke,
     before any run exists, and never inside `Pipeline.run()`. The §2c consistency boundary
     — one run, one frozen read per entity — is untouched, because discovery resolves no
     entity. It proposes candidates.
  3. **Nothing it returns is evidence.** No checker, no snapshot, no cache and no graph node
     can reach this package, and `tests/test_discovery_boundary.py` asserts that
     structurally, in the house style of `NO_LLM_IN_THE_VERDICT_PATH`.

CLAUDE.md §4 forbids AUTOMATIC entity resolution — the system deciding text -> URN, where a
wrong resolution silently becomes a wrong verdict. This is candidates proposed and a human
picking, which is the shape the URN picker has had since Session 9; only the source of the
list changed, from a generated snapshot of one seeded catalog to the live one.

--------------------------------------------------------------------------------
Absence is not an answer, at a third transport
--------------------------------------------------------------------------------

Sessions 18 and 20 closed the same error-taxonomy hole twice: a provider that returned
nothing, and a catalog that could not be reached, were both being rendered as *an audit
that found nothing*. §12 named this transport as the next place it would happen, because
"nothing defends the catalog READ".

So the rules here are stated as a taxonomy rather than left to a `.get(…, [])`:

  * **Zero matches is a real answer.** `total == 0` with no `searchResults` key is what the
    server actually sends (the empty-array strip), and it means the catalog matched nothing.
    200, `hits: []`.
  * **A malformed response is NOT zero matches.** `total > 0` with no `searchResults`, or a
    body that is not JSON, means the transport failed. `DiscoveryUnavailable`, 503.
  * **An outage is NOT zero matches.** A server that will not start, a call that times out,
    a session that died. `DiscoveryUnavailable`, 503 + Retry-After.
  * **A missing dependency is NOT an outage.** Retrying cannot install an extra.
    `DiscoveryNotConfigured`, 501, no Retry-After.

The split between the last two mirrors `llm.ProviderUnavailable` / `ProviderRefused`
exactly, and for the identical reason: a caller can act on one of them and not the other,
and telling someone to wait out a configuration problem wastes their time on Attest's
behalf.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

__all__ = [
    "ADVISORY_NOTE",
    "Discovery",
    "DiscoveryError",
    "DiscoveryNotConfigured",
    "DiscoveryResult",
    "DiscoveryUnavailable",
    "SearchHit",
]

# What every response says about itself, on every call. Not a caveat in the docs: a field on
# the wire, the same instinct as `RetrievalView` reporting WHERE each predicate was applied
# (§11). A reader who is shown catalog entries by an auditor may reasonably assume the
# auditor has checked something about them. Nothing here has been checked.
ADVISORY_NOTE = (
    "Discovery only, and nothing here is evidence. These candidates come from the DataHub "
    "MCP Server, which Attest does not read verdicts through. A URN you pick is verified "
    "separately, over GraphQL, against the catalog, by deterministic checkers - and only "
    "once you have written a claim about it."
)


class DiscoveryError(RuntimeError):
    """Discovery could not answer. It is never an empty result."""


class DiscoveryUnavailable(DiscoveryError):
    """The MCP server did not answer, or answered something unusable. TRANSIENT.

    A server that would not start, a call that timed out, a session that died mid-flight, a
    tool that returned `isError`, or a payload that is not the shape the server documents.
    Retrying may work, so the route says so with a number (`Retry-After`).

    **This class is what stops an outage reading as "the catalog holds nothing".** Attest is
    the tool that exists to say absence is not an answer; a discovery path that returned an
    empty list when it could not ask would be committing this project's cardinal sin in its
    own output, one transport further out than §18 and §20 already found it twice.

    Attest adds NO retry of its own around it. There is no transport-level retry underneath
    to complement (unlike the OpenAI SDK's, §18), so one failure is one failure, reported
    honestly and fast, and the human at the keyboard decides whether to type again.
    """


class DiscoveryNotConfigured(DiscoveryError):
    """Discovery is switched off, or its optional dependency is not installed.

    Deliberately NOT `DiscoveryUnavailable`. Waiting changes nothing: `mcp` is an optional
    extra (`pip install -e '.[mcp]'`) that CI never installs, so a deployment without it is a
    deployment that does not have this feature — a fact, not an outage. Same reasoning as
    `llm.ProviderRefused` being a 502 and pointedly not a 503.
    """


@dataclass(frozen=True)
class SearchHit:
    """One candidate dataset. A URN, and a display name that is not evidence.

    **`urn` is the only field anything downstream may use**, and it is the only field the
    MCP transport returns losslessly. `name` is the server's `properties.name` — a DISPLAY
    string, shown to a human so they can tell two URNs apart, and it is empty when the
    catalog has no name for the entity rather than being invented from the URN.

    There is deliberately no `platform`, `description`, `tags` or `owner` field. The search
    fragment does not return a platform for a Dataset (measured: `SearchEntityInfo` asks a
    Dataset for `properties { name }` and nothing else), and the fields it does return for
    other entity types are exactly the ones §12 measured as flattened. A field Attest cannot
    get losslessly is a field Attest does not publish. The UI derives a platform from the
    URN, which is parsing an identifier rather than trusting a compacted one.
    """

    urn: str
    name: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    """Candidates, and an honest account of where they came from.

    `advisory` is a constant `True` rather than a computed flag, and it is on the wire rather
    than in the docs. It is the same argument as `Retrieval` (§11): a caller who cannot see
    that this came from an unverified transport would reasonably read a list of datasets
    returned by an auditor as a list of datasets the auditor has checked something about.
    """

    hits: tuple[SearchHit, ...] = ()
    # The catalog's OWN total for the query, round-tripped rather than discarded — the same
    # rule as `Retrieval.total` (Session 21 gap 1). `total > len(hits)` means the picker is
    # showing a page, and a dataset past it is absent from the LISTING, not from the catalog.
    total: int = 0
    transport: str = ""
    # The MCP server that answered, as it identified itself at `initialize` (e.g.
    # "datahub v3.4.5"). Reported, never asserted on: it is provenance for a human, and the
    # tripwire that says which server produced a list if one ever looks wrong.
    #
    # NOT the pinned version, and do not "fix" it to agree with one. `settings.mcp_command`
    # pins the PyPI package (`mcp-server-datahub==0.6.0`); this string is what the handshake
    # announces, which is the **fastmcp framework** version under the name the server
    # registers itself as. Measured: the same pinned 0.6.0 server reported `v3.4.4` in
    # Session 17 and `v3.4.5` today, because fastmcp moved and the server did not. This is
    # the live measurement; the pin is a launch fact and lives in the config, which is also
    # the only place it stays true when someone overrides the command.
    server: str = ""
    advisory: bool = True
    note: str = field(default=ADVISORY_NOTE)


class Discovery(Protocol):
    """The one method the API layer needs. Keeps the fake trivial, as `cache.Reader` does.

    Deliberately one method and no lifecycle: a route asks a question and gets candidates or
    a named failure. Starting, holding and tearing down a session is `mcp.McpDiscovery`'s
    problem and nobody else's.
    """

    def search(self, text: str, limit: int = 10) -> DiscoveryResult: ...
