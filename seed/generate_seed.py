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

The unit of coverage is NOT the dataset, it is the (claim type x verdict) cell —
four claim types by three verdicts, twelve cells. A single dataset lands in
different cells for different claims: orders_fact is Supported for an ownership
claim and Contradicted for a PII claim. `exercises` below names only the headline
verdict a dataset was built for; it is a label, not a partition.

All twelve cells must be reachable against the live catalog, and Insufficient-
Coverage is reachable only where the relevant aspect is genuinely ABSENT. That is
what `no_timestamp` and `no_schema` exist for: without them every dataset carried
a lastModified and a schemaMetadata, so the freshness and schema checkers had no
path to Insufficient-Coverage at all. tests/test_coverage.py asserts all twelve
cells stay reachable, so this cannot silently regress.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from datahub.emitter.mce_builder import (
    make_dataset_urn,
    make_group_urn,
    make_tag_urn,
    make_term_urn,
    make_user_urn,
)
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.sink.file import write_metadata_file
from datahub.metadata.schema_classes import (
    AuditStampClass,
    BooleanTypeClass,
    CorpGroupInfoClass,
    CorpUserInfoClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    GlossaryNodeInfoClass,
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

NOW = datetime.now(UTC)
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

# --- the glossary hierarchy --------------------------------------------------
# Terms live under nodes, and the PII node is what makes a term a PII *signal*
# without anyone having to decide semantically that "EmailAddress sounds personal".
# Membership of the node is a governance act someone performed in the catalog; Attest
# reads the structure and infers nothing. This is the same move as policy.EXCLUSIONS:
# the semantics are declared in the data, not guessed at by a model.
#
# CustomerIdentifier is deliberately NOT under the PII node, even though it is
# customer-related and an agent will assume otherwise. A surrogate key is not personal
# data, and a checker that treats "mentions customers" as "contains PII" flags every
# table in the warehouse. That it sits outside the node is the edge case.

PII_NODE = "PII"

GLOSSARY_NODES = {
    PII_NODE: (
        "PII",
        "Personally identifiable information. A term under this node marks data that "
        "identifies a natural person.",
    ),
}


def make_node_urn(node_id: str) -> str:
    return f"urn:li:glossaryNode:{node_id}"


# term id -> (display name, definition, parent node or None)
TERMS = {
    "EmailAddress": (
        "Email Address",
        "An address identifying a person's mailbox.",
        PII_NODE,
    ),
    "PhoneNumber": ("Phone Number", "A telephone number reaching a person.", PII_NODE),
    "PersonName": (
        "Person Name",
        "The given and/or family name of a natural person.",
        PII_NODE,
    ),
    "Revenue": ("Revenue", "Gross income from business activity before costs.", None),
    "CustomerIdentifier": (
        "Customer Identifier",
        "A surrogate key identifying a customer. Not itself personal data.",
        None,
    ),
}

# The custom property an upstream classifier writes when it scans a table and finds
# personal data. Seeded because real catalogs carry exactly this: a scanner's finding,
# recorded as a property, with no tag and no glossary term behind it.
HAS_PII_PROPERTY = "hasPII"

USERS = {
    "alice.chen": ("Alice Chen", "alice.chen@example.com", "Data Engineer"),
    "bob.martinez": ("Bob Martinez", "bob.martinez@example.com", "Analytics Lead"),
    "carol.davis": ("Carol Davis", "carol.davis@example.com", "Marketing Analyst"),
    "dana.wu": ("Dana Wu", "dana.wu@example.com", "Platform Engineer"),
}

# GROUPS EXIST BECAUSE A CATALOG THAT ONLY EVER EMITS `make_user_urn` CANNOT EXERCISE
# `CorpGroup` OWNERSHIP, AND THAT IS EXACTLY HOW THE GAP HID (Session 32).
#
# `Owner.owner` is the GraphQL union CorpUser | CorpGroup. `client.DATASET_QUERY` selected
# only the CorpUser arm, so a group-owned dataset came back `{"owner": {}}` and was refused
# as a malformed response — every claim about it a ClaimError. Measured on an external
# catalog: 15 of 67 datasets unauditable. Nothing in this repo could see it, because every
# seeded dataset, every captured fixture, the offline tier, the live tier and the 12-cell
# matrix contained corpuser owners and NOTHING ELSE. A seed cannot exercise a shape it never
# emits — Session 5's "a fake cannot fail the way the real thing fails", one level up.
#
# So the group is seeded first and the query arm second, in that order, deliberately.
GROUPS = {
    "data-platform": (
        "Data Platform",
        "Owns the shared ingestion and warehouse infrastructure.",
    ),
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
    # CorpGroup owners, by group id. Separate from `owner` rather than folded into it
    # because `owner` is a corpuser username and both `ground_truth.json` and the
    # frontend's seeded-dataset list read it under that meaning. A dataset may carry
    # either, or both — DataHub's owners list is heterogeneous.
    owner_groups: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    terms: list[str] = field(default_factory=list)
    # Written as the `hasPII` custom property: an upstream classifier's finding.
    # None means the property is absent — the scanner never ran, which is silence.
    has_pii: bool | None = None
    stale: bool = False
    # Omit an aspect entirely, so the catalog is *silent* about it rather than
    # disagreeing. Insufficient-Coverage is only reachable when the aspect is
    # absent, so these flags are what make that verdict testable at all.
    no_timestamp: bool = False
    no_schema: bool = False
    # Which verdict this entity exists to exercise. Recorded in ground_truth.json.
    exercises: str = "Supported"
    note: str = ""

    @property
    def urn(self) -> str:
        return make_dataset_urn(platform=self.platform, name=self.name, env=ENV)

    @property
    def last_modified(self) -> datetime | None:
        if self.no_timestamp:
            return None
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
        has_pii=True,
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
        note=(
            "Fully documented. Claims about its owner, PII columns, and terms should "
            "be Supported."
        ),
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
        # The scanner ran and found nothing. A negative finding is NOT a denial: this
        # table's clean bill comes from the Verified tag (a human reviewed it), not from
        # a scanner's miss. hasPII=false therefore fires no signal and licenses nothing.
        has_pii=False,
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
        # The table carries EmailAddress (a term UNDER the PII node) while its email
        # column is tagged NonPII. That is a real conflict, not an authoring mistake,
        # and it is here on purpose: the table is *about* email, and the one column
        # that held an address was de-identified. Production catalogs look like this
        # constantly. Attest resolves it by precedence — see the module docstring of
        # attest/checkers/policy.py — and the resolution is that the column-level
        # NonPII tag wins over the table-level term.
        terms=["CustomerIdentifier", "EmailAddress"],
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
            "Insufficient-Coverage. Also the precedence case: the table carries the "
            "EmailAddress term (under the PII node) and an explicit NonPII tag. A "
            "column-scoped claim is decided by the column's own tag; the table's term "
            "does not propagate down into it."
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
        has_pii=True,
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
    Dataset(
        platform="postgres",
        name="attest_db.public.hr_headcount",
        description=(
            "Headcount and compensation by employee. Classified with tags only — the "
            "HR domain never adopted the glossary."
        ),
        owner="dana.wu",
        # PII is asserted ONLY as a globalTag. No glossary term anywhere on this
        # dataset, at table or column grain. Real catalogs look like this constantly:
        # tags are cheap and get applied, glossaries are a governance project that
        # half of an org never finishes. A classification checker that reads terms and
        # forgets tags would return a confident "PII-free: Supported" on this table
        # while a PII tag sits on it untouched — the worst verdict Attest can produce.
        # This dataset exists to make that failure impossible to ship unnoticed.
        tags=["PII", "Sensitive", "Tier1", "Verified"],
        terms=[],
        columns=[
            Column("employee_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("home_address", "string", "text", "Residential address.", tags=["PII"]),
            Column("salary_usd", "number", "numeric(12,2)", "Annual salary.", tags=["PII"]),
            Column("department", "string", "varchar(64)", "Cost centre.", tags=["NonPII"]),
        ],
        exercises="Contradicted",
        note=(
            "PII signalled by globalTag alone — zero glossary terms. A 'PII-free' claim "
            "here must be Contradicted on the strength of the tag. Guards the union "
            "semantics of the classification checker: tags and terms are BOTH evidence, "
            "and a checker that reads only one would pass every other test in the suite."
        ),
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.marketing_leads",
        description=(
            "Inbound leads from the website. Classified with the glossary only — the "
            "marketing domain adopted terms and never applied tags."
        ),
        owner="carol.davis",
        # PII asserted ONLY by glossary terms under the PII node. No PII tag anywhere,
        # at table or column grain. This is hr_headcount's mirror image, and it exists
        # for the same reason: a checker that reads tags and forgets terms returns a
        # confident "PII-free: Supported" here while EmailAddress and PersonName sit on
        # the table. Between the two datasets, neither half of the union can be dropped
        # without a test going red.
        tags=["Tier2"],
        terms=["EmailAddress", "PersonName"],
        columns=[
            Column("lead_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("work_email", "string", "varchar(255)", "Lead's work address.",
                   terms=["EmailAddress"]),
            Column("contact_name", "string", "varchar(200)", "Lead's full name.",
                   terms=["PersonName"]),
            Column("source_campaign", "string", "varchar(64)", "Attribution campaign."),
        ],
        exercises="Contradicted",
        note=(
            "PII signalled by glossary term alone — zero PII tags. A 'PII-free' claim "
            "must be Contradicted on the strength of the terms' membership of the PII "
            "node. Guards the term half of PII_SIGNALS."
        ),
    ),
    Dataset(
        platform="snowflake",
        name="analytics.product.device_telemetry",
        description=(
            "Per-device app telemetry. Never manually classified; an automated scanner "
            "found personal data in it and recorded the finding as a property."
        ),
        owner="dana.wu",
        # PII asserted ONLY by the hasPII custom property — no tag, no term. This is how
        # an upstream classifier reports a finding when nobody has done the governance
        # work of tagging afterwards, and it is the third independent way a real catalog
        # says "there is PII here". A checker reading only tags and terms calls this
        # table clean.
        tags=["Tier2"],
        terms=[],
        has_pii=True,
        columns=[
            Column("device_id", "string", "VARCHAR(64)", "Device installation key."),
            Column("ip_address", "string", "VARCHAR(45)", "Last seen IP address."),
            Column("app_version", "string", "VARCHAR(16)", "Client build."),
            Column("seen_at", "time", "TIMESTAMP_NTZ", "Last heartbeat."),
        ],
        exercises="Contradicted",
        note=(
            "PII signalled by the hasPII custom property alone — no tag, no term. A "
            "'PII-free' claim must be Contradicted. Guards the property half of "
            "PII_SIGNALS."
        ),
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.audit_log",
        description=(
            "Append-only record of privileged actions. Nobody ever classified the table "
            "itself; one column was tagged when it was added."
        ),
        owner="dana.wu",
        # The INVERSE of email_campaign_stats, and the case that proves the precedence
        # rule generalizes instead of being special-cased to NonPII. The table carries no
        # PII signal of any kind — no tag, no term, no property — while `actor_email` is
        # explicitly tagged PII at column grain.
        #
        # Two things must follow, and neither did before this dataset existed:
        #   1. "actor_email is PII-free" is CONTRADICTED. The column's own tag decides,
        #      exactly as the NonPII tag decides for recipient_email_hash.
        #   2. "audit_log is PII-free" is CONTRADICTED too. A table-level PII claim is
        #      existential, so a PII column settles it. A checker that only reads
        #      table-level metadata answers "the catalog is silent" here — and if anyone
        #      ever adds a Verified tag to this table, it would answer SUPPORTED, and
        #      certify a table holding email addresses as clean.
        tags=["Tier2"],
        terms=[],
        columns=[
            Column("entry_id", "string", "uuid", "Primary key.", tags=["NonPII"]),
            Column("actor_email", "string", "varchar(255)", "Who performed the action.",
                   tags=["PII"]),
            Column("action", "string", "varchar(64)", "What they did."),
            Column("occurred_at", "time", "timestamptz", "When they did it."),
        ],
        exercises="Contradicted",
        note=(
            "Column-only PII signal: the table carries none. Proves precedence is about "
            "GRAIN, not about NonPII — and pins that column signals propagate UP into a "
            "table-scoped claim, while table signals never propagate DOWN into a column."
        ),
    ),
    # --- aspect-silent stubs -------------------------------------------------
    # Insufficient-Coverage is only reachable when the relevant aspect is ABSENT.
    # Ownership and classification already have silent datasets above (raw_events,
    # legacy_accounts). Freshness and schema did not — every other dataset carries
    # a lastModified and a schemaMetadata — so those two claim types could only ever
    # return Supported or Contradicted. These two stubs close that hole. Each omits
    # exactly one aspect and is fully documented otherwise, so a test that lands here
    # can only have landed here for one reason.
    Dataset(
        platform="snowflake",
        name="analytics.staging.pipeline_scratch",
        description=(
            "Scratch output of an ad-hoc backfill. Registered in the catalog, but "
            "nothing records when it last ran."
        ),
        owner="dana.wu",
        tags=["Tier2"],
        terms=[],
        columns=[
            Column("batch_id", "string", "VARCHAR(36)", "Backfill batch key.", tags=["NonPII"]),
            Column("row_count", "number", "NUMBER(12,0)", "Rows written by the batch."),
            Column("status", "string", "VARCHAR(16)", "Batch outcome."),
        ],
        no_timestamp=True,
        exercises="Insufficient-Coverage",
        note=(
            "Has schema, owner, and a tag — but NO lastModified. A freshness claim "
            "('refreshed daily', 'updated in the last 24h') must be "
            "Insufficient-Coverage: the catalog does not know when this last ran, "
            "which is not the same as knowing it is stale. This is the only dataset "
            "that isolates the freshness-silent case."
        ),
    ),
    Dataset(
        platform="postgres",
        name="attest_db.public.external_report",
        description=(
            "A vendor-delivered report registered for lineage. The columns live in "
            "the vendor's system; no schema has ever been ingested."
        ),
        owner="bob.martinez",
        tags=["Tier2"],
        terms=["Revenue"],
        columns=[],
        no_schema=True,
        exercises="Insufficient-Coverage",
        note=(
            "Has owner, timestamp, tag, and term — but NO schemaMetadata. A schema "
            "claim ('has a revenue_amount column') must be Insufficient-Coverage, NOT "
            "Contradicted: absence of a schema is not absence of a column. This is the "
            "only dataset that isolates the schema-silent case."
        ),
    ),
    # THE ONLY GROUP-OWNED DATASET, and it exists for exactly one reason: to be the shape
    # every other seeded dataset is not. Its owner is a `urn:li:corpGroup:` rather than a
    # `urn:li:corpuser:`, which is the shape that read back as a malformed response until
    # `DATASET_QUERY` grew its `... on CorpGroup` arm (Session 32).
    #
    # It carries NO PII signals, NO Verified marker and an ordinary fresh timestamp on
    # purpose: it must isolate the owner-type variable and nothing else, so a change in its
    # verdicts can only be about how ownership is READ. It is deliberately NOT named
    # anything matching "custo"/"customer" — `test_discovery_live` searches that string and
    # then resolves every hit over GraphQL.
    Dataset(
        platform="snowflake",
        name="analytics.platform.ingest_metrics",
        description=(
            "Per-run counters for the warehouse ingestion jobs. Owned by a team, not "
            "by a person."
        ),
        owner=None,
        owner_groups=["data-platform"],
        tags=["Tier2"],
        terms=[],
        columns=[
            Column("run_id", "string", "VARCHAR(36)", "Ingestion run key.", tags=["NonPII"]),
            Column("rows_loaded", "number", "NUMBER(12,0)", "Rows written by the run."),
            Column("started_at", "time", "TIMESTAMP_NTZ", "When the run started."),
        ],
        exercises="Supported",
        note=(
            "The ONLY dataset owned by a CorpGroup rather than a CorpUser. An ownership "
            "claim naming urn:li:corpGroup:data-platform must be Supported, and one "
            "naming anyone else must be Contradicted — the same rules as every other "
            "dataset, which is the whole point. Before the CorpGroup arm existed in "
            "DATASET_QUERY this dataset was unreadable and every claim about it came "
            "back as a ClaimError."
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

    for node_id, (name, definition) in GLOSSARY_NODES.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_node_urn(node_id),
                aspect=GlossaryNodeInfoClass(name=name, definition=definition),
            )
        )

    for term_id, (name, definition, parent) in TERMS.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_term_urn(term_id),
                aspect=GlossaryTermInfoClass(
                    name=name,
                    definition=definition,
                    termSource="INTERNAL",
                    # Membership of the PII node is what makes this term a PII signal.
                    # Declared here, in the catalog, so Attest reads it rather than
                    # inferring that "EmailAddress" sounds personal.
                    parentNode=make_node_urn(parent) if parent else None,
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

    # The group must exist as an ENTITY, not merely as a URN inside an ownership aspect.
    # DataHub will resolve the `... on CorpGroup` arm off the reference either way, but a
    # group with no CorpGroupInfo has no name in the UI — and the fixture would then be
    # pinning a shape nobody would ship.
    for group_id, (display, description) in GROUPS.items():
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=make_group_urn(group_id),
                aspect=CorpGroupInfoClass(
                    displayName=display,
                    description=description,
                    admins=[],
                    members=[],
                    groups=[],
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
    # The audit stamp on an aspect records when *we wrote the metadata*, which is
    # not the same thing as when the dataset itself last changed. Only the latter
    # is a freshness claim's ground truth, and a no_timestamp dataset has none —
    # so aspect stamps fall back to NOW and never leak in as a timestamp.
    stamp = audit(ds.last_modified or NOW)

    mcps.append(
        MetadataChangeProposalWrapper(
            entityUrn=ds.urn,
            aspect=DatasetPropertiesClass(
                name=ds.name.split(".")[-1],
                qualifiedName=ds.name,
                description=ds.description,
                lastModified=(
                    TimeStampClass(time=millis(ds.last_modified), actor=ACTOR)
                    if ds.last_modified
                    else None
                ),
                customProperties={
                    # Only real catalog signals live here — no seed scaffolding. `seeded_by`
                    # and `exercises_verdict` (the answer-key verdict a dataset is built to
                    # exercise) were dropped: the answer key sitting on the audited entity
                    # made independent verification read as circular, and a `seeded_by` of
                    # this script announced the whole catalog as synthetic. Neither was read
                    # by any checker; the coverage verdict already lives in ground_truth.json.
                    #
                    # `hasPII` stays — it is load-bearing (PII_SIGNALS #3 in policy.py, the
                    # property-only witness is device_telemetry). Absent unless a classifier
                    # scanned this table; absence is silence, not a clean bill.
                    **(
                        {HAS_PII_PROPERTY: "true" if ds.has_pii else "false"}
                        if ds.has_pii is not None
                        else {}
                    ),
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

    # Deliberately omitted for the schema-silent dataset: a catalog entry can be
    # registered without its schema ever being ingested.
    if not ds.no_schema:
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
    if ds.owner or ds.owner_groups:
        owners = [
            OwnerClass(owner=make_user_urn(u), type=OwnershipTypeClass.TECHNICAL_OWNER)
            for u in ([ds.owner] if ds.owner else [])
        ] + [
            OwnerClass(owner=make_group_urn(g), type=OwnershipTypeClass.TECHNICAL_OWNER)
            for g in ds.owner_groups
        ]
        mcps.append(
            MetadataChangeProposalWrapper(
                entityUrn=ds.urn,
                aspect=OwnershipClass(owners=owners, lastModified=stamp),
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
        # The three independent ways this catalog says "there is PII here". Recorded so
        # the benchmark can assert that each one is load-bearing on its own.
        "pii_node_urn": make_node_urn(PII_NODE),
        "pii_node_terms": sorted(
            make_term_urn(t) for t, (_, _, parent) in TERMS.items() if parent == PII_NODE
        ),
        "has_pii_property": HAS_PII_PROPERTY,
        "datasets": [
            {
                "urn": ds.urn,
                "platform": ds.platform,
                "name": ds.name,
                "has_description": ds.description is not None,
                "owner": ds.owner,
                "owner_groups": ds.owner_groups,
                "tags": ds.tags,
                "terms": ds.terms,
                "has_pii": ds.has_pii,
                "last_modified": ds.last_modified.isoformat() if ds.last_modified else None,
                "is_stale": ds.stale,
                "has_schema": not ds.no_schema,
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
    pii_terms = sum(1 for _, _, parent in TERMS.values() if parent == PII_NODE)
    print(
        f"  vocabulary: {len(TAGS)} tags, {len(TERMS)} glossary terms "
        f"({pii_terms} under the {PII_NODE} node), {len(GLOSSARY_NODES)} node(s), "
        f"{len(USERS)} users"
    )
    print(f"  exercises:  {by_verdict}")
    print("wrote seed/recipe.yml and seed/ground_truth.json")
    print("\nnext: datahub ingest -c ./seed/recipe.yml")


if __name__ == "__main__":
    main()
