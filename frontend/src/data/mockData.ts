// Sample agent output for the Hero textarea — real seeded catalog URNs, so "Run Audit"
// exercises the live backend end to end. The three claims land on three different verdicts,
// and the first produces a genuine correction proposal (the model revises "contains no PII"
// and it re-verifies), which is what makes the human-checkpoint flow demonstrable.
export const sampleAgentOutput = `Findings from the data platform review:

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD) contains no PII.

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD) is owned by dana.wu.

The dataset urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD) was updated within the last 24 hours.`;

// The benchmark page's data now lives in benchmarkData.ts, sourced from the committed
// receipts (benchmark/results/*.json + README.md). The fabricated 300-claim / AI-judge-
// baseline mock that used to live here is gone — see that file.
