"""Generate Attest's seed catalog and the recipe that ingests it.

Why hand-rolled seed data instead of a DataHub sample datapack: the showcase
datapacks reference `dataQualityCheck`, a DataHub Cloud entity type that is not
in Core's EntityRegistry. Loading one fails, and the failure takes the schemas,
owners, tags, and glossary links after it down with it.

We sidestep the entire error class by construction: we emit only aspects Core
supports natively, and we never need the missing one. Attest's four claim types
are freshness, ownership, classification, and schema — none of them touch
data-quality entities. We also do not fake `dataQualityCheck` as custom
properties: verification logic built against a hand-invented shape that
corresponds to nothing real in DataHub would not survive scrutiny.

That constraint is also the point. Attest verifies claims against known ground
truth, so its benchmark needs entities where we control exactly what is true.
Every dataset below is here on purpose. Each carries an `exercises` field naming
the verdict bucket it is designed to land in, and a `note` explaining how — that
mapping is what the golden benchmark gets built from. Both flow into
ground_truth.json.

The three buckets:

  Supported             — fully documented datasets: a claim matches the catalog.
  Contradicted          — the catalog positively disagrees with a plausible claim
                          (a column tagged NonPII that a naive agent would call
                          PII; an owner who is not the claimed owner; a table
                          last modified 400+ days ago that a claim calls fresh).
  Insufficient-Coverage — the catalog is silent: no owner, no tags, no terms, no
                          description. Nothing to support *or* contradict.

Outputs (all under ./seed):
  seed_metadata.json  — MCPs for `datahub ingest`
  recipe.yml          — file source -> datahub-rest sink
  ground_truth.json   — what we asserted, for the benchmark to check against

Run:  python seed/generate_seed.py
Then: datahub ingest -c ./seed/recipe.yml
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from datahub.emitter.mce_builder import (
    make_dataset_urn,
    make_tag_urn,
    make_term_urn,
    make_user_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.sink.file import write_metadata_file
from datahub.metadata.schema_classes import (
    AuditStampClass,
    BooleanTypeClass,
    CorpUserInfoClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    GlossaryTermAssociationClass,
    GlossaryTermInfoClass,
    GlossaryTermsClass,
    NumberTypeClass,
    OtherSchemaClass,
    OwnerClass,
    OwnershipClass,
    OwnershipTypeClass,
    SchemaFieldClass,
    SchemaFieldDataTypeClass,
    SchemaMetadataClass,
    StringTypeClass,
    StructuredPropertyDefinitionClass,
    TagAssociationClass,
    TagPropertiesClass,
    TimeStampClass,
    TimeTypeClass,
)

SEED_DIR = Path(__file__).parent
ENV = "PROD"
ACTOR = "urn:li:corpuser:datahub"

NOW = datetime.now(timezone.utc)
FRESH = NOW - timedelta(hours=6)
STALE = NOW - timedelta(days=417)


def millis(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def audit(dt: datetime) -> AuditStampClass:
    return AuditStampClass(time=millis(dt), actor=ACTOR)


# --- vocabulary --------------------------------------------------------------

TYPES = {
    "string": StringTypeClass(),
    "number": NumberTypeClass(),
    "boolean": BooleanTypeClass(),
    "time": TimeTypeClass(),
}

TAGS = {
    "PII": "Column or table carries personally identifiable information.",
    "NonPII": "Reviewed and confirmed to carry no personally identifiable information.",
    "Sensitive": "Restricted access; handle under the data protection policy.",
    "Tier1": "Business-critical. Breakages page someone.",
    "Tier2": "Important but not business-critical.",
    "Deprecated": "Scheduled for removal. Do not build on this.",
    "Verified": "Reviewed by the data governance team.",
}

# term id -> (display name, definition)
TERMS = {
    "EmailAddress": ("Email Address", "An address identifying a person's mailbox."),
    "PhoneNumber": ("Phone Number", "A telephone number reaching a person."),
    "PersonName": ("Person Name", "The given and/or family name of a natural person."),
    "Revenue": ("Revenue", "Gross income from business activity before costs."),
    "CustomerIdentifier": (
        "Customer Identifier",
        "A surrogate key identifying a customer. Not itself personal data.",
    ),
}

USERS = {
    "alice.chen": ("Alice Chen", "alice.chen@example.com", "Data Engineer"),
    "bob.martinez": ("Bob Martinez", "bob.martinez@example.com", "Analytics Lead"),
    "carol.davis": ("Carol Davis", "carol.davis@example.com", "Marketing Analyst"),
    "dana.wu": ("Dana Wu", "dana.wu@example.com", "Platform Engineer"),
}

VERDICT_PROPERTY_URN = "urn:li:structuredProperty:attest.groundedness_verdict"


# --- catalog spec ------------------------------------------------------------


@dataclass
class Column:
    name: str
    type: str
    native: str
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)


@dataclass
class Dataset:
    platform: str
    name: str
    description: str | None
    columns: list[Column]
    owner: str | None
    tags: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    stale: bool = False
    # Which verdict this entity exists to exercise. Recorded in ground_truth.json.
    exercises: str = "Supported"
    note: str = ""

    @property
    def urn(self) -> str:
        return make_dataset_urn(platform=self.platform, name=self.name, env=ENV)

    @property
    def last_modified(self) -> datetime:
        return STALE if self.stale else FRESH


CATALOG: list[Dataset] = [
    # --- snowflake ----------------------------------------------------------
    Dataset(
        platform="snowflake",
        name="analytics.customers.customer_profile",
        description=(
            "One row per customer. The canonical customer record; everything "
            "downstream joins to this."
        ),
        owner="alice.chen",
        tags=["PII", "Tier1", "Verified"],
        terms=["EmailAddress", "PersonName", "CustomerIdentifier"],
        columns=[
            Column("customer_id", "string", "VARCHAR(36)", "Surrogate key.",
                   tags=["NonPII"], terms=["CustomerIdentifier"]),
            Column("email", "string", "VARCHAR(255)", "Primary contact email.",
                   tags=["PII"], terms=["EmailAddress"]),
            Column("full_name", "string", "VARCHAR(200)", "Customer's full name.",
                   tags=["PII"], terms=["PersonName"]),
            Column("signup_ts", "time", "TIMESTAMP_NTZ", "When the customer signed up."),
            Column("is_active", "boolean", "BOOLEAN", "Whether the account is active."),
        ],
        exercises="Supported",
        note="Fully documented. Claims about its owner, PII columns, and terms should be Supported.",
    ),
    Dataset(
        platform="snowflake",
        name="analytics.customers.customer_contact",
        description="Contact channels per customer. One row per customer per channel.",
        owner="alice.chen",
        tags=["PII", "Sensitive", "Tier2"],
        terms=["PhoneNumber", "EmailAddress"],
        columns=[
            Column("customer_id", "string", "VARCHAR(36)", "FK to customer_profile.",
                   tags=["NonPII"], terms=["CustomerIdentifier"]),
            Column("phone_number", "string", "VARCHAR(32)", "E.164 phone number.",
                   tags=["PII"], terms=["PhoneNumber"]),
            Column("email", "string", "VARCHAR(255)", "Contact email.",
                   tags=["PII"], terms=["EmailAddress"]),
            Column("verified_at", "time", "TIMESTAMP_NTZ", "When the channel was verified."),
        ],
        exercises="Supported",
        note=(
            "Complete metadata, fresh. Exercises classification claims at column grain: "
            "`phone_number` carries PhoneNumber, `email` carries EmailAddress, and "
            "`customer_id` is explicitly NonPII."
        ),
    ),
    Dataset(
        platform="snowflake",
        name="analytics.orders.orders_fact",
        description="One row per order. Grain: order_id. Excludes cancelled orders.",
        owner="bob.martinez",
        tags=["Tier1", "Verified"],
        terms=["Revenue", "CustomerIdentifier"],
        columns=[
            Column("order_id", "string", "VARCHAR(36)", "Order surrogate key.", tags=["NonPII"]),
            Column("customer_id", "string", "VARCHAR(36)", "FK to customer_profile.",
                   tags=["NonPII"], terms=["CustomerIdentifier"]),
            Column("order_total", "number", "NUMBER(12,2)", "Order total in USD.",
                   terms=["Revenue"]),
            Column("order_ts", "time", "TIMESTAMP_NTZ", "When the order was placed."),
        ],
        exercises="Supported",
        note="No PII at all. A claim that this table contains PII should be Contradicted.",
    ),
    Dataset(
        platform="snowflake",
        name="analytics.marketing.email_campaign_stats",
        description=(
            "Per-recipient campaign engagement. Recipients are joined on a salted "
            "hash; the raw address is never landed here."
        ),
        owner="carol.davis",
        tags=["NonPII", "Tier2", "Verified"],
        terms=["CustomerIdentifier"],
        columns=[
            Column("campaign_id", "string", "VARCHAR(36)", "Campaign key.", tags=["NonPII"]),
            Column(
                "recipient_email_hash",
                "string",
                "VARCHAR(64)",
                "SHA-256 of the lowercased address, salted. Not reversible; reviewed "
                "and classified as non-personal.",
                tags=["NonPII"],
            ),
            Column("opened", "boolean", "BOOLEAN", "Whether the email was opened."),
            Column("clicked", "boolean", "BOOLEAN", "Whether any link was clicked."),
        ],
        exercises="Contradicted",
        note=(
            "The trap: `recipient_email_hash` looks like PII by name and is explicitly "
            "tagged NonPII. A claim that it contains PII must be Contradicted, not "
            "Insufficient-Coverage."
        ),
    ),
    Dataset(
        platform="snowflake",
        name="analytics.finance.revenue_daily",
        description="Daily gross revenue by region. Rebuilt nightly — or it was.",
        owner="bob.martinez",
        tags=["Tier1"],
        terms=["Revenue"],
        columns=[
            Column("revenue_date", "time", "DATE", "The day being reported."),
            Column("region", "string", "VARCHAR(64)", "Sales region."),
            Column("gross_revenue", "number", "NUMBER(14,2)", "Gross revenue in USD.",
                   terms=["Revenue"]),
        ],
        stale=True,
        exercises="Contradicted",
        note=(
            "Complete metadata but last modified 417 days ago. A claim that this table "
            "is refreshed daily / is fresh must be Contradicted."
        ),
    ),
    Dataset(
        platform="snowflake",
        name="analytics.staging.raw_events",
        description=None,
        owner=None,
        tags=[],
        terms=[],
        columns=[
            Column("event_id", "string", "VARCHAR(36)"),
            Column("payload", "string", "VARIANT"),
            Column("ingested_at", "time", "TIMESTAMP_NTZ"),
        ],
        exercises="Insufficient-Coverage",
        note=(
            "Schema only: no owner, no tags, no terms, no description, no column "
            "descriptions. Claims about ownership or PII here must be "
            "Insufficient-Coverage — the catalog is silent, not disagreeing."
        ),
    ),
    # --- postgres -----------------------------------------------------------
    Dataset(
        platform="postgres",
        name="attest_db.public.users",
        description="Application user accounts. Source of truth for auth.",
        owner="dana.wu",
        tags=["PII", "Tier1"],
        terms=["EmailAddress", "PersonName"],
        columns=[
            Column("user_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("email", "string", "varchar(255)", "Login email. Unique.",
                   tags=["PII"], terms=["EmailAddress"]),
            Column("first_name", "string", "varchar(100)", "Given name.",
                   tags=["PII"], terms=["PersonName"]),
            Column("last_name", "string", "varchar(100)", "Family name.",
                   tags=["PII"], terms=["PersonName"]),
            Column("created_at", "time", "timestamptz", "Account creation time."),
        ],
        exercises="Supported",
        note=(
            "The postgres mirror of customer_profile. Complete owner, tags, terms, and a "
            "fresh timestamp, so ownership and freshness claims should both be Supported."
        ),
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.payment_methods",
        description=(
            "Stored payment instruments. Card numbers are held by the payment "
            "processor; only the last four digits are landed here."
        ),
        owner="dana.wu",
        tags=["PII", "Sensitive", "Tier1"],
        terms=["CustomerIdentifier"],
        columns=[
            Column("payment_method_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("user_id", "string", "uuid", "FK to users.",
                   tags=["NonPII"], terms=["CustomerIdentifier"]),
            Column("card_last4", "string", "char(4)", "Last four digits only. Not a card number.",
                   tags=["NonPII"]),
            Column("billing_zip", "string", "varchar(16)", "Billing postal code.",
                   tags=["PII"]),
        ],
        exercises="Contradicted",
        note=(
            "Table is tagged PII but `card_last4` is explicitly NonPII. Exercises "
            "table-level vs column-level claims pointing opposite ways."
        ),
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.support_tickets",
        description="Customer support tickets. One row per ticket.",
        owner="carol.davis",
        tags=["Tier2"],
        terms=["CustomerIdentifier"],
        columns=[
            Column("ticket_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("user_id", "string", "uuid", "FK to users.",
                   tags=["NonPII"], terms=["CustomerIdentifier"]),
            Column("subject", "string", "text", "Ticket subject line."),
            Column("opened_at", "time", "timestamptz", "When the ticket was opened."),
        ],
        exercises="Supported",
        note="Owned by carol.davis. A claim that dana.wu owns it must be Contradicted.",
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.legacy_accounts",
        description=None,
        owner=None,
        tags=["Deprecated"],
        terms=[],
        columns=[
            Column("account_id", "string", "varchar(64)"),
            Column("email", "string", "varchar(255)"),
            Column("closed_at", "time", "timestamptz"),
        ],
        stale=True,
        exercises="Insufficient-Coverage",
        note=(
            "Tagged Deprecated but has no owner and no glossary terms. `email` is "
            "untagged — a claim that it is PII is Insufficient-Coverage (unclassified), "
            "NOT Contradicted. This is the distinction the auditor must not blur."
        ),
    ),
]


# --- emission ----------------------------------------------------------------


def vocabulary_mcps() -> list[MetadataChangeProposalWrapper]:
    """Tags, glossary terms, and users must exist as entities, not just links."""
    mcps: list[MetadataChangeProposalWrapper] = []

    for tag, description in TAGS.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_tag_urn(tag),
                aspect=TagPropertiesClass(name=tag, description=description),
            )
        )

    for term_id, (name, definition) in TERMS.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_term_urn(term_id),
                aspect=GlossaryTermInfoClass(
                    name=name,
                    definition=definition,
                    termSource="INTERNAL",
                ),
            )
        )

    for username, (display, email, title) in USERS.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_user_urn(username),
                aspect=CorpUserInfoClass(
                    active=True, displayName=display, email=email, title=title
                ),
            )
        )

    # The structured property Attest writes its verdicts into. Defined here so
    # ingestion owns the definition; the probe asserts it landed.
    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=VERDICT_PROPERTY_URN,
            aspect=StructuredPropertyDefinitionClass(
                qualifiedName="attest.groundedness_verdict",
                displayName="Attest Groundedness Verdict",
                description=(
                    "The most recent groundedness verdict Attest recorded for this "
                    "dataset."
                ),
                valueType="urn:li:dataType:datahub.string",
                entityTypes=["urn:li:entityType:datahub.dataset"],
                cardinality="SINGLE",
            ),
        )
    )

    return mcps


def dataset_mcps(ds: Dataset) -> list[MetadataChangeProposalWrapper]:
    mcps: list[MetadataChangeProposalWrapper] = []
    stamp = audit(ds.last_modified)

    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=ds.urn,
            aspect=DatasetPropertiesClass(
                name=ds.name.split(".")[-1],
                qualifiedName=ds.name,
                description=ds.description,
                lastModified=TimeStampClass(time=millis(ds.last_modified), actor=ACTOR),
                customProperties={
                    "seeded_by": "attest/seed/generate_seed.py",
                    "exercises_verdict": ds.exercises,
                },
            ),
        )
    )

    fields = [
        SchemaFieldClass(
            fieldPath=col.name,
            type=SchemaFieldDataTypeClass(type=TYPES[col.type]),
            nativeDataType=col.native,
            description=col.description,
            nullable=col.name not in ("customer_id", "user_id", "order_id"),
            globalTags=GlobalTagsClass(
                tags=[TagAssociationClass(tag=make_tag_urn(t)) for t in col.tags]
            )
            if col.tags
            else None,
            glossaryTerms=GlossaryTermsClass(
                terms=[
                    GlossaryTermAssociationClass(urn=make_term_urn(t)) for t in col.terms
                ],
                auditStamp=stamp,
            )
            if col.terms
            else None,
        )
        for col in ds.columns
    ]

    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=ds.urn,
            aspect=SchemaMetadataClass(
                schemaName=ds.name,
                platform=f"urn:li:dataPlatform:{ds.platform}",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=fields,
                created=stamp,
                lastModified=stamp,
            ),
        )
    )

    # Deliberately omitted for the Insufficient-Coverage datasets.
    if ds.owner:
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=OwnershipClass(
                    owners=[
                        OwnerClass(
                            owner=make_user_urn(ds.owner),
                            type=OwnershipTypeClass.TECHNICAL_OWNER,
                        )
                    ],
                    lastModified=stamp,
                ),
            )
        )

    if ds.tags:
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=GlobalTagsClass(
                    tags=[TagAssociationClass(tag=make_tag_urn(t)) for t in ds.tags]
                ),
            )
        )

    if ds.terms:
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=GlossaryTermsClass(
                    terms=[
                        GlossaryTermAssociationClass(urn=make_term_urn(t))
                        for t in ds.terms
                    ],
                    auditStamp=stamp,
                ),
            )
        )

    return mcps


def ground_truth() -> dict:
    """What we asserted, in a form the benchmark can check verdicts against."""
    return {
        "generated_at": NOW.isoformat(),
        "fresh_timestamp": FRESH.isoformat(),
        "stale_timestamp": STALE.isoformat(),
        "verdict_property_urn": VERDICT_PROPERTY_URN,
        "datasets": [
            {
                "urn": ds.urn,
                "platform": ds.platform,
                "name": ds.name,
                "has_description": ds.description is not None,
                "owner": ds.owner,
                "tags": ds.tags,
                "terms": ds.terms,
                "last_modified": ds.last_modified.isoformat(),
                "is_stale": ds.stale,
                "exercises": ds.exercises,
                "note": ds.note,
                "columns": [
                    {
                        "name": c.name,
                        "native_type": c.native,
                        "has_description": c.description is not None,
                        "tags": c.tags,
                        "terms": c.terms,
                    }
                    for c in ds.columns
                ],
            }
            for ds in CATALOG
        ],
    }


def write_recipe(path: Path) -> None:
    """Write the ingestion recipe.

    Written from Python with an explicit UTF-8 (no BOM) encoding and relative
    ./ paths. PowerShell's Out-File writes a UTF-8 BOM that the YAML parser
    chokes on, and absolute Windows paths trip a drive-letter parsing bug in the
    CLI ("D:" reads as a scheme).
    """
    recipe = """\
