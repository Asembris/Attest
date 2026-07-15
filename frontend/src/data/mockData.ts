// Sample agent output for the Hero textarea — real seeded catalog URNs, so "Run Audit"
// exercises the live backend end to end. The three claims land on three different verdicts,
// and the first produces a genuine correction proposal (the model revises "contains no PII"
// and it re-verifies), which is what makes the human-checkpoint flow demonstrable.
export const sampleAgentOutput = `Findings from the data platform review:

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD) contains no PII.

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD) is owned by dana.wu.

The dataset urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD) was updated within the last 24 hours.`;

// --------------------------------------------------------------------------
// Benchmark page mock data — STILL FICTION, re-truthed next session (Phase 5).
// These numbers (300 claims, an "AI Judge" accuracy baseline) do not match the real
// benchmark (40 claims, macro-F1 1.000, Nemotron cross-family labeling). The Benchmark page
// is deliberately out of scope for this session; it is fed the real 6-run determinism data
// and the real metrics in the next session. Left here so the page keeps rendering until then.
// --------------------------------------------------------------------------

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
