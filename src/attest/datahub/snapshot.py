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

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


def _urns(container: Any, key: str, inner: str) -> tuple[str, ...] | None:
    """Pull URNs out of DataHub's `{key: [{inner: {urn}}]}` association shape.

    Returns None when the aspect is absent — which is *not* the same as an aspect
    that is present and empty, and the caller is entitled to know which.
    """
    if container is None:
        return None
    items = container.get(key)
    if items is None:
        return None
    return tuple((item.get(inner) or {}).get("urn", "") for item in items)


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

    @classmethod
    def from_graphql(cls, data: dict[str, Any]) -> DatasetSnapshot:
        props = data.get("properties") or {}

        # DatasetProperties.lastModified is epoch millis. Absent -> the catalog does
        # not know when this last changed, which is not a claim that it is stale.
        raw_time = (props.get("lastModified") or {}).get("time")
        last_modified = (
            datetime.fromtimestamp(raw_time / 1000, tz=UTC) if raw_time else None
        )

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
        )