# Generated by seed/generate_seed.py - do not edit by hand.
# Run from the repo root:  datahub ingest -c ./seed/recipe.yml
source:
  type: file
  config:
    path: ./seed/seed_metadata.json

sink:
  type: datahub-rest
  config:
    server: http://localhost:8080
"""
    path.write_text(recipe, encoding="utf-8", newline="\n")


def main() -> None:
    mcps = vocabulary_mcps()
    for ds in CATALOG:
        mcps.extend(dataset_mcps(ds))

    metadata_path = SEED_DIR / "seed_metadata.json"
    # write_metadata_file resolves the path through DataHub's filesystem
    # registry, which reads the "d:" of an absolute Windows path as a URI scheme
    # and dies with "Did not find a registered class for d". A relative path has
    # no scheme to misparse. Same bug bites recipe paths — hence ./ in recipe.yml.
    write_metadata_file(Path(os.path.relpath(metadata_path, Path.cwd())), mcps)

    write_recipe(SEED_DIR / "recipe.yml")

    truth = ground_truth()
    (SEED_DIR / "ground_truth.json").write_text(
        json.dumps(truth, indent=2), encoding="utf-8", newline="\n"
    )

    by_verdict: dict[str, int] = {}
    for ds in CATALOG:
        by_verdict[ds.exercises] = by_verdict.get(ds.exercises, 0) + 1

    print(f"wrote {len(mcps)} MCPs -> {metadata_path}")
    print(f"  datasets:  {len(CATALOG)} across 2 platforms")
    print(f"  vocabulary: {len(TAGS)} tags, {len(TERMS)} glossary terms, {len(USERS)} users")
    print(f"  exercises:  {by_verdict}")
    print("wrote seed/recipe.yml and seed/ground_truth.json")
    print("\nnext: datahub ingest -c ./seed/recipe.yml")


if __name__ == "__main__":
    main()
