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


class DataHubError(RuntimeError):
    """A GraphQL request failed, or returned errors."""


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
        glossaryTerms { terms { term { urn properties { name } } } }
        schemaMetadata {
          fields {
            fieldPath
            type
            nativeDataType
            description
            globalTags { tags { tag { urn } } }
            glossaryTerms { terms { term { urn } } }
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
        """Fetch a dataset's schema, ownership, tags, terms, and properties."""
        return self.execute(self.DATASET_QUERY, {"urn": urn}).get("dataset")

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
        if isinstance(value, (int, float)):
            return {"numberValue": float(value)}
        return {"stringValue": str(value)}

    def set_structured_property(
        self, asset_urn: str, property_urn: str, values: list[Any]
    ) -> dict[str, Any]:
        """Write a structured property value onto an asset.

        This is the write path Attest's verdicts will eventually travel: an
        audit result attached to the dataset it is about, queryable afterwards.
        """
        return self.execute(
            self.UPSERT_STRUCTURED_PROPERTIES,
            {
                "assetUrn": asset_urn,
                "params": [
                    {
                        "structuredPropertyUrn": property_urn,
                        "values": [self._property_value(v) for v in values],
                    }
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
        return self.execute(self.STRUCTURED_PROPERTY_QUERY, {"urn": urn}).get("entity")

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
