// A TypeScript mirror of Attest's `AuditRecord` (src/attest/record.py) — the exact object
// POST /audit returns and GET /audit/{run_id} serves. Hand-kept in sync with record.py;
// the field names and enum string values match the Python wire form verbatim, so the JSON
// deserializes into these with no remapping.
//
// What is deliberately ABSENT is the point: there is no `confidence`, no `rowCount`, no
// `zone`, no `steward`. Attest's verdicts come from deterministic code — there is no
// confidence score to show, and inventing one is the exact failure the product exists to
// catch. These types are the contract that keeps a fabricated field from re-appearing.

export type Verdict = 'Supported' | 'Contradicted' | 'Insufficient-Coverage';

export type ClaimType = 'freshness' | 'ownership' | 'classification' | 'schema';

export type RunStatus = 'complete' | 'awaiting-review';

export type ExplanationSource = 'model' | 'template';

export type StepKind = 'deterministic' | 'llm' | 'io';

export type CorrectionOutcome =
  | 'not-attempted'
  | 'corrected'
  | 'not-corrected'
  | 'exhausted'
  | 'stood-firm'
  | 'refused';

export type ReviewStatus = 'pending' | 'accepted' | 'rejected';

/** One catalog field that produced a verdict. `value === null` means the catalog is silent. */
export interface EvidenceView {
  field: string;
  value: unknown | null;
  note: string | null;
}

/** A disagreement between the model and the deterministic core. Never resolved, only surfaced. */
export interface ConflictView {
  kind: string;
  detail: string;
}

/** One factual token in an explanation the evidence did not support (the guard's finding). */
export interface ViolationView {
  token: string;
  kind: string;
}

/** A claim the decomposer produced and Attest refused to carry (a minted URN, a validation fail). */
export interface DroppedView {
  reason: string;
  payload: Record<string, unknown>;
}

/** One instruction-like span redacted from the agent's output before any model saw it. */
export interface FindingView {
  pattern: string;
  matched: string;
}

/** One turn of the correction loop, and what the deterministic re-check said back. */
export interface AttemptView {
  n: number;
  verdict: Verdict | null;
  reason: string;
  revised_claim: Record<string, unknown> | null;
}

/** The correction loop's record for one claim. `outcome` is one of six names, not a boolean. */
export interface CorrectionView {
  outcome: CorrectionOutcome;
  review: ReviewStatus;
  proposal: Record<string, unknown> | null;
  attempts: AttemptView[];
}

/** One audited claim: what was said, what the catalog said, and why. */
export interface ClaimRecord {
  index: number;
  claim_type: ClaimType;
  target_urn: string;
  raw_text: string;
  claim: Record<string, unknown>;

  verdict: Verdict;
  reason: string;
  evidence: EvidenceView[];

  explanation: string;
  explanation_source: ExplanationSource;
  faithful: boolean;
  faithfulness_violations: ViolationView[];
  conflicts: ConflictView[];
  rejected: string[];

  correction: CorrectionView;
}

/** A claim that could not be checked at all (e.g. entity-not-found). NOT a verdict. */
export interface ClaimErrorRecord {
  index: number;
  target_urn: string;
  claim: Record<string, unknown>;
  error: string;
}

/** One pipeline node's execution. `kind` is what trajectory verification reads. */
export interface StepView {
  seq: number;
  name: string;
  kind: StepKind;
  claim_index: number | null;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  models: string[];
  error: string | null;
}

/** What the run cost and whether it kept to its own architecture. Measured, not estimated. */
export interface Receipts {
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  /** null, never 0, when a model in the run had no price (cost.py). Render as "unknown". */
  usd: number | null;
  steps: number;

  trajectory_ok: boolean;
  trajectory_summary: string;
  rules_checked: string[];

  catalog_lookups: number;
  catalog_fetches: number;
  catalog_entities: number;
}

/** One complete audit run, as stored and as served. Mirror of record.AuditRecord. */
export interface AuditRecord {
  run_id: string;
  created_at: string;
  status: RunStatus;
  source_agent: string;
  source_text: string;

  claims: ClaimRecord[];
  errors: ClaimErrorRecord[];
  receipts: Receipts;
  steps: StepView[];

  dropped: DroppedView[];
  injection_findings: FindingView[];
}

/** POST /audit/{run_id}/approve — one human decision on one proposed correction. */
export interface DecisionRequest {
  claim_index: number;
  accept: boolean;
  reviewer?: string;
  note?: string;
}

/** What the catalog did with an accepted verdict. Reported separately: a write can fail
 *  independently while the human decision still stands. */
export interface WriteBackView {
  target_urn: string;
  ok: boolean;
  detail: string;
}

/** The settled run plus what reached DataHub. Response of the approve endpoint. */
export interface ApprovalResponse {
  audit: AuditRecord;
  writebacks: WriteBackView[];
}

/** Liveness: Attest's own, and the catalog's, kept separate on purpose. */
export interface HealthResponse {
  status: string;
  version: string;
  model: string;
  datahub: string;
}

// --- derived helpers, shared by components ---------------------------------

export const VERDICT_KEY: Record<Verdict, 'supported' | 'contradicted' | 'insufficient'> = {
  Supported: 'supported',
  Contradicted: 'contradicted',
  'Insufficient-Coverage': 'insufficient',
};

/** Corrections that re-verified clean and are waiting on a human (record.proposals). */
export function proposals(record: AuditRecord): ClaimRecord[] {
  return record.claims.filter(
    (c) => c.correction.outcome === 'corrected' && c.correction.review === 'pending',
  );
}

export function verdictCounts(record: AuditRecord): Record<Verdict, number> {
  const counts: Record<Verdict, number> = {
    Supported: 0,
    Contradicted: 0,
    'Insufficient-Coverage': 0,
  };
  for (const c of record.claims) counts[c.verdict] += 1;
  return counts;
}

/** `urn:li:corpuser:dana.wu` -> `dana.wu`. Truthful and legible; display-name resolution
 *  ("Dana Wu") is a deferred polish item — it needs a corpUser read we deliberately skip. */
export function corpuserId(urn: string): string {
  return urn.split(':').pop() ?? urn;
}

/** The DataHub UI page for a dataset URN — the live catalog behind the audit, on :9002 in
 *  the demo. The URN is URL-encoded whole; DataHub decodes it on the dataset route. */
export function datahubDatasetUrl(urn: string): string {
  return `http://localhost:9002/dataset/${encodeURIComponent(urn)}`;
}
