export type Verdict = 'supported' | 'contradicted' | 'insufficient';

export interface ColumnInfo {
  name: string;
  type: string;
  pii: boolean;
  description: string;
}

export interface Evidence {
  table: string;
  owner: string;
  steward: string;
  lastUpdated: string;
  rowCount: number;
  piiTag: boolean;
  freshness: string;
  columns: ColumnInfo[];
  sourceSystem: string;
  zone: 'raw' | 'curated' | 'consumption';
}

export interface Claim {
  id: string;
  text: string;
  verdict: Verdict;
  confidence: number;
  explanation: string;
  explanationSource: 'ai' | 'template';
  evidence: Evidence;
  correctionAttempt?: {
    originalText: string;
    correctedText: string;
    deceptionType: string;
    explanation: string;
  };
}

export interface AuditReceipt {
  totalClaims: number;
  supported: number;
  contradicted: number;
  insufficient: number;
  timeTakenMs: number;
  tokensUsed: number;
  cost: string;
  modelVersion: string;
  catalogVersion: string;
}

export const auditReceipt: AuditReceipt = {
  totalClaims: 6,
  supported: 3,
  contradicted: 2,
  insufficient: 1,
  timeTakenMs: 4218,
  tokensUsed: 8412,
  cost: '$0.0003',
  modelVersion: 'attest-v2.4.1',
  catalogVersion: 'datahub-2024.11.18',
};

export const sampleAgentOutput = `Based on my analysis of the customer data platform:

1. The customers table contains no personal data — it only stores company names and account IDs.
2. The transactions table is updated in real-time with sub-second latency.
3. All records in the orders table have a valid foreign key to the customers table.
4. The user_events table is partitioned by event_date and contains no PII columns.
5. The payments table is owned by the Finance team and refreshed daily.
6. The loyalty_points table has complete coverage for all active customers.`;

