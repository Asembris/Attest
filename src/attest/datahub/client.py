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
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import ValidationError

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


class CatalogUnavailable(DataHubError):
    """Attest could not READ the catalog. This is not a fact about the claim.

    THE TAXONOMY HOLE THIS CLOSES, and it is §18's provider bug at the other transport.
    Every other `DataHubError` raised on the read path says something about the ENTITY:
    `EntityNotFoundError` says the URN names nothing, `MalformedResponseError` says the
    answer was structurally broken. Both are facts, both are correctly surfaced as a
    `report.ClaimError` — a malformed question, kept out of the verdict tally.

    A transport failure says nothing about the entity at all. It says Attest never got to
    ask. Collapsed into the same class, a GMS that was merely still booting produced a run
    that settled `awaiting-review` with `trajectory_ok`, ZERO verdicts, and a UI reading
    AUDIT COMPLETE — Attest rendering silence as an answer, in its own output, about its own
    failure. That is the cardinal sin of this project's own thesis, which is exactly what
    §18 fixed for the model provider and what §12 predicted here: *nothing defends the
    catalog READ, because the read was the one thing that could be trusted to mean what it
    said.*

    So this one does NOT become a ClaimError. `graph._resolve` lets it propagate, the run
    dies without a report, and `POST /audit` answers **503 + Retry-After** — the same
    disposition, for the same reason, as `llm.ProviderUnavailable`.

    **The transient line is drawn where httpx draws it** (`httpx.TransportError`: connect,
    read, timeout, proxy, and remote-protocol failures — "the request never produced a
    response"), plus the status codes that mean the server could not serve rather than that
    it answered. Anything else stays a plain `DataHubError`: a GraphQL `errors` payload is a
    real answer to a broken query, and dressing it as an outage would promise a retry that
    cannot help.
    """


