"""A normalized read model of one dataset, as the catalog currently holds it.

Checkers consume this, never raw GraphQL. Two reasons.

**It pins down the difference between "absent" and "empty".** The whole
Insufficient-Coverage verdict rests on that distinction, and raw GraphQL states it
badly: an absent aspect is `null`, but so is a present aspect with nothing in it,
and both arrive as the same `None` after a couple of `or {}` fallbacks. Here,
`None` means the catalog has no such aspect and an empty tuple means the aspect
exists but holds nothing. Both yield Insufficient-Coverage — an unowned table is
unowned either way — but a checker reporting evidence should be able to say which
it saw, and this is the only layer that still knows.

**It keeps the raw response shape out of the checkers.** Every field here is
reachable from a real DataHub query, so nothing downstream can be written against
a shape that does not exist.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


def _urns(container: Any, key: str, inner: str) -> tuple[str, ...] | None:
    """Pull URNs out of DataHub's `{key: [{inner: {urn}}]}` association shape.

    Three outcomes, and they must stay three (Session 23, Hole 2):

      * absent aspect (`None`) -> `None`. The catalog is SILENT.
      * present-but-empty list (`[]`) -> `()`. The aspect exists and lists nobody. A
        VALID state — an unowned dataset, an untagged one — and Insufficient-Coverage.
      * present-but-BROKEN entry (an item with no usable URN) -> RAISE. A structurally
        broken response is neither absent nor empty, and normalizing it to the
        empty-string URN `''` — which is what this used to do — produced a
        populated-LOOKING list of garbage that drove a confident wrong verdict. A
        malformed response is not data; `client.fetch_dataset` turns this ValueError
        into a MalformedResponseError, which resolve surfaces as a ClaimError.

    The empty-vs-broken line is exact: `[]` never enters the loop and returns `()`;
    only a present entry that carries no URN raises. Absence-is-not-empty extended to
    absence-is-not-malformed.
    """
    if container is None:
        return None
    items = container.get(key)
    if items is None:
        return None
    urns: list[str] = []
    for item in items:
        urn = ((item or {}).get(inner) or {}).get("urn")
        if not urn:
            raise ValueError(
                f"malformed catalog response: an entry under {key!r} carries no {inner!r} "
                f"URN ({item!r})"
            )
        urns.append(urn)
    return tuple(urns)


def _term_parents(container: Any) -> dict[str, tuple[str, ...]]:
    """term URN -> the glossary nodes it sits under.

    The hierarchy is what makes a term a *classification signal* rather than a bare
    label: EmailAddress is a PII signal because someone filed it under the PII node,
    not because the string looks personal. Attest reads the structure and infers
    nothing — see policy.PII_SIGNALS.
    """
    parents: dict[str, tuple[str, ...]] = {}
    for assoc in (container or {}).get("terms") or []:
        term = assoc.get("term") or {}
        urn = term.get("urn")
        if not urn:
            continue
        nodes = (term.get("parentNodes") or {}).get("nodes") or []
        parents[urn] = tuple(n.get("urn", "") for n in nodes)
    return parents


class FieldSnapshot(BaseModel):
    """One column, with whatever the catalog says about it."""

    model_config = ConfigDict(frozen=True)

    path: str
    # DataHub carries two type notions and a claim may reasonably use either:
    # `native_type` is the platform's own spelling ("VARCHAR(36)"), `data_type` is
    # DataHub's abstract enum ("STRING"). Keeping both lets "customer_id is a string"
    # be checked deterministically instead of needing a dialect-mapping model.
    native_type: str | None = None
    data_type: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    terms: tuple[str, ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        """Tags and glossary terms together — classification claims range over both."""
        return self.tags + self.terms


class DatasetSnapshot(BaseModel):
    """What the catalog holds about one dataset, at read time.

    `None` on an aspect means the catalog is SILENT about it. Empty means the aspect
    exists but is unpopulated. Never conflate either with a contradiction.
    """

    model_config = ConfigDict(frozen=True)

    urn: str
    name: str | None = None
    platform: str | None = None
    description: str | None = None

    last_modified: datetime | None = None
    owners: tuple[str, ...] | None = None
    tags: tuple[str, ...] | None = None
    terms: tuple[str, ...] | None = None
    fields: tuple[FieldSnapshot, ...] | None = None

    # Custom properties carry upstream classifiers' findings — `hasPII` is one of the
    # three signals in policy.PII_SIGNALS. None means the properties aspect is absent.
    custom_properties: dict[str, str] | None = None
    # Every glossary term seen on this dataset, at either grain, mapped to the nodes it
    # sits under. Kept at snapshot level because a column's terms and the table's terms
    # resolve against the same glossary.
    term_parents: dict[str, tuple[str, ...]] = {}

    @property
    def identity(self) -> str:
        """A content hash of this snapshot: WHICH catalog state a verdict was decided against.

        A verdict means "this held against the catalog as it stood when this run read it"
        (architecture.md, the snapshot-cache consistency boundary). Session 21 gap 2 carries
        that identity into the published artifact, so an inherited verdict can say which state
        it pertains to rather than floating free of the catalog it was checked against.

        Content-addressed and canonical (`sort_keys`), so the same catalog state hashes the
        same. It is captured at audit time and STORED — never recomputed at write-back, which
        would re-fetch a catalog that may have moved and make the stored identity a lie (§2c,
        the same-snapshot rule the correction loop already obeys).
        """
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def labels(self) -> tuple[str, ...]:
        """Table-level tags and terms together."""
        return (self.tags or ()) + (self.terms or ())

    @property
    def has_classification(self) -> bool:
        """Has anyone classified this table at all?

        False is the licence to return Insufficient-Coverage: nobody has reviewed
        this table, so it can neither confirm nor deny that it holds PII.
        """
        return bool(self.labels)

    def field(self, path: str) -> FieldSnapshot | None:
        """A column by name, or None if the schema does not list it.

        Callers MUST check `self.fields is None` first. This returning None means
        two very different things otherwise: "the schema says there is no such
        column" (a contradiction) versus "there is no schema" (silence).
        """
        for f in self.fields or ():
            if f.path == path:
                return f
        return None

    def terms_under(self, node_urn: str, labels: tuple[str, ...]) -> tuple[str, ...]:
        """Which of `labels` are glossary terms filed under `node_urn`.

        The answer comes from the catalog's own hierarchy, never from the term's name.
        """
        return tuple(
            label for label in labels if node_urn in self.term_parents.get(label, ())
        )

    @classmethod
    def from_graphql(cls, data: dict[str, Any]) -> DatasetSnapshot:
        props = data.get("properties") or {}

        # DatasetProperties.lastModified is epoch millis. Absent -> the catalog does
        # not know when this last changed, which is not a claim that it is stale.
        raw_time = (props.get("lastModified") or {}).get("time")
        last_modified = (
            datetime.fromtimestamp(raw_time / 1000, tz=UTC) if raw_time else None
        )

        # The glossary hierarchy, gathered from every grain the terms appear at. A term
        # on a column resolves against the same node as the same term on the table.
        term_parents = _term_parents(data.get("glossaryTerms"))

        schema = data.get("schemaMetadata")
        fields: tuple[FieldSnapshot, ...] | None = None
        if schema is not None:
            fields = tuple(
                FieldSnapshot(
                    path=f["fieldPath"],
                    native_type=f.get("nativeDataType"),
                    data_type=f.get("type"),
                    description=f.get("description"),
                    tags=_urns(f.get("globalTags"), "tags", "tag") or (),
                    terms=_urns(f.get("glossaryTerms"), "terms", "term") or (),
                )
                for f in (schema.get("fields") or [])
            )
            for f in schema.get("fields") or []:
                term_parents.update(_term_parents(f.get("glossaryTerms")))

        # DataHub returns customProperties as [{key, value}]. Absent aspect -> None,
        # because "nobody has written properties here" and "the property says nothing"
        # are different, and only this layer still knows which it saw.
        raw_props = props.get("customProperties")
        custom_properties = (
            {p["key"]: p.get("value", "") for p in raw_props}
            if raw_props is not None
            else None
        )

        return cls(
            urn=data["urn"],
            name=data.get("name"),
            platform=(data.get("platform") or {}).get("name"),
            description=props.get("description"),
            last_modified=last_modified,
            owners=_urns(data.get("ownership"), "owners", "owner"),
            tags=_urns(data.get("tags"), "tags", "tag"),
            terms=_urns(data.get("glossaryTerms"), "terms", "term"),
            fields=fields,
            custom_properties=custom_properties,
            term_parents=term_parents,
        )
