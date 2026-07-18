// Sample agent output for the Hero textarea — real seeded catalog URNs, so "Run Audit"
// exercises the live backend end to end. The first claim is Contradicted (hr_headcount is
// tagged PII) and produces a genuine correction proposal, which is what makes the
// human-checkpoint flow demonstrable; the second is Supported; the third is the freshness
// claim below.
//
// WHY THIS IS A FUNCTION, AND WHY THE FRESHNESS WINDOW VARIES.
// A published verdict is written to DataHub as a CONTENT-ADDRESSED artifact: its URN is a
// hash of the claim's identity (target URN, type, grain, and what it asserts). So a fixed
// sample maps to the SAME artifact forever — the second visitor to publish it appends to a
// history the first visitor already wrote, and the "watch an empty claim gain its first
// verdict" read-back is spoiled. Varying the claim's CONTENT mints a fresh URN per page
// load, so each visitor's story starts clean without deleting anything (a delete would be a
// lie here anyway — measured: DELETE /openapi/v3/entity/assertion returns 200 and leaves the
// timeseries verdict standing; only `datahub delete --hard` removes it).
//
// THE LIMITATION, STATED PLAINLY: this trick works for the FRESHNESS claim only. A freshness
// claim's identity has one free dimension — the window (`max_age_hours`) — and it can be
// varied within a band that keeps the verdict stable. The classification and ownership
// claims above CANNOT be freshened: their asserted content IS what determines the verdict,
// so changing it to get a fresh URN would change what the claim means. On a SHARED instance
// those two accumulate verdict history across visitors; the real reset is per-judge
// isolation (each judge runs their own local stack against their own catalog), which makes
// this belt-and-suspenders rather than the mechanism. See docs/deployment.md.
//
// The window is 2000–11000 hours (~83–458 days) — always far above any seeded dataset's age,
// so the freshness claim is honestly Supported and the correction loop stays out of the demo
// path (it is not about the loop). It is a REAL FreshnessClaim, decided by the real checker;
// the odd number is the price of a fresh URN, exactly as tests/test_e2e_browser.py pays it.
export function sampleAgentOutput(): string {
  const window = 2000 + Math.floor(Math.random() * 9000);
  return `Findings from the data platform review:

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.hr_headcount,PROD) contains no PII.

The dataset urn:li:dataset:(urn:li:dataPlatform:postgres,attest_db.public.users,PROD) is owned by dana.wu.

The dataset urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD) was updated within the last ${window} hours.`;
}

// The benchmark page's data now lives in benchmarkData.ts, sourced from the committed
// receipts (benchmark/results/*.json + README.md). The fabricated 300-claim / AI-judge-
// baseline mock that used to live here is gone — see that file.