export const claims: Claim[] = [
  {
    id: 'clm-001',
    text: 'The customers table contains no personal data — it only stores company names and account IDs.',
    verdict: 'contradicted',
    confidence: 0.97,
    explanation:
      'The customers table contains 6 columns flagged as PII, including personal_email, phone_number, and home_address. The claim that it holds no personal data is false.',
    explanationSource: 'ai',
    evidence: {
      table: 'warehouse.customers',
      owner: 'Priya Raman',
      steward: 'Data Platform Team',
      lastUpdated: '2024-11-14T08:32:00Z',
      rowCount: 2_847_103,
      piiTag: true,
      freshness: '4 days ago',
      zone: 'curated',
      sourceSystem: 'Snowflake — PROD',
      columns: [
        { name: 'customer_id', type: 'UUID', pii: false, description: 'Primary key' },
        { name: 'company_name', type: 'VARCHAR(255)', pii: false, description: 'Legal entity name' },
        { name: 'account_id', type: 'VARCHAR(64)', pii: false, description: 'External CRM account ref' },
        { name: 'personal_email', type: 'VARCHAR(320)', pii: true, description: 'Billing contact email' },
        { name: 'phone_number', type: 'VARCHAR(32)', pii: true, description: 'Primary contact number' },
        { name: 'home_address', type: 'TEXT', pii: true, description: 'Registered address' },
        { name: 'tier', type: 'ENUM', pii: false, description: 'gold | silver | bronze' },
        { name: 'created_at', type: 'TIMESTAMP', pii: false, description: 'Account creation timestamp' },
      ],
    },
    correctionAttempt: {
      originalText:
        'The customers table contains no personal data — it only stores company names and account IDs.',
      correctedText:
        'The company_profiles table contains no personal data — it only stores company names and account IDs.',
      deceptionType: 'Subject Swap',
      explanation:
        'The agent replaced "customers" with "company_profiles" — a table that genuinely has no PII — rather than correcting the false claim about the customers table. This is a subject swap: the original claim was about the customers table, but the correction silently redirects to a different table to appear right without being right.',
    },
  },
  {
    id: 'clm-002',
    text: 'The transactions table is updated in real-time with sub-second latency.',
    verdict: 'contradicted',
    confidence: 0.94,
    explanation:
      'The transactions table is refreshed via a batch pipeline every 15 minutes. The catalog lists the update cadence as "batch — 15 min" with a measured median lag of 872 seconds. No real-time or streaming ingestion is configured.',
    explanationSource: 'ai',
    evidence: {
      table: 'warehouse.transactions',
      owner: 'Marcus Holt',
      steward: 'Payments Data Team',
      lastUpdated: '2024-11-18T06:00:00Z',
      rowCount: 184_402_881,
      piiTag: true,
      freshness: '6 hours ago',
      zone: 'curated',
      sourceSystem: 'Snowflake — PROD',
      columns: [
        { name: 'transaction_id', type: 'UUID', pii: false, description: 'Primary key' },
        { name: 'customer_id', type: 'UUID', pii: false, description: 'FK → customers' },
        { name: 'amount', type: 'DECIMAL(12,2)', pii: false, description: 'Transaction amount (USD)' },
        { name: 'status', type: 'ENUM', pii: false, description: 'pending | settled | failed' },
        { name: 'card_last4', type: 'CHAR(4)', pii: true, description: 'Last 4 digits of card' },
        { name: 'processed_at', type: 'TIMESTAMP', pii: false, description: 'Processing timestamp' },
      ],
    },
  },
  {
    id: 'clm-003',
    text: 'All records in the orders table have a valid foreign key to the customers table.',
    verdict: 'supported',
    confidence: 0.99,
    explanation:
      'A referential integrity check confirms 100% of 4.2M order records resolve to an existing customer. The FK constraint orders.customer_id → customers.customer_id is enforced and no orphaned rows were detected in the last validation run.',
    explanationSource: 'ai',
    evidence: {
      table: 'warehouse.orders',
      owner: 'Elena Vasquez',
      steward: 'Commerce Data Team',
      lastUpdated: '2024-11-18T04:15:00Z',
      rowCount: 4_198_772,
      piiTag: false,
      freshness: '2 hours ago',
      zone: 'curated',
      sourceSystem: 'Snowflake — PROD',
      columns: [
        { name: 'order_id', type: 'UUID', pii: false, description: 'Primary key' },
        { name: 'customer_id', type: 'UUID', pii: false, description: 'FK → customers (validated)' },
        { name: 'order_total', type: 'DECIMAL(10,2)', pii: false, description: 'Order subtotal' },
        { name: 'currency', type: 'CHAR(3)', pii: false, description: 'ISO 4217 code' },
        { name: 'placed_at', type: 'TIMESTAMP', pii: false, description: 'Order placement time' },
        { name: 'fulfilled_at', type: 'TIMESTAMP', pii: false, description: 'Fulfillment timestamp' },
      ],
    },
  },
  {
    id: 'clm-004',
    text: 'The user_events table is partitioned by event_date and contains no PII columns.',
    verdict: 'supported',
    confidence: 0.96,
    explanation:
      'The table is partitioned on event_date (DATE type, 1,847 partitions). A column-level PII scan returned no flagged columns. Event payloads are hashed and IP addresses are truncated to /24 before ingestion.',
    explanationSource: 'ai',
    evidence: {
      table: 'analytics.user_events',
      owner: 'Dev Patel',
      steward: 'Analytics Platform Team',
      lastUpdated: '2024-11-18T07:45:00Z',
      rowCount: 9_204_118_336,
      piiTag: false,
      freshness: '15 minutes ago',
      zone: 'raw',
      sourceSystem: 'Kafka → S3 → Snowflake',
      columns: [
        { name: 'event_id', type: 'UUID', pii: false, description: 'Primary key' },
        { name: 'event_date', type: 'DATE', pii: false, description: 'Partition key' },
        { name: 'event_type', type: 'VARCHAR(64)', pii: false, description: 'click | view | scroll' },
        { name: 'session_hash', type: 'CHAR(64)', pii: false, description: 'SHA-256 session hash' },
        { name: 'ip_truncated', type: 'VARCHAR(15)', pii: false, description: 'IP masked to /24' },
        { name: 'payload', type: 'VARIANT', pii: false, description: 'JSON event payload' },
      ],
    },
  },
  {
    id: 'clm-005',
    text: 'The payments table is owned by the Finance team and refreshed daily.',
    verdict: 'supported',
    confidence: 0.92,
    explanation:
      'Catalog ownership is registered to "Finance — Revenue Ops" (group owner). The ingestion SLA is daily at 02:00 UTC via the payments_batch_dag Airflow pipeline. Last successful run completed at 02:14 UTC today.',
    explanationSource: 'ai',
    evidence: {
      table: 'finance.payments',
      owner: 'Finance — Revenue Ops',
      steward: 'Sarah Chen',
      lastUpdated: '2024-11-18T02:14:00Z',
      rowCount: 52_410_887,
      piiTag: true,
      freshness: '5 hours ago',
      zone: 'curated',
      sourceSystem: 'Stripe → Snowflake',
      columns: [
        { name: 'payment_id', type: 'UUID', pii: false, description: 'Primary key' },
        { name: 'invoice_id', type: 'UUID', pii: false, description: 'FK → invoices' },
        { name: 'amount', type: 'DECIMAL(12,2)', pii: false, description: 'Payment amount' },
        { name: 'payment_method', type: 'ENUM', pii: false, description: 'card | ach | wire' },
        { name: 'bank_account_last4', type: 'CHAR(4)', pii: true, description: 'Last 4 of bank acct' },
        { name: 'settled_at', type: 'TIMESTAMP', pii: false, description: 'Settlement timestamp' },
      ],
    },
  },
  {
    id: 'clm-006',
    text: 'The loyalty_points table has complete coverage for all active customers.',
    verdict: 'insufficient',
    confidence: 0.41,
    explanation:
      'The catalog has no metadata on coverage or nullability for the loyalty_points table. The table exists and is registered, but no freshness, row-count, or completeness metrics have been published. We cannot confirm or deny this claim from available catalog metadata.',
    explanationSource: 'template',
    evidence: {
      table: 'marketing.loyalty_points',
      owner: 'Unassigned',
      steward: '—',
      lastUpdated: '2024-09-02T00:00:00Z',
      rowCount: 0,
      piiTag: false,
      freshness: '77 days ago',
      zone: 'raw',
      sourceSystem: 'Unknown — not ingested',
      columns: [
        { name: 'customer_id', type: 'UUID', pii: false, description: 'FK → customers' },
        { name: 'points_balance', type: 'INTEGER', pii: false, description: 'No metadata available' },
        { name: 'tier', type: 'VARCHAR(32)', pii: false, description: 'No metadata available' },
        { name: 'updated_at', type: 'TIMESTAMP', pii: false, description: 'No metadata available' },
      ],
    },
  },
];

