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

import time
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

    # --- claim artifacts (assertions) --------------------------------------
    #
    # A claim artifact is a CUSTOM Assertion plus its appended run events. The assertion is
    # the CLAIM (stable: what is asserted about what); each run event is the VERDICT at a
    # point in time (append-only). Structured properties cannot hold this: they are
    # last-write-wins, so two claims about one dataset overwrite each other and the catalog
    # ends up holding a verdict with no subject. See docs/design/claim-artifact.md.

    UPSERT_CUSTOM_ASSERTION = """
    mutation upsertCustomAssertion($urn: String, $input: UpsertCustomAssertionInput!) {
      upsertCustomAssertion(urn: $urn, input: $input) {
        urn
        info {
          type description externalUrl
          customAssertion { type entityUrn logic field { path } }
        }
      }
    }
    """

    def upsert_custom_assertion(
        self,
        urn: str,
        entity_urn: str,
        custom_type: str,
        description: str,
        platform_urn: str,
        logic: str,
        external_url: str = "",
    ) -> dict[str, Any]:
        """Create or update ONE claim artifact, at a URN THE CALLER OWNS.

        The `urn` argument is what makes the whole design work: Attest derives it from the
        claim's own content, so re-writing the same claim lands on the same artifact and two
        different claims about one dataset coexist. A server-minted id would diverge on
        every retry.

        **`fieldPath` IS DELIBERATELY NOT EXPOSED, AND MUST NEVER BE SENT.** Setting it makes
        DataHub record the assertion's `asserteeUrn` as a *schemaField* URN, while the
        `assertionRunEvent` aspect requires a *dataset* URN — so the artifact can be created
        and read back perfectly and can then NEVER carry a verdict:

            Invalid entity type urn validation failure (Required: [dataset]).
            Path: /asserteeUrn  Urn: urn:li:schemaField:(...,email)

        Worse, whether it fails depends on what the index holds at that instant, so it
        sometimes appears to work. The column grain travels in `logic` instead, where it is
        fully recoverable and costs nothing. Not passing it is not an oversight; it is the
        fix, and it is enforced by this method having no parameter for it.
        """
        payload: dict[str, Any] = {
            "entityUrn": entity_urn,
            "type": custom_type,
            "description": description,
            "platform": {"urn": platform_urn},
            "logic": logic,
        }
        if external_url:
            payload["externalUrl"] = external_url
        return self.execute(
            self.UPSERT_CUSTOM_ASSERTION, {"urn": urn, "input": payload}
        )["upsertCustomAssertion"]

    REPORT_ASSERTION_RESULT = """
    mutation reportAssertionResult($urn: String!, $result: AssertionResultInput!) {
      reportAssertionResult(urn: $urn, result: $result)
    }
    """

    # GMS resolves the assertion's assertee through an eventually-consistent index, so a
    # result reported immediately after the artifact is created is refused with this,
    # verbatim, until the index catches up. It is pure latency, not a real rejection.
    _ASSERTION_NOT_INDEXED = "not associated with any entity"

    def report_assertion_result(
        self,
        urn: str,
        result_type: str,
        timestamp_millis: int,
        properties: dict[str, str] | None = None,
        external_url: str = "",
        retry_seconds: float = 30.0,
    ) -> bool:
        """Append ONE verdict event to a claim artifact.

        `timestamp_millis` IS THE KEY, and it must be the audit run's own timestamp rather
        than the wall clock. A timeseries row is keyed by (urn, aspect, timestampMillis), so
        the same event reported twice at the same timestamp collapses to ONE row — which is
        what makes a retry safe. Pass `now()` and every retry mints a new key, appends a
        duplicate, and corrupts an append-only history with an audit that never happened.
        Callers cannot get this wrong by accident: writeback.py has no default for it.

        Retries past the index lag (see `_ASSERTION_NOT_INDEXED`) and gives up honestly:
        `False` means the verdict did not land, and the caller reports that rather than
        assuming it did. Any other GraphQL error is a real refusal and is raised.
        """
        result: dict[str, Any] = {
            "timestampMillis": timestamp_millis,
            "type": result_type,
            "properties": [
                {"key": k, "value": v} for k, v in (properties or {}).items()
            ],
        }
        if external_url:
            result["externalUrl"] = external_url

        deadline = time.monotonic() + retry_seconds
        while True:
            try:
                return bool(
                    self.execute(
                        self.REPORT_ASSERTION_RESULT, {"urn": urn, "result": result}
                    )["reportAssertionResult"]
                )
            except DataHubError as exc:
                if self._ASSERTION_NOT_INDEXED not in str(exc):
                    raise
                if time.monotonic() >= deadline:
                    raise DataHubError(
                        f"the catalog did not index {urn} within {retry_seconds:.0f}s, so "
                        f"its verdict could not be reported: {exc}"
                    ) from exc
                time.sleep(1.0)

    ASSERTION_QUERY = """
    query assertion($urn: String!) {
      assertion(urn: $urn) {
        urn
        info {
          description externalUrl
          customAssertion { type entityUrn logic field { path } }
        }
        tags { tags { tag { urn } } }
        runEvents(status: COMPLETE, limit: 50) {
          total failed succeeded
          runEvents {
            timestampMillis
            result { type externalUrl nativeResults { key value } }
          }
        }
      }
    }
    """

    def get_assertion(self, urn: str) -> dict[str, Any] | None:
        """One claim artifact and its whole verdict history, or None if it is not there."""
        return self.execute(self.ASSERTION_QUERY, {"urn": urn}).get("assertion")

    DATASET_ASSERTIONS_QUERY = """
    query datasetAssertions($urn: String!, $start: Int!, $count: Int!) {
      dataset(urn: $urn) {
        assertions(start: $start, count: $count) {
          total
          assertions {
            urn
            info {
              description externalUrl
              customAssertion { type entityUrn logic field { path } }
            }
            tags { tags { tag { urn } } }
            runEvents(status: COMPLETE, limit: 50) {
              total failed succeeded
              runEvents {
                timestampMillis
                result { type externalUrl nativeResults { key value } }
              }
            }
          }
        }
      }
    }
    """

    def list_dataset_assertions(
        self, dataset_urn: str, start: int = 0, count: int = 50
    ) -> list[dict[str, Any]]:
        """Every claim artifact on a dataset, with its verdict history, in ONE query.

        THE THESIS QUERY. This is what a second agent inherits from DataHub alone — every
        claim ever made about this dataset, what it asserted, and every verdict it has had.
        Note it is a relationship read off the dataset, NOT a search: search cannot scope
        assertions to a dataset at all (no assertee field is indexed), so this is the only
        way to ask the question. See docs/design/claim-artifact.md §5.
        """
        dataset = self.execute(
            self.DATASET_ASSERTIONS_QUERY,
            {"urn": dataset_urn, "start": start, "count": count},
        ).get("dataset")
        if not dataset:
            return []
        return list((dataset.get("assertions") or {}).get("assertions") or [])

    # --- tags ---------------------------------------------------------------

    CREATE_TAG = """
    mutation createTag($input: CreateTagInput!) { createTag(input: $input) }
    """
    ADD_TAG = """
    mutation addTag($input: TagAssociationInput!) { addTag(input: $input) }
    """
    REMOVE_TAG = """
    mutation removeTag($input: TagAssociationInput!) { removeTag(input: $input) }
    """

    def create_tag(self, tag_id: str, name: str, description: str = "") -> str:
        return self.execute(
            self.CREATE_TAG,
            {"input": {"id": tag_id, "name": name, "description": description}},
        )["createTag"]

    def add_tag(self, tag_urn: str, resource_urn: str) -> bool:
        return bool(
            self.execute(
                self.ADD_TAG, {"input": {"tagUrn": tag_urn, "resourceUrn": resource_urn}}
            )["addTag"]
        )

    def remove_tag(self, tag_urn: str, resource_urn: str) -> bool:
        return bool(
            self.execute(
                self.REMOVE_TAG,
                {"input": {"tagUrn": tag_urn, "resourceUrn": resource_urn}},
            )["removeTag"]
        )

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
