// The seeded DataHub catalog, for the Hero URN autocomplete. Generated from
// seed/ground_truth.json (the same manifest the seed writes, the capture reads, and the
// anti-drift pin holds to GMS) -- so the URNs offered here are exactly the ones the live
// backend can resolve. Regenerate when the seed changes.

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
  }
];
