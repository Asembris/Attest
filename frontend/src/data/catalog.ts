// The seeded DataHub catalog, feeding ONE control: the dataset filter in ClaimsExplorer.
// It is NOT the Hero URN picker (that searches the live catalog over MCP), and it is NOT a
// fallback for that search -- answering a failed search from this file would show a human a
// "catalog" that is a build artifact, which `just e2e-sabotage` exists to keep red.
//
// HAND-MAINTAINED against the committed snapshots in tests/fixtures/snapshots/ (one per
// seeded dataset, held equal to live GMS by test_fixture_drift.py). There is no generator.
// Update it when the seed changes; tests/test_frontend_catalog.py fails if you do not.
//
// `owner` is inert UI metadata -- only `urn` and `name` are rendered. A CorpUser owner is
// carried as a bare id (`alice.chen`); a CorpGroup owner as its canonical URN
// (`urn:li:corpGroup:data-platform`), so a team is never displayed as if it were a person.

export interface SeededDataset {
  urn: string;
  name: string;
  platform: string;
  owner: string | null;
}

export const seededDatasets: SeededDataset[] = [
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)",
    "name": "analytics.customers.customer_profile",
    "platform": "snowflake",
    "owner": "alice.chen"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_contact,PROD)",
    "name": "analytics.customers.customer_contact",
    "platform": "snowflake",
    "owner": "alice.chen"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders.orders_fact,PROD)",
    "name": "analytics.orders.orders_fact",
    "platform": "snowflake",
    "owner": "bob.martinez"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.marketing.email_campaign_stats,PROD)",
    "name": "analytics.marketing.email_campaign_stats",
    "platform": "snowflake",
    "owner": "carol.davis"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.finance.revenue_daily,PROD)",
    "name": "analytics.finance.revenue_daily",
    "platform": "snowflake",
    "owner": "bob.martinez"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD)",
    "name": "analytics.staging.raw_events",
    "platform": "snowflake",
    "owner": null
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD)",
    "name": "attest_db.public.users",
    "platform": "postgres",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.payment_methods,PROD)",
    "name": "attest_db.public.payment_methods",
    "platform": "postgres",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.support_tickets,PROD)",
    "name": "attest_db.public.support_tickets",
    "platform": "postgres",
    "owner": "carol.davis"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.legacy_accounts,PROD)",
    "name": "attest_db.public.legacy_accounts",
    "platform": "postgres",
    "owner": null
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD)",
    "name": "attest_db.public.hr_headcount",
    "platform": "postgres",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.marketing_leads,PROD)",
    "name": "attest_db.public.marketing_leads",
    "platform": "postgres",
    "owner": "carol.davis"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.product.device_telemetry,PROD)",
    "name": "analytics.product.device_telemetry",
    "platform": "snowflake",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.audit_log,PROD)",
    "name": "attest_db.public.audit_log",
    "platform": "postgres",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.pipeline_scratch,PROD)",
    "name": "analytics.staging.pipeline_scratch",
    "platform": "snowflake",
    "owner": "dana.wu"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.external_report,PROD)",
    "name": "attest_db.public.external_report",
    "platform": "postgres",
    "owner": "bob.martinez"
  },
  {
    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.platform.ingest_metrics,PROD)",
    "name": "analytics.platform.ingest_metrics",
    "platform": "snowflake",
    "owner": "urn:li:corpGroup:data-platform"
  }
];
