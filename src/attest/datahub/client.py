"""DataHub GraphQL client.

Attest talks to DataHub over raw GraphQL via httpx. The acryl-datahub SDK is
deliberately not used here: it warns on Python 3.12 and drags in a large
dependency surface. The CLI is still used, but only for seed ingestion.

GraphQL errors are surfaced as DataHubError rather than being swallowed — a
groundedness auditor that silently reads empty metadata would report
"Insufficient-Coverage" when the truth is "the query broke", which is exactly
the failure mode this project exists to prevent.
"""

from __future__ import annotations

from typing import Any

import httpx

from attest.config import settings
from attest.datahub.snapshot import DatasetSnapshot


class DataHubError(RuntimeError):
    """A GraphQL request failed, or returned errors."""


class EntityNotFoundError(DataHubError):
    """The URN does not name anything in the catalog.

    This is an ERROR, not a verdict. A claim about an entity that does not exist is
    not contradicted by the catalog and it is not under-covered by the catalog —
    the question itself was malformed, most likely a bad URN from upstream entity
    resolution. Returning Insufficient-Coverage here would quietly launder a broken
    input into a legitimate-looking audit result, and the bad URN would never be
    seen. So it raises.
    """

    def __init__(self, urn: str) -> None:
        super().__init__(f"No such entity in the catalog: {urn}")
        self.urn = urn


