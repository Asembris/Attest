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

Measured against mcp-server-datahub 0.6.0 — PINNED in PARAMS below — against the pinned
Core v1.5.0.6. The handshake announces itself as `datahub v3.4.4`, and that number is NOT
the server's: FastMCP fills in its OWN version when the server does not set one, so it
tracks fastmcp, not mcp-server-datahub. (Verified: on the same pinned 0.6.0 the server
object reports name `datahub`, version == `fastmcp.__version__`, which reads 3.4.5 today.)
Do not read it as evidence about which server ran; the pin is that evidence.

The server RUNS against Core — it detects OSS
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

  4. ABSENT AND EMPTY ARE COLLAPSED. `clean_gql_response` recursively drops `None`,
     `[]` and `{}`, so "the catalog has no properties aspect" and "the aspect exists and
     holds nothing" arrive identically. MEASURED: `custom_properties` is `{}` via GraphQL
     (the aspect is there and empty) and `None` via MCP (absent) on 12/16 datasets.

     STATED PRECISELY, because the flattering version of this finding is wrong and was
     written here first: this does NOT flip a verdict, and snapshot.py says why in its
     own docstring — "Both yield Insufficient-Coverage: an unowned table is unowned
     either way." What it destroys is EVIDENCE FIDELITY, which is the entire reason
     snapshot.py preserves the distinction: a checker reporting what it saw can no
     longer say whether the catalog was silent or merely empty, and it is the only layer
     that still knew. A latent correctness risk for any consumer whose rules do turn on
     it; a measured loss of evidence for this one.

     Note the near-miss, since it is the reason to distrust a tidy story: `count` is a
     scalar, so `parentNodes: {count: 0, nodes: []}` survives as `{count: 0}` — a term
     with no parents is still legible. The one `term_parents` mismatch in the run is NOT
     this rule; it is finding 2 (customer_contact's CustomerIdentifier is a COLUMN-level
     term, and column terms arrive as display names, so no URN and no hierarchy).

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

import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
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

# The two-name readers for the `initialize` handshake and the tool error flag, IMPORTED
# from the shipped discovery module rather than copied into this file.
#
# `mcp` 2.0.0 renamed `CallToolResult.isError` to `is_error` and `InitializeResult.serverInfo`
# to `server_info`; the pyproject floor (`mcp>=1.2`) admits both. This probe used bare
# attribute access (`result.isError`), which under a 2.x client raises AttributeError at the
# FIRST tool call -- loud rather than silent, but it kills the run before it measures anything
# and blames a missing attribute rather than the server refusing the call.
#
# Imported, not duplicated: two copies of one two-name lookup is exactly the drift that
# `evidence.py` was written to refuse, and the shipped pair already carries the offline suite
# that pins both spellings plus the legacy-only-reader vacuity check (tests/test_discovery.py).
# This is a READ of production code. It changes nothing in it, and `spikes/` sits outside the
# import graph `tests/test_discovery_boundary.py` walks.
from attest.discovery.mcp import server_identity, tool_reported_error  # noqa: E402
from evidence import provenance, write_receipt  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
SEED = os.path.join(os.path.dirname(__file__), "..", "seed", "ground_truth.json")
GMS_URL = "http://localhost:8080"

# The frozen comparison rules, recorded IN the receipt so a later reader can tell whether two
# parity receipts are comparable without diffing this file. Declared, not derived from the
# code below -- the `trajectory.py` house rule: derive it from the thing it describes and it
# agrees by construction and asserts nothing.
#
# `parity-v1` is the rule set Session 17 measured 130/16 under. NOTHING in `diff()` or
# `snapshot_from_mcp()` moved when this block was added, which is what lets the two runs be
# set beside each other at all. Bump the version if a comparison rule ever changes, and do it
# in a commit that contains no measurement.
METHODOLOGY = {
    "version": "parity-v1",
    "enumeration": (
        "every datasets[].urn in seed/ground_truth.json, in file order; the count is "
        "DERIVED from the manifest and never a literal"
    ),
    "reference": "graphql",
    "measured": "mcp",
    "compared_fields": (
        "dataset: name, platform, description, last_modified, owners, tags, terms, "
        "custom_properties, term_parents, fields-presence, column-set; "
        "per matched column: native_type, data_type, description, tags, terms"
    ),
    "mismatch_unit": (
        "one `!=` between the reference value and the measured value. A column-set "
        "divergence counts as ONE mismatch however many columns differ."
    ),
    "comparison_opportunity": (
        "one executed comparison. Counted on the same traversal that counts mismatches, "
        "so the denominator cannot drift from the numerator. Columns present in the "
        "reference but absent from the measured snapshot are skipped by the traversal and "
        "contribute no opportunities -- their loss is already reported by the column-set "
        "mismatch."
    ),
    "absent_empty_rule": (
        "None != () != {}. Absent, empty and malformed stay three distinct states "
        "(snapshot.py, CLAUDE.md Session 23)."
    ),
    "pass_fail": "non-zero exit if any mismatch OR any verdict flip. Non-zero is BY DESIGN.",
}