class MalformedResponseError(DataHubError):
    """The catalog answered, but the answer was structurally broken (Session 23).

    The entity exists and resolved; one part of the response is unusable — a null
    where a nested object was required, a missing key, a wrong-typed field, an
    association entry carrying no URN. This is the SAME CLASS as entity-not-found and
    for the same reason: the catalog neither agrees with the claim nor is silent about
    it, so it is not a verdict. It is a `DataHubError`, so the resolve path catches it
    and surfaces it as a `report.ClaimError` — kept out of the verdict tally, named in
    `errors`, never scored.

    Why this must raise rather than normalize to empty: a present-but-URL-less owner
    used to become the empty-string URN `''`, i.e. a POPULATED-looking owners list, and
    `check_ownership` then read the claimed owner as "not among the owners" and returned
    a confident Contradicted. Reading a broken response as data drives exactly the
    confident wrong verdict this project exists to prevent. It is the twin of the
    `get_dataset` / `get_structured_property` traps: a read that looks fine and is not.

    NOT raised for a legitimately ABSENT aspect (None) or a PRESENT-EMPTY one (()): an
    unowned dataset is Insufficient-Coverage, and turning that into an error would be
    absence collapsed into malformation — the same sin one grain down.
    """

    def __init__(self, urn: str, cause: object = None) -> None:
        detail = f": {cause}" if cause else ""
        super().__init__(f"Malformed catalog response for {urn}{detail}")
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
        except httpx.TransportError as exc:
            # The request never produced a response. httpx's own name for that class is
            # TransportError, so the line is drawn there rather than hand-enumerated — a
            # list would drift from the library that defines the failures.
            #
            # The observed shape, and the reason this class exists: quickstart's GMS accepts
            # the TCP connection while it is still booting and closes it before answering,
            # which arrives as RemoteProtocolError("Server disconnected without sending a
            # response"). Indistinguishable, before this, from a URN that does not exist.
            raise CatalogUnavailable(
                f"the catalog at {self.gms_url} could not be reached: {exc}"
            ) from exc
        except httpx.HTTPError as exc:
            # Not transient: a malformed URL or an unsupported scheme is configuration, and
            # telling someone to wait for it would waste their time on Attest's behalf.
            raise DataHubError(f"GraphQL transport error: {exc}") from exc

        if response.status_code != httpx.codes.OK:
            if response.status_code >= 500 or response.status_code in (408, 429):
                # The server could not serve the request. GMS answers a GraphQL problem with
                # 200 and an `errors` payload, so a 5xx here is the server itself, never the
                # query — and never an answer about the entity.
                raise CatalogUnavailable(
                    f"the catalog at {self.gms_url} returned "
                    f"{response.status_code}: {response.text[:500]}"
                )
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
              # OwnerType is the union CorpUser | CorpGroup, and BOTH arms are needed: an
              # arm that matches nothing contributes an empty object, so a group-owned
              # dataset used to arrive as {"owner": {}} and was refused as malformed --
              # correctly failing closed, with a diagnosis that blamed the response when
              # the response was fine and this query was incomplete. Measured on an
              # external catalog: 15 of 67 datasets unauditable (CLAUDE.md §23).
              #
              # The group arm asks for `urn` and NOTHING ELSE, deliberately. An owner's
              # canonical identity is its URN; a display name is not an identity, and a
              # parser cannot compare what the query never fetched.
              ... on CorpUser { urn properties { displayName email } }
              ... on CorpGroup { urn }
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

        A structurally broken response is caught HERE and re-raised as
        MalformedResponseError — a DataHubError — so it takes the same off-ramp as a
        missing entity (resolve catches DataHubError -> ClaimError) instead of either
        crashing the run with a raw KeyError/AttributeError/ValidationError or, worse,
        being normalized into a garbage snapshot that drives a confident verdict. The
        catch is DELIBERATELY the narrow set of exceptions a parse of a broken shape
        produces (a missing key, a wrong type, an association entry with no URN); it is
        never broadened to bare Exception, which would swallow real bugs in the parse.
        `from_graphql` raises ValueError on the present-but-URL-less association entry,
        which is why it is in the set.
        """
        dataset = self.get_dataset(urn)
        if dataset is None:
            raise EntityNotFoundError(urn)
        try:
            return DatasetSnapshot.from_graphql(dataset)
        except (KeyError, AttributeError, TypeError, ValueError, ValidationError) as exc:
            raise MalformedResponseError(urn, exc) from exc

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

    UPDATE_STRUCTURED_PROPERTY = """
    mutation updateStructuredProperty($input: UpdateStructuredPropertyInput!) {
      updateStructuredProperty(input: $input) { urn }
    }
    """

    def update_structured_property(
        self, urn: str, display_name: str = "", description: str = ""
    ) -> dict[str, Any]:
        """Change what an ALREADY-DEFINED property says about itself.

        `createStructuredProperty` refuses a property that exists, so without this a
        definition written once was frozen forever — and every later correction to its text
        lived in the code, described a surface nobody could see, and failed silently.
        Measured in Session 16: `attest.verdict`'s live description in the catalog was the
        string `"test"`, from whichever early run created it, while writeback.py had
        carried three careful sentences past two sessions that thought they had fixed it.

        Only the fields given are sent: `updateStructuredProperty` takes a partial input, so
        omitting `newAllowedValues`/`newEntityTypes` leaves them alone rather than clearing
        them.
        """
        payload: dict[str, Any] = {"urn": urn}
        if display_name:
            payload["displayName"] = display_name
        if description:
            payload["description"] = description
        return self.execute(self.UPDATE_STRUCTURED_PROPERTY, {"input": payload})[
            "updateStructuredProperty"
        ]

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

    # The server-side page size for the pagination loops below. `dataset.assertions` and
    # `searchAcrossEntities` both take start/count and BOTH return `total` — which is the
    # whole fix for gap 1 (Session 21): the total was always in the response, and discarding
    # it made a claim past the first page SILENTLY absent, indistinguishable from "no such
    # claim". That collapse — absence read as an answer — is the exact sin this project
    # exists to catch. So these paginate to `limit` and return the server's `total` beside
    # the nodes, and a caller with `len(nodes) < total` KNOWS the listing was truncated.
    _ASSERTION_PAGE = 50

    def list_dataset_assertions(
        self, dataset_urn: str, limit: int | None = 50
    ) -> tuple[list[dict[str, Any]], int]:
        """Claim artifacts on a dataset, up to `limit`, PLUS the catalog's own total.

        THE THESIS QUERY. This is what a second agent inherits from DataHub alone — every
        claim ever made about this dataset, what it asserted, and every verdict it has had.
        Note it is a relationship read off the dataset, NOT a search: search cannot scope
        assertions to a dataset at all (no assertee field is indexed), so this is the only
        way to ask the question. See docs/design/claim-artifact.md §5.

        Paginates start/count until it has `limit` nodes or the dataset is exhausted, and
        returns `(nodes, total)`. `limit=None` fetches every claim on the dataset — the
        honest reading of "everything the next agent inherits". The `total` is round-tripped
        so a truncated page (`len(nodes) < total`) is never mistaken for a complete one.
        """
        nodes: list[dict[str, Any]] = []
        total = 0
        start = 0
        while limit is None or len(nodes) < limit:
            count = (
                self._ASSERTION_PAGE
                if limit is None
                else min(self._ASSERTION_PAGE, limit - len(nodes))
            )
            dataset = self.execute(
                self.DATASET_ASSERTIONS_QUERY,
                {"urn": dataset_urn, "start": start, "count": count},
            ).get("dataset")
            if not dataset:
                break
            block = dataset.get("assertions") or {}
            total = block.get("total", total)
            page = list(block.get("assertions") or [])
            nodes.extend(page)
            start += len(page)
            if not page or start >= total:
                break
        return nodes, total

    SEARCH_ASSERTIONS = """
    query searchAssertions($orFilters: [AndFilterInput!], $start: Int!, $count: Int!) {
      searchAcrossEntities(
        input: {
          types: [ASSERTION]
          query: "*"
          start: $start
          count: $count
          orFilters: $orFilters
        }
      ) {
        total
        searchResults {
          entity {
            urn
            ... on Assertion {
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
    }
    """

    def search_assertions(
        self,
        custom_types: Sequence[str] = (),
        tags: Sequence[str] = (),
        limit: int | None = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Claim artifacts across the WHOLE catalog, filtered in the index.

        The second of the two server-side entry points, and it is DISJOINT from the first:
        this one FILTERS but cannot SCOPE, while `list_dataset_assertions` scopes but cannot
        filter. They do not compose, so a caller who wants both gets one server-side and the
        other applied by Attest — which `retrieval.py` reports rather than papers over.

        Only `customType` and `tags` are indexed on an assertion (measured — `entityUrn`,
        `asserteeUrn`, `datasetUrn`, `fieldPath`, `description` and five more all return
        total=0 against a baseline that returns everything). So claim type travels in
        `customType` and the latest verdict travels as a tag; there is no third filter to
        add here, and a reviewer or a time window CANNOT be pushed down at all.

        Both filters accept multiple values and OR within a field, AND across fields — so
        "every Attest claim" is one query naming all four claim types, and "contradicted
        ownership claims" is one query naming one of each.

        Paginates start/count to `limit` like `list_dataset_assertions`, and returns
        `(nodes, total)` so a truncated result set is never read as a complete one. The run
        events come back INLINE, so each page is one round trip rather than a fan-out.
        """
        conditions: list[dict[str, Any]] = []
        if custom_types:
            conditions.append({"field": "customType", "values": list(custom_types)})
        if tags:
            conditions.append({"field": "tags", "values": list(tags)})
        # No conditions is a legitimate ask ("every assertion") and `orFilters: []` is not
        # how you say it -- an empty AND matches nothing. Omit the filter entirely.
        or_filters = [{"and": conditions}] if conditions else None

        nodes: list[dict[str, Any]] = []
        total = 0
        start = 0
        while limit is None or len(nodes) < limit:
            count = (
                self._ASSERTION_PAGE
                if limit is None
                else min(self._ASSERTION_PAGE, limit - len(nodes))
            )
            result = self.execute(
                self.SEARCH_ASSERTIONS,
                {"orFilters": or_filters, "start": start, "count": count},
            )["searchAcrossEntities"]
            total = result.get("total", total)
            search_results = result.get("searchResults") or []
            nodes.extend(r["entity"] for r in search_results if r.get("entity"))
            # Advance by what the server returned, not by kept entities, so a result with no
            # entity node cannot stall the loop.
            start += len(search_results)
            if not search_results or start >= total:
                break
        return nodes, total

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