class DataHubClient:
    def __init__(
        self,
        gms_url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.gms_url = (gms_url or settings.datahub_gms_url).rstrip("/")
        self.token = token if token is not None else settings.datahub_token
        headers = {"Content-Type": "application/json"}
        # Local quickstart runs with metadata auth disabled; the header is only
        # sent when a token is actually configured.
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self._client = httpx.Client(
            base_url=self.gms_url, headers=headers, timeout=timeout
        )

    def __enter__(self) -> DataHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def execute(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run a GraphQL query or mutation and return its `data` payload."""
        try:
            response = self._client.post(
                "/api/graphql", json={"query": query, "variables": variables or {}}
            )
        except httpx.HTTPError as exc:
            raise DataHubError(f"GraphQL transport error: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            raise DataHubError(
                f"GraphQL HTTP {response.status_code}: {response.text[:500]}"
            )

        payload = response.json()
        if payload.get("errors"):
            raise DataHubError(f"GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    # --- reads -------------------------------------------------------------

    DATASET_QUERY = """
    query dataset($urn: String!) {
      dataset(urn: $urn) {
        urn
        exists
        name
        platform { name }
        properties {
          description
          lastModified { time }
          customProperties { key value }
        }
        ownership {
          owners {
            owner {
              ... on CorpUser { urn properties { displayName email } }
            }
            ownershipType { urn }
          }
        }
        tags { tags { tag { urn properties { name } } } }
        glossaryTerms {
          terms { term { urn properties { name } parentNodes { nodes { urn } } } }
        }
        schemaMetadata {
          fields {
            fieldPath
            type
            nativeDataType
            description
            globalTags { tags { tag { urn } } }
            glossaryTerms { terms { term { urn parentNodes { nodes { urn } } } } }
          }
        }
        structuredProperties {
          properties {
            structuredProperty { urn definition { displayName valueType { info { type } } } }
            values { ... on StringValue { stringValue } ... on NumberValue { numberValue } }
          }
        }
      }
    }
    """

    def get_dataset(self, urn: str) -> dict[str, Any] | None:
        """Fetch a dataset's schema, ownership, tags, terms, and properties.

        Returns None if no such dataset exists.

        The None has to be computed, not read off the response. DataHub answers
        `dataset(urn:)` for ANY well-formed dataset URN, synthesizing `urn`, `name`,
        and `platform` back out of the URN string itself and nulling every aspect.
        A dataset that was never ingested is therefore byte-identical to a real
        dataset carrying no metadata, and a typo'd URN would sail through every
        checker as Insufficient-Coverage. `exists` is the only field that tells them
        apart, which is why the query asks for it and why this method is the only
        supported way in.
        """
        dataset = self.execute(self.DATASET_QUERY, {"urn": urn}).get("dataset")
        if not dataset or not dataset.get("exists"):
            return None
        return dataset

    def fetch_dataset(self, urn: str) -> DatasetSnapshot:
        """Fetch a dataset as a normalized snapshot, or raise EntityNotFoundError.

        This is what checkers use: a missing entity must stop the check, not be
        scored as one.
        """
        dataset = self.get_dataset(urn)
        if dataset is None:
            raise EntityNotFoundError(urn)
        return DatasetSnapshot.from_graphql(dataset)

    # --- writes ------------------------------------------------------------

    UPSERT_STRUCTURED_PROPERTIES = """
    mutation upsertStructuredProperties(
      $assetUrn: String!
      $params: [StructuredPropertyInputParams!]!
    ) {
      upsertStructuredProperties(
        input: { assetUrn: $assetUrn, structuredPropertyInputParams: $params }
      ) {
        properties {
          structuredProperty { urn }
          values { ... on StringValue { stringValue } ... on NumberValue { numberValue } }
        }
      }
    }
    """

    @staticmethod
    def _property_value(value: Any) -> dict[str, Any]:
        """Wrap a Python value as a GraphQL PropertyValueInput.

        The API takes `{stringValue: ...}` / `{numberValue: ...}` objects, not
        bare scalars; passing a raw string fails with "Expected type 'Map'".
        """
        if isinstance(value, bool):
            raise TypeError("DataHub structured properties have no boolean type")
        if isinstance(value, int | float):
            return {"numberValue": float(value)}
        return {"stringValue": str(value)}

    def set_structured_property(
        self, asset_urn: str, property_urn: str, values: list[Any]
    ) -> dict[str, Any]:
        """Write one structured property value onto an asset."""
        return self.set_structured_properties(asset_urn, {property_urn: values})

    def set_structured_properties(
        self, asset_urn: str, properties: dict[str, list[Any]]
    ) -> dict[str, Any]:
        """Write several structured properties onto an asset, in ONE mutation.

        One call, not one per property, and not for tidiness: a verdict written as five
        separate mutations can half-fail, leaving a dataset carrying Attest's verdict but
        not the run id that would let anyone check it. The aspect is written atomically or
        not at all.

        This is the write path Attest's verdicts travel — an audit result attached to the
        dataset it is about, queryable afterwards. See writeback.py.
        """
        return self.execute(
            self.UPSERT_STRUCTURED_PROPERTIES,
            {
                "assetUrn": asset_urn,
                "params": [
                    {
                        "structuredPropertyUrn": urn,
                        "values": [self._property_value(v) for v in values],
                    }
                    for urn, values in properties.items()
                ],
            },
        )["upsertStructuredProperties"]

    # --- structured property definitions -----------------------------------

    STRUCTURED_PROPERTY_QUERY = """
    query structuredProperty($urn: String!) {
      entity(urn: $urn) {
        urn
        ... on StructuredPropertyEntity {
          definition {
            qualifiedName
            displayName
            description
            cardinality
            valueType { info { type } }
          }
        }
      }
    }
    """

    def get_structured_property(self, urn: str) -> dict[str, Any] | None:
        """A structured property's definition, or None if it is not defined.

        THE SAME TRAP AS `get_dataset`, and it bites harder. DataHub answers
        `entity(urn:)` for ANY well-formed structuredProperty URN, synthesizing the entity
        out of the URN string and handing back a definition whose `qualifiedName` is the
        EMPTY STRING. A property nobody ever created is therefore a non-null object that
        looks, to a truthiness check, exactly like a real one.

        What that costs is not a bad read — it is a bad WRITE. A caller that bootstraps its
        properties by asking "does this exist?" is told yes, skips creating it, and the
        first upsert against it fails deep in GMS with

            Failed to validate MCP ... Unexpected null value found for
            urn:li:structuredProperty:attest.verdict Structured Property Definition.

        which names the property, says nothing about why, and points at the write rather
        than at the read that caused it. So existence is COMPUTED here, from the only field
        that can tell the two apart, and this method is the only supported way in.
        """
        entity = self.execute(self.STRUCTURED_PROPERTY_QUERY, {"urn": urn}).get("entity")
        if not entity or not (entity.get("definition") or {}).get("qualifiedName"):
            return None
        return entity

    CREATE_STRUCTURED_PROPERTY = """
    mutation createStructuredProperty($input: CreateStructuredPropertyInput!) {
      createStructuredProperty(input: $input) { urn }
    }
    """

    def create_structured_property(
        self,
        qualified_name: str,
        display_name: str,
        description: str,
        value_type: str = "urn:li:dataType:datahub.string",
        entity_types: list[str] | None = None,
        cardinality: str = "SINGLE",
    ) -> dict[str, Any]:
        """Define a structured property.

        Attest must be able to bootstrap its own verdict property without the
        CLI — the ingestion path is for seed data only.
        """
        return self.execute(
            self.CREATE_STRUCTURED_PROPERTY,
            {
                "input": {
                    "id": qualified_name,
                    "qualifiedName": qualified_name,
                    "displayName": display_name,
                    "description": description,
                    "valueType": value_type,
                    "entityTypes": entity_types
                    or ["urn:li:entityType:datahub.dataset"],
                    "cardinality": cardinality,
                }
            },
        )["createStructuredProperty"]

    # --- search ------------------------------------------------------------

    SEARCH_BY_STRUCTURED_PROPERTY = """
    query search($field: String!, $value: String!) {
      searchAcrossEntities(
        input: {
          types: [DATASET]
          query: "*"
          start: 0
          count: 10
          orFilters: [{ and: [{ field: $field, values: [$value] }] }]
        }
      ) {
        total
        searchResults { entity { urn } }
      }
    }
    """

    def search_by_structured_property(
        self, qualified_name: str, value: str
    ) -> dict[str, Any]:
        """Find datasets whose structured property equals `value`.

        Proves the written value is not just persisted but *indexed* — Attest
        needs to answer "which datasets did we mark Contradicted?" later.
        """
        return self.execute(
            self.SEARCH_BY_STRUCTURED_PROPERTY,
            {"field": f"structuredProperties.{qualified_name}", "value": value},
        )["searchAcrossEntities"]