# Session 17's result. There was no machine-readable receipt for it: the probe printed to
# stdout and the number lived in prose. It is carried here so the new receipt records what it
# is being set beside, and it is NEVER recomputed, scaled, or used as a denominator.
HISTORICAL = {
    "session": 17,
    "result": "130 mismatches over 16 datasets",
    "mismatches_total": 130,
    "datasets_compared": 16,
    "datasets_with_mismatches": 16,
    "receipt": None,
    "receipt_note": (
        "prose-only. The Session 17 probe wrote no receipt; the number lives in this "
        "file's docstring and in docs/mcp-evaluation.md. This is the first "
        "machine-readable parity receipt, so there is no baseline file to hash."
    ),
    "described_in": "docs/mcp-evaluation.md",
    "seed_note": (
        "measured over a 16-dataset seed. The 17th (analytics.platform.ingest_metrics, "
        "CorpGroup-owned) arrived with 2d7eaf9, so the two runs do not share a denominator "
        "and neither total may be derived from the other by arithmetic."
    ),
}

# The MCP server, exactly as an agent would launch it. `--native-tls` is this machine's
# corporate-CA trap, one runtime further out than the Python truststore injection and the
# Node NODE_USE_SYSTEM_CA flag: uv ships its own Rust root store and does not consult the
# OS one until told to. Same root cause, third runtime.
#
# PINNED to the version this file's report was measured against. The whole point of this
# probe is that it exits non-zero BY DESIGN — a tripwire that goes green the day the finding
# expires. An unpinned `--from` resolves whatever is newest at first use, so the tripwire
# would silently start testing a server the report above does not describe, and could flip
# either way for a reason nobody could reconstruct. Bump this deliberately, re-run, and
# re-take the numbers; the pin is what makes "it went green" mean something.
PARAMS = StdioServerParameters(
    command="uvx",
    args=[
        "--native-tls",
        "--from",
        "mcp-server-datahub==0.6.0",
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


async def fetch_all_via_mcp(urns: list[str]) -> tuple[dict[str, dict[str, Any]], str]:
    """One MCP session, every seeded URN, raw payloads back — and the handshake identity.

    The handshake string is returned rather than only printed so the receipt can record
    which server actually answered. Read `server_identity`'s docstring before trusting the
    version in it: FastMCP fills in its OWN version when the server does not set one, so it
    tracks fastmcp rather than mcp-server-datahub. The `--from` pin is the real evidence.
    """
    out: dict[str, dict[str, Any]] = {}
    async with stdio_client(PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            handshake = server_identity(init)
            print(f"MCP server: {handshake}")
            tools = await session.list_tools()
            print(f"tools exposed: {', '.join(t.name for t in tools.tools)}\n")
            for urn in urns:
                result = await session.call_tool("get_entities", {"urns": [urn]})
                if tool_reported_error(result):
                    raise SystemExit(f"get_entities failed for {urn}: {result.content}")
                payload = json.loads(result.content[0].text)
                out[urn] = payload[0] if isinstance(payload, list) else payload
    return out, handshake


def diff(ref: DatasetSnapshot, got: DatasetSnapshot) -> tuple[list[str], int]:
    """Every way `got` fails to be `ref`. Absent (None) and empty (()) are NOT equal.

    Returns the mismatches AND the number of comparisons that were executed to find them.

    **NO COMPARISON RULE IN HERE MOVED when the counter was added.** Every `check`, every
    branch and every early return is byte-identical to the code Session 17 measured 130/16
    under; the only addition is `opportunities += 1` beside each executed comparison. That
    is deliberate and it is the whole reason two runs can be set beside each other: a
    refresh that quietly retuned the comparison would be measuring its own author.

    The counter is incremented on the SAME traversal that finds the mismatches, so the
    denominator cannot drift from the numerator -- a separate counting function would be a
    second implementation of the same walk, and it would be wrong the first time either
    changed. Early returns stop both counts together, which is honest: a comparison the
    traversal never reached was never an opportunity.
    """
    problems: list[str] = []
    opportunities = 0

    def check(label: str, a: Any, b: Any) -> None:
        nonlocal opportunities
        opportunities += 1
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

    opportunities += 1
    if (ref.fields is None) != (got.fields is None):
        problems.append(f"fields presence: graphql={ref.fields is None=} mcp={got.fields is None=}")
        return problems, opportunities
    if ref.fields is None:
        return problems, opportunities

    ref_paths = [f.path for f in ref.fields]
    got_paths = [f.path for f in got.fields or ()]
    opportunities += 1
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
    return problems, opportunities


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
) -> tuple[int, list[dict[str, Any]]]:
    """The finding that matters: a lossy transport does not degrade to silence.

    A field diff is abstract. This is the same gap expressed in the only units the
    project cares about — the verdict a human would be shown.

    Returns the flip count and the row-by-row table, so the receipt carries the verdicts
    themselves rather than only how many of them moved.
    """
    now = reference[CP].last_modified
    print("\n=== the same gap, as verdicts ===\n")
    print(f"{'claim (all five are TRUE)':<56} {'GraphQL':<22} {'MCP':<22}")
    print("-" * 104)
    flips = 0
    rows: list[dict[str, Any]] = []
    for label, urn, claim, checker in verdict_cases():
        got = snapshot_from_mcp(raw[urn])
        kw = {"now": now} if checker is check_freshness else {}
        a = checker(claim, reference[urn], **kw).verdict.value
        b = checker(claim, got, **kw).verdict.value
        marker = "  <-- FLIPPED" if a != b else ""
        flips += a != b
        rows.append(
            {"claim": label, "target_urn": urn, "graphql": a, "mcp": b, "flipped": a != b}
        )
        print(f"{label:<56} {a:<22} {b:<22}{marker}")
    print(f"\n{flips}/5 verdicts move under the MCP transport.")
    return flips, rows


def pinned_server_version() -> str:
    """The `mcp-server-datahub` version this run launched, read off PARAMS.

    Derived from the launch arguments rather than restated as a constant: a second copy
    would be the thing that drifts, and the pin is the evidence for which server answered.
    """
    args = list(PARAMS.args or ())
    spec = args[args.index("--from") + 1] if "--from" in args else ""
    return spec.split("==", 1)[1] if "==" in spec else spec or "unpinned"


def mcp_client_version() -> str:
    """The resolved Python `mcp` client. Unknown is said, never guessed."""
    try:
        return importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown (not installed)"


def census(manifest: dict[str, Any]) -> tuple[list[str], list[str]]:
    """The dataset list this run must compare, checked before a byte crosses the network.

    Every assertion here exists to stop the denominator moving quietly. Session 17 measured
    16 datasets and the seed now holds 17; a run that silently compared 16 of them would
    report a smaller total that looked like an improvement in the transport.

    The expected count is DERIVED from the manifest -- there is deliberately no literal to
    fall out of date. The CorpGroup dataset is likewise identified by `owner_groups` in the
    manifest rather than by a URN written here.
    """
    datasets = manifest["datasets"]
    urns = [d["urn"] for d in datasets]
    group_owned = [d["urn"] for d in datasets if d.get("owner_groups")]

    if len(urns) != len(datasets):
        raise SystemExit("census: a manifest entry has no urn")
    if len(set(urns)) != len(urns):
        dupes = sorted({u for u in urns if urns.count(u) > 1})
        raise SystemExit(f"census: duplicate URNs in the manifest: {dupes}")
    if not group_owned:
        raise SystemExit(
            "census: no group-owned dataset in the seed manifest. The CorpGroup-owned "
            "dataset (2d7eaf9) is the one shape Attest's own reader was blind to until "
            "an external catalog found it, and a parity run that does not include it "
            "cannot say whether this transport is blind to it too. Re-seed."
        )
    return urns, group_owned


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--receipt",
        type=Path,
        default=None,
        help=(
            "write a machine-readable parity receipt here. REFUSES to overwrite an "
            "existing file: a second run must land somewhere new so that it cannot hide."
        ),
    )
    args = parser.parse_args()

    # Refused UP FRONT as well as at write time. `write_receipt` is the authoritative gate,
    # but it fires after the whole measurement has run -- and the most likely way to hit it
    # is re-running a command whose path is already committed evidence. Failing in a second
    # beats failing after a full parity sweep.
    if args.receipt is not None and args.receipt.exists():
        raise SystemExit(
            f"{args.receipt} already exists and this will not overwrite a receipt.\n"
            f"A receipt is evidence: overwriting one destroys the artifact a committed "
            f"number traces to, and there is deliberately no --force.\n"
            f"Pass --receipt with a NEW path."
        )

    manifest = json.load(open(SEED))
    urns, group_owned = census(manifest)
    print(f"=== MCP vs GraphQL parity over {len(urns)} seeded datasets ===")
    print(f"census: {len(urns)} from the manifest, {len(group_owned)} group-owned\n")

    # Every enumerated URN must resolve over GraphQL or the run ABORTS. The reference read
    # is what the measurement is against; a dataset that quietly dropped out here would
    # shrink both the numerator and the denominator and read as better parity.
    client = DataHubClient()
    reference: dict[str, DatasetSnapshot] = {}
    for urn in urns:
        try:
            reference[urn] = client.fetch_dataset(urn)
        except Exception as exc:  # noqa: BLE001 - any failure here invalidates the census
            raise SystemExit(
                f"census: {urn} did not resolve over GraphQL ({type(exc).__name__}: {exc}).\n"
                f"The reference read is the measurement's denominator. Aborting rather "
                f"than comparing {len(urns) - 1} of {len(urns)} datasets and reporting a "
                f"total nobody could line up against another run."
            ) from exc

    raw, handshake = asyncio.run(fetch_all_via_mcp(urns))
    missing = [u for u in urns if u not in raw]
    if missing:
        raise SystemExit(f"census: MCP returned no payload for {missing}")

    total = 0
    opportunities = 0
    by_kind: dict[str, int] = {}
    per_dataset: list[dict[str, Any]] = []
    for urn in urns:
        got = snapshot_from_mcp(raw[urn])
        problems, seen = diff(reference[urn], got)
        opportunities += seen
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
        kinds: dict[str, int] = {}
        for p in problems:
            kind = p.split(":")[0].split("[")[0].replace("field", "field.*")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            kinds[kind] = kinds.get(kind, 0) + 1
        per_dataset.append(
            {
                "urn": urn,
                "name": short,
                "group_owned": urn in group_owned,
                "mismatches": len(problems),
                "comparisons": seen,
                "by_kind": kinds,
                "detail": problems,
            }
        )

    failing = sum(1 for d in per_dataset if d["mismatches"])
    print(f"\n=== {total} mismatches over {len(urns)} datasets ===")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}  {kind}")
    print(
        f"\n{failing}/{len(urns)} datasets carry at least one mismatch; "
        f"mean {total / len(urns):.2f} mismatches per dataset over "
        f"{opportunities} comparisons."
    )

    flips, verdict_rows = report_verdict_impact(reference, raw)

    if args.receipt is not None:
        write_receipt(
            args.receipt,
            {
                "what_this_is": (
                    "MCP-vs-GraphQL snapshot parity, re-measured over the current seed "
                    "catalog. NOT a benchmark: nothing here is scored, and no accuracy, "
                    "macro-F1 or confusion matrix is computed over it. GraphQL is the "
                    "reference read; MCP is the transport under measurement. "
                    "See docs/mcp-evaluation.md."
                ),
                "reproduce": (
                    f"python spikes/mcp_reader_probe.py --receipt {args.receipt.as_posix()}"
                ),
                "provenance": provenance(
                    fix_under_test=(
                        "none. No product code is under test here: the transport, the "
                        "normalizer and every comparison rule are unchanged from Session "
                        "17. What moved is the SEED (2d7eaf9 added a CorpGroup-owned "
                        "dataset, 16 -> 17) and the probe's own error-flag reader."
                    ),
                    gms_url=GMS_URL,
                    extra={
                        "mcp_server_datahub_version": pinned_server_version(),
                        "mcp_server_datahub_pin": "PARAMS --from, pinned deliberately",
                        "mcp_client_version": mcp_client_version(),
                        "mcp_server_handshake": handshake,
                        "mcp_server_handshake_note": (
                            "FastMCP fills in its OWN version when the server does not set "
                            "one, so this tracks fastmcp rather than mcp-server-datahub. "
                            "The --from pin is the evidence for which server ran."
                        ),
                    },
                ),
                "methodology": METHODOLOGY,
                "census": {
                    "datasets_enumerated": len(urns),
                    "datasets_compared": len(per_dataset),
                    "source": "seed/ground_truth.json",
                    "group_owned_urns": group_owned,
                    "urns": urns,
                },
                "totals": {
                    "mismatches_total": total,
                    "datasets_compared": len(urns),
                    "datasets_with_mismatches": failing,
                    "mean_mismatches_per_dataset": round(total / len(urns), 3),
                    "comparison_opportunities_total": opportunities,
                    "mismatch_fraction": round(total / opportunities, 6)
                    if opportunities
                    else None,
                    "mismatch_fraction_note": (
                        "measured, not inferred: the denominator is the count of "
                        "comparisons this run actually executed, tallied on the same "
                        "traversal as the mismatches. It is NOT comparable to Session "
                        "17, which counted no denominator."
                    ),
                    "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
                    "verdict_flips": flips,
                    "verdict_cases": len(verdict_rows),
                },
                "verdicts": verdict_rows,
                "per_dataset": per_dataset,
                "historical": HISTORICAL,
            },
        )
        print(f"\nreceipt: {args.receipt}")

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