export interface BenchmarkRow {
  metric: string;
  attest: number;
  aiJudge: number;
  unit: string;
}

export const benchmarkTable: BenchmarkRow[] = [
  { metric: 'Accuracy', attest: 0.97, aiJudge: 0.71, unit: '%' },
  { metric: 'Precision', attest: 0.96, aiJudge: 0.68, unit: '%' },
  { metric: 'Recall', attest: 0.95, aiJudge: 0.74, unit: '%' },
  { metric: 'F1 Score', attest: 0.955, aiJudge: 0.709, unit: '' },
  { metric: 'Determinism', attest: 1.0, aiJudge: 0.2, unit: '%' },
];

export interface ConfusionMatrix {
  actual: string;
  predicted: { supported: number; contradicted: number; insufficient: number };
}

export const confusionMatrix: ConfusionMatrix[] = [
  { actual: 'Supported', predicted: { supported: 287, contradicted: 4, insufficient: 9 } },
  { actual: 'Contradicted', predicted: { supported: 6, contradicted: 291, insufficient: 3 } },
  { actual: 'Insufficient', predicted: { supported: 2, contradicted: 5, insufficient: 293 } },
];

export const determinismRuns = [
  { run: 1, answer: 'Contradicted', source: 'Attest' },
  { run: 2, answer: 'Contradicted', source: 'Attest' },
  { run: 3, answer: 'Contradicted', source: 'Attest' },
  { run: 4, answer: 'Contradicted', source: 'Attest' },
  { run: 5, answer: 'Contradicted', source: 'Attest' },
  { run: 1, answer: 'Supported', source: 'AI Judge' },
  { run: 2, answer: 'Contradicted', source: 'AI Judge' },
  { run: 3, answer: 'Insufficient', source: 'AI Judge' },
  { run: 4, answer: 'Supported', source: 'AI Judge' },
  { run: 5, answer: 'Contradicted', source: 'AI Judge' },
];
