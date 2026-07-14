"""Writing an approved verdict back to DataHub, as a structured property.

The goal is one sentence long: **"show me every contradicted ownership claim caught this
week" should be a DataHub query.** Not a text blob on a description field that a human has
to read, and not a tag whose name encodes four things. Structured properties are typed,
indexed, and filterable, so a verdict written this way is queryable in the catalog's own
search — which is the difference between an audit result that lives in the catalog and one
that merely visited it.

--------------------------------------------------------------------------------
What is written, and the one field that deliberately is not
--------------------------------------------------------------------------------

Five properties, each a separate filterable field:

    attest.verdict       Supported | Contradicted | Insufficient-Coverage
    attest.claim_type    freshness | ownership | classification | schema
    attest.checked_at    ISO-8601, when the audit ran
    attest.source_agent  who made the claim
    attest.audit_run     the run id — the join key back into Attest's own store

There is no `attest.confidence`, and its absence is a decision rather than an oversight.
Attest's verdicts come from deterministic code: date arithmetic, set membership, string
comparison. There IS no confidence — the catalog either says a thing or it does not, and
the third verdict already carries the only uncertainty in the system, which is uncertainty
about the *catalog's coverage* rather than about the answer. Writing `confidence: 0.95`
would be inventing a number to look like a machine-learning system, and a fabricated
number reported as a measurement is the exact failure this project exists to catch. If a
future claim type genuinely carries a probability, it gets a field then.

--------------------------------------------------------------------------------
Last-write-wins, and why that is fine HERE and disqualifying THERE
--------------------------------------------------------------------------------

DataHub's structured properties are unversioned: this write replaces the previous verdict
for this dataset and the previous one is gone. That is exactly what you want from a
catalog — *what is true of this dataset now* — and exactly what you cannot build a history
on. So the catalog gets the current verdict, and `attest.audit_run` points back into
Attest's store, which has every run, every piece of evidence, and every human decision
that ever touched this dataset. DataHub is the catalog, not the event store (see store.py).

A consequence worth stating: a dataset with several claims against it in one run carries
the verdict of the LAST one written back. `attest.audit_run` is what lets a reader recover
the rest, and the store is where they are.

--------------------------------------------------------------------------------
Nothing is written until a human approves it
--------------------------------------------------------------------------------

This module is called from exactly one place: the approval path. An audit that nobody
reviewed writes nothing to the catalog, and a rejected correction writes nothing either.
An auditor that silently edits the system it audits has stopped being an auditor, and the
write-back is where that principle stops being philosophy and starts being a network call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from attest.datahub import DataHubClient, DataHubError

log = logging.getLogger(__name__)

NAMESPACE = "attest"


@dataclass(frozen=True)
class PropertyDef:
    """One structured property Attest writes. Defined here so it can be bootstrapped."""

    name: str
    display_name: str
    description: str

    @property
    def qualified_name(self) -> str:
        return f"{NAMESPACE}.{self.name}"

    @property
    def urn(self) -> str:
        return f"urn:li:structuredProperty:{self.qualified_name}"


VERDICT = PropertyDef(
    "verdict",
    "Attest verdict",
    "The latest verdict Attest reached about a claim an agent made about this dataset: "
    "Supported, Contradicted, or Insufficient-Coverage.",
)
CLAIM_TYPE = PropertyDef(
    "claim_type",
    "Attest claim type",
    "Which kind of claim the verdict is about: freshness, ownership, classification, or "
    "schema.",
)
CHECKED_AT = PropertyDef(
    "checked_at",
    "Attest checked at",
    "When the audit that produced this verdict ran, ISO-8601.",
)
SOURCE_AGENT = PropertyDef(
    "source_agent",
    "Attest source agent",
    "The agent whose claim was audited.",
)
AUDIT_RUN = PropertyDef(
    "audit_run",
    "Attest audit run",
    "The id of the audit run that produced this verdict. DataHub holds the latest verdict; "
    "Attest's own store holds every run, its evidence, and its approvals. This is the key "
    "that joins them.",
)

PROPERTIES: tuple[PropertyDef, ...] = (
    VERDICT,
    CLAIM_TYPE,
    CHECKED_AT,
    SOURCE_AGENT,
    AUDIT_RUN,
)


@dataclass(frozen=True)
class WriteResult:
    """What happened when a verdict was pushed to the catalog.

    `ok=False` carries the reason. An approval whose write-back failed is NOT reported as a
    success — the approval still stands (a human did decide), but the catalog does not yet
    know, and the store records exactly that. A silent failure here would leave the catalog
    disagreeing with the audit history and nobody any the wiser.
    """

    ok: bool
    target_urn: str
    detail: str = ""

    def __str__(self) -> str:
        return f"written to {self.target_urn}" if self.ok else f"failed: {self.detail}"


def ensure_definitions(client: DataHubClient) -> tuple[str, ...]:
    """Define Attest's structured properties in DataHub if they are not there yet.

    Attest bootstraps its own write surface. The ingestion CLI is for seed data, and a
    service that cannot write its results without someone first running a recipe by hand is
    not deployable.

    Idempotent: a property that already exists is left alone. Returns what it created.
    """
    created: list[str] = []
    for prop in PROPERTIES:
        if client.get_structured_property(prop.urn) is not None:
            continue
        client.create_structured_property(
            qualified_name=prop.qualified_name,
            display_name=prop.display_name,
            description=prop.description,
        )
        created.append(prop.qualified_name)
        log.info("defined structured property %s", prop.qualified_name)
    return tuple(created)


def write_verdict(
    client: DataHubClient,
    target_urn: str,
    verdict: str,
    claim_type: str,
    run_id: str,
    source_agent: str = "",
    checked_at: datetime | None = None,
) -> WriteResult:
    """Attach an approved verdict to its dataset. One mutation, all five fields.

    Never called on its own initiative — see the module docstring. A DataHub failure is
    returned, not raised: the approval is a human decision and it happened, whatever the
    catalog did with it afterwards.
    """
    when = (checked_at or datetime.now(tz=UTC)).isoformat()
    try:
        ensure_definitions(client)
        client.set_structured_properties(
            target_urn,
            {
                VERDICT.urn: [verdict],
                CLAIM_TYPE.urn: [claim_type],
                CHECKED_AT.urn: [when],
                SOURCE_AGENT.urn: [source_agent or "unknown"],
                AUDIT_RUN.urn: [run_id],
            },
        )
    except DataHubError as exc:
        log.error("write-back to %s failed: %s", target_urn, exc)
        return WriteResult(ok=False, target_urn=target_urn, detail=str(exc))

    log.info("wrote %s verdict for %s (run %s)", verdict, target_urn, run_id)
    return WriteResult(ok=True, target_urn=target_urn)


def find_datasets(client: DataHubClient, verdict: str) -> tuple[str, ...]:
    """Datasets whose latest Attest verdict is `verdict`. The query this all exists for.

    Proves the written value is not merely persisted but INDEXED, which is the whole claim
    being made by writing it as a structured property rather than as text.
    """
    result = client.search_by_structured_property(VERDICT.qualified_name, verdict)
    return tuple(
        r["entity"]["urn"] for r in result.get("searchResults") or [] if r.get("entity")
    )
