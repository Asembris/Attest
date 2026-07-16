"""Can the DataHub MCP server serve a faithful DatasetSnapshot on Core v1.5.0.6?

The Session 17 spike. Challenge 1 names the DataHub MCP Server as the way agents obtain
catalog context; Attest reads via direct GraphQL. This asks whether the MCP server can sit
behind `cache.Reader` — one method, `fetch_dataset(urn) -> DatasetSnapshot` — without the
checkers noticing.

It is a PARITY probe, not a demo. For every seeded dataset it builds the snapshot twice —
once through Attest's GraphQL client, once through the MCP server's `get_entities` tool —
and diffs them field by field. The GraphQL snapshot is the reference because it is what
every measured number in this project was measured against.

The MCP normalizer below is deliberately written to give MCP its BEST SHOT: it reads every
field the tool actually returns and reconstructs as much of the snapshot as the response
physically permits. Where it gives up, the field is not there to be read. That distinction
is the whole point of the spike — a gap this probe reports is a gap in the DATA, not in the
mapping, and a normalizer written any other way would be measuring its own author.

Run: just spike-mcp    (needs live DataHub, uvx, and `pip install mcp` for the client)
Exits non-zero if MCP cannot reproduce the reference snapshot.

`mcp` is deliberately NOT a declared dependency of this package: the finding below is
that Attest does not read the catalog over MCP, so shipping the client in `dependencies`
would be carrying a transport nothing imports. It is a spike requirement, named here
rather than discovered as an ImportError.

--------------------------------------------------------------------------------
THE RESULT: 130 mismatches over 16 datasets. Every dataset fails.
--------------------------------------------------------------------------------

Measured against mcp-server-datahub 0.6.0 (protocol server name `datahub` v3.4.4)
against the pinned Core v1.5.0.6. The server RUNS against Core — it detects OSS
correctly (`is_oss=True`, `is_cloud=False`) and answers every call. The wall is not
compatibility. It is that **the MCP server is built to feed a language model, and
every transformation it makes for that purpose destroys something a deterministic
checker needs**:

  1. `lastModified` IS NEVER RETURNED FOR A DATASET, at any version, by any tool.
     Not a version gate — `gql/entity_details.gql` simply never asks for it inside
     the Dataset fragment (it asks for Dashboard, Chart, Document, BusinessAttribute).
     `search.gql` asks for `properties { name }` and nothing else. So FreshnessClaim —
     one of four claim types, three of the twelve matrix cells — has no input at all.
     MEASURED: 15/16 datasets lose it. (The 16th, pipeline_scratch, is genuinely None
     in the catalog: it is the freshness-silence witness, so it agrees by accident.)

  2. FIELD TAGS AND TERMS ARE FLATTENED TO DISPLAY NAMES, NOT URNs.
     `graphql_helpers._clean_schema_fields` keeps `t["tag"]["properties"]["name"]`,
     so `urn:li:tag:PII` arrives as `"PII"` and `urn:li:glossaryTerm:CustomerIdentifier`
     arrives as `"Customer Identifier"` — with a space. ClassificationClaim.labels are
     URNs by validator, and `term_parents` is keyed by term URN, so a column's term can
     never be joined to the glossary hierarchy that makes it a PII signal. A tag with no
     `properties.name` is dropped silently.

  3. `type` IS COMMENTED OUT of the server's own fragment (`entitySchemaFieldFields`),
     so `FieldSnapshot.data_type` is unreachable. MEASURED: None on every column of
     every dataset.

  4. ABSENT AND EMPTY ARE COLLAPSED. `clean_gql_response` recursively drops `None`
     values, `[]` and `{}` — the single distinction the third verdict rests on. So
     "the catalog has no properties aspect" and "the aspect exists and is empty" arrive
     identically. MEASURED: `custom_properties` is `{}` via GraphQL and `None` via MCP
     on 12/16 datasets.

     Its sharpest form is `term_parents`: `urn:li:glossaryTerm:CustomerIdentifier` has
     NO parent nodes — it is deliberately outside the PII node (see policy.PII_SIGNALS,
     and CLAUDE.md §6: "EmailAddress is a PII signal because it is filed under the PII
     node; CustomerIdentifier is deliberately outside it"). Its `parentNodes.nodes` is
     `[]`, so the empty-array strip deletes the term from `term_parents` entirely. The
     evidence that a term is deliberately NOT PII is exactly the evidence that gets
     dropped, because "not PII" is encoded as emptiness.

  5. SCHEMAS ARE TRUNCATED TO A TOKEN BUDGET (`ENTITY_SCHEMA_TOKEN_BUDGET`, default
     16000, and a separate `TOOL_RESPONSE_TOKEN_LIMIT` of 80000). It does not fire on
     the seed (5-column tables) and it is a latent correctness bug on a wide one: a
     dropped column makes `snapshot.field(path)` return None, and the schema checker
     reads that as the catalog POSITIVELY DENYING the column exists. Contradicted,
     from a token budget.

  6. A MISSING ENTITY IS NOT AN ERROR. `get_entities` on a nonexistent URN returns
     `isError: False` and `[{"error": "... not found", "urn": ...}]`. An adapter that
     does not special-case that dict builds an all-None snapshot and returns
     Insufficient-Coverage — laundering a hallucinated URN into a legitimate-looking
     audit result, which is the precise failure `EntityNotFoundError` exists to prevent.

None of this is configurable. There is no raw mode and no toggle: the only env vars the
server reads are token budgets and document-tool switches. The compaction is unconditional.

--------------------------------------------------------------------------------
WHY THIS IS A CORRECTNESS FINDING AND NOT A COVERAGE ONE
--------------------------------------------------------------------------------

The flattening does not degrade to silence. Fed the MCP snapshot, the real checkers —
imported here, not modelled — return:

  freshness: customer_profile within 48h        Supported    -> Insufficient-Coverage
  freshness: revenue_daily within 24h (stale)   Contradicted -> Insufficient-Coverage
  classification: customer_profile.email is PII Supported    -> CONTRADICTED
  classification: marketing_leads.work_email    Supported    -> Insufficient-Coverage
  schema: customer_profile.email VARCHAR(255)   Supported    -> Supported

Four of five verdicts move, and the third is the one that matters: a TRUE claim about a
CORRECTLY TAGGED PII column comes back CONTRADICTED. The mechanism is Session 6's
`COMPLETENESS_REACHES_COLUMNS`. The column's tag arrives as `"PII"` rather than
`urn:li:tag:PII`, so the claimed label does not match; the column is therefore "not
labelled"; `customer_profile` carries `Verified`, which grants closed-world reasoning;
and rule 4 of check_classification turns an unlabelled column of a reviewed table into a
DENIAL. So a lossy transport does not produce a cautious "we don't know" — it produces a
confident false denial that the catalog's own PII tag is there. That is a
Supported<->Contradicted correctness failure, which benchmark/README.md names as the
worst thing this product can do.

The rule this leaves behind: **a transport that is lossy for an LLM is not merely
lossy for a checker, it is INVERTING.** Attest's guards are built to fail closed on the
model's output; nothing in the design defends against the CATALOG READ being quietly
compacted upstream, because the read was never a place where meaning could be lost.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from attest.checkers.classification import check_classification  # noqa: E402
from attest.checkers.freshness import check_freshness  # noqa: E402
from attest.checkers.schema import check_schema  # noqa: E402
from attest.claims import (  # noqa: E402
    ClassificationClaim,
    ColumnAssertion,
    FreshnessClaim,
    SchemaClaim,
)
from attest.datahub.client import DataHubClient  # noqa: E402
from attest.datahub.snapshot import DatasetSnapshot, FieldSnapshot  # noqa: E402

SEED = os.path.join(os.path.dirname(__file__), "..", "seed", "ground_truth.json")

# The MCP server, exactly as an agent would launch it. `--native-tls` is this machine's
# corporate-CA trap, one runtime further out than the Python truststore injection and the
# Node NODE_USE_SYSTEM_CA flag: uv ships its own Rust root store and does not consult the
# OS one until told to. Same root cause, third runtime.
PARAMS = StdioServerParameters(
    command="uvx",
    args=[
        "--native-tls",
        "--from",
        "mcp-server-datahub",
        "mcp-server-datahub",
        "--transport",
        "stdio",
    ],
    env={
        "DATAHUB_GMS_URL": "http://localhost:8080",
        # The server posts usage to Mixpanel on startup. Off: a probe that measures the
        # catalog should not also report to a third party that it ran.
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "PATH": os.environ["PATH"],
    },
)


def snapshot_from_mcp(entity: dict[str, Any]) -> DatasetSnapshot:
    """MCP's `get_entities` payload -> a DatasetSnapshot, as faithfully as it allows.

    Reads every field the tool returns. What it cannot populate, the response does not
    carry — see the report at the bottom of this file.
    """
    props = entity.get("properties") or {}

    raw_props = props.get("customProperties")
    custom_properties = (
        {p["key"]: p.get("value", "") for p in raw_props}
        if raw_props is not None
        else None
    )

    owners = None
    if (ownership := entity.get("ownership")) is not None:
        owners = tuple(
            (o.get("owner") or {}).get("urn", "") for o in ownership.get("owners") or []
        )

    tags = None
    if (tag_block := entity.get("tags")) is not None:
        tags = tuple(
            (t.get("tag") or {}).get("urn", "") for t in tag_block.get("tags") or []
        )

    terms = None
    term_parents: dict[str, tuple[str, ...]] = {}
    if (term_block := entity.get("glossaryTerms")) is not None:
        terms = tuple(
            (t.get("term") or {}).get("urn", "") for t in term_block.get("terms") or []
        )
        for assoc in term_block.get("terms") or []:
            term = assoc.get("term") or {}
            if urn := term.get("urn"):
                nodes = (term.get("parentNodes") or {}).get("nodes") or []
                term_parents[urn] = tuple(n.get("urn", "") for n in nodes)

    fields = None
    if (schema := entity.get("schemaMetadata")) is not None:
        fields = tuple(
            FieldSnapshot(
                path=f["fieldPath"],
                native_type=f.get("nativeDataType"),
                # `type` is commented out of the server's own GraphQL fragment
                # (gql/entity_details.gql, `fragment entitySchemaFieldFields`), so
                # there is nothing to read here at any version.
                data_type=f.get("type"),
                description=f.get("description"),
                # MCP hands back tag/term DISPLAY NAMES, not URNs. Passed through
                # verbatim: inventing `urn:li:tag:{name}` here would fabricate an
                # identifier the catalog never sent and hide the finding.
                tags=tuple(f.get("tags") or ()),
                terms=tuple(f.get("glossaryTerms") or ()),
            )
            for f in schema.get("fields") or []
        )

    return DatasetSnapshot(
        urn=entity["urn"],
        name=entity.get("name"),
        platform=(entity.get("platform") or {}).get("name"),
        description=props.get("description"),
        # No lastModified is requested for a Dataset anywhere in the server's query.
        last_modified=None,
        owners=owners,
        tags=tags,
        terms=terms,
        fields=fields,
        custom_properties=custom_properties,
        term_parents=term_parents,
    )


async def fetch_all_via_mcp(urns: list[str]) -> dict[str, dict[str, Any]]:
    """One MCP session, every seeded URN, raw payloads back."""
    out: dict[str, dict[str, Any]] = {}
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"MCP server: {init.serverInfo.name} v{init.serverInfo.version}")
            tools = await session.list_tools()
            print(f"tools exposed: {', '.join(t.name for t in tools.tools)}\n")
            for urn in urns:
                result = await session.call_tool("get_entities", {"urns": [urn]})
                if result.isError:
                    raise SystemExit(f"get_entities failed for {urn}: {result.content}")
                payload = json.loads(result.content[0].text)
                out[urn] = payload[0] if isinstance(payload, list) else payload
    return out


def diff(ref: DatasetSnapshot, got: DatasetSnapshot) -> list[str]:
    """Every way `got` fails to be `ref`. Absent (None) and empty (()) are NOT equal."""
    problems: list[str] = []

    def check(label: str, a: Any, b: Any) -> None:
        if a != b:
            problems.append(f"{label}: graphql={a!r} mcp={b!r}")

    check("name", ref.name, got.name)
    check("platform", ref.platform, got.platform)
    check("description", ref.description, got.description)
    check("last_modified", ref.last_modified, got.last_modified)
    check("owners", ref.owners, got.owners)
    check("tags", ref.tags, got.tags)
    check("terms", ref.terms, got.terms)
    check("custom_properties", ref.custom_properties, got.custom_properties)
    check("term_parents", ref.term_parents, got.term_parents)

    if (ref.fields is None) != (got.fields is None):
        problems.append(f"fields presence: graphql={ref.fields is None=} mcp={got.fields is None=}")
        return problems
    if ref.fields is None:
        return problems

    ref_paths = [f.path for f in ref.fields]
    got_paths = [f.path for f in got.fields or ()]
    if set(ref_paths) != set(got_paths):
        missing = set(ref_paths) - set(got_paths)
        extra = set(got_paths) - set(ref_paths)
        problems.append(f"columns: missing={sorted(missing)} extra={sorted(extra)}")
    for rf in ref.fields:
        gf = next((f for f in got.fields or () if f.path == rf.path), None)
        if gf is None:
            continue
        check(f"field[{rf.path}].native_type", rf.native_type, gf.native_type)
        check(f"field[{rf.path}].data_type", rf.data_type, gf.data_type)
        check(f"field[{rf.path}].description", rf.description, gf.description)
        check(f"field[{rf.path}].tags", rf.tags, gf.tags)
        check(f"field[{rf.path}].terms", rf.terms, gf.terms)
    return problems


CP = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)"
ML = "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.marketing_leads,PROD)"
RD = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.finance.revenue_daily,PROD)"


def verdict_cases() -> list[tuple[str, str, Any, Any]]:
    """Claims chosen so each names one thing the transport drops. All five are TRUE."""
    return [
        (
            "freshness: customer_profile within 48h",
            CP,
            FreshnessClaim(target_urn=CP, max_age_hours=48, raw_text="updated within 48h"),
            check_freshness,
        ),
        (
            "freshness: revenue_daily within 24h (stale by a year)",
            RD,
            FreshnessClaim(target_urn=RD, max_age_hours=24, raw_text="updated within 24h"),
            check_freshness,
        ),
        (
            "classification: customer_profile.email is PII",
            CP,
            ClassificationClaim(
                target_urn=CP,
                field_path="email",
                labels=("urn:li:tag:PII",),
                present=True,
                raw_text="the email column is PII",
            ),
            check_classification,
        ),
        (
            "classification: marketing_leads.work_email is an EmailAddress",
            ML,
            ClassificationClaim(
                target_urn=ML,
                field_path="work_email",
                labels=("urn:li:glossaryTerm:EmailAddress",),
                present=True,
                raw_text="work_email holds an email address",
            ),
            check_classification,
        ),
        (
            "schema: customer_profile.email is VARCHAR(255)",
            CP,
            SchemaClaim(
                target_urn=CP,
                columns=(ColumnAssertion(name="email", native_type="VARCHAR(255)"),),
                raw_text="email is VARCHAR(255)",
            ),
            check_schema,
        ),
    ]


def report_verdict_impact(
    reference: dict[str, DatasetSnapshot], raw: dict[str, dict[str, Any]]
) -> int:
    """The finding that matters: a lossy transport does not degrade to silence.

    A field diff is abstract. This is the same gap expressed in the only units the
    project cares about — the verdict a human would be shown.
    """
    now = reference[CP].last_modified
    print("\n=== the same gap, as verdicts ===\n")
    print(f"{'claim (all five are TRUE)':<56} {'GraphQL':<22} {'MCP':<22}")
    print("-" * 104)
    flips = 0
    for label, urn, claim, checker in verdict_cases():
        got = snapshot_from_mcp(raw[urn])
        kw = {"now": now} if checker is check_freshness else {}
        a = checker(claim, reference[urn], **kw).verdict.value
        b = checker(claim, got, **kw).verdict.value
        marker = "  <-- FLIPPED" if a != b else ""
        flips += a != b
        print(f"{label:<56} {a:<22} {b:<22}{marker}")
    print(f"\n{flips}/5 verdicts move under the MCP transport.")
    return flips


def main() -> None:
    urns = [d["urn"] for d in json.load(open(SEED))["datasets"]]
    print(f"=== MCP vs GraphQL parity over {len(urns)} seeded datasets ===\n")

    client = DataHubClient()
    reference = {urn: client.fetch_dataset(urn) for urn in urns}
    raw = asyncio.run(fetch_all_via_mcp(urns))

    total = 0
    by_kind: dict[str, int] = {}
    for urn in urns:
        got = snapshot_from_mcp(raw[urn])
        problems = diff(reference[urn], got)
        short = urn.split(",")[1] if "," in urn else urn
        if problems:
            print(f"[FAIL] {short}  ({len(problems)} mismatches)")
            for p in problems[:6]:
                print(f"         {p}")
            if len(problems) > 6:
                print(f"         ... {len(problems) - 6} more")
        else:
            print(f"[ OK ] {short}")
        total += len(problems)
        for p in problems:
            kind = p.split(":")[0].split("[")[0].replace("field", "field.*")
            by_kind[kind] = by_kind.get(kind, 0) + 1

    print(f"\n=== {total} mismatches over {len(urns)} datasets ===")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}  {kind}")

    flips = report_verdict_impact(reference, raw)

    if total or flips:
        print(
            "\nMCP cannot reproduce the reference snapshot, and the gap is not academic: "
            "a checker fed the MCP snapshot decides a different verdict from the one every "
            "measured number in this project was measured against - including a TRUE claim "
            "about a correctly tagged PII column coming back CONTRADICTED."
        )
        sys.exit(1)
    print("\nParity holds: MCP can sit behind cache.Reader unnoticed.")


if __name__ == "__main__":
    main()
