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

// `flagged` is a TERMINAL, UN-APPROVABLE state, not a warning: the run violated one of its
// own trajectory invariants (a checker spent tokens, an explanation skipped the guard), so
// `service.approve` refuses it with a 409 and nothing it produced may reach the catalog.
// It was missing here, so the UI rendered a flagged run as though it were merely parked and
// offered a Submit that could only ever 409.
export type RunStatus = 'complete' | 'awaiting-review' | 'flagged';

// Whether a human has cleared a claim's VERDICT for the catalog (report.PublicationStatus).
// SEPARATE from a correction's review, and that separation is the whole Option A decision:
// publishing a verdict is not accepting a correction, and fusing them kept every Supported,
// Insufficient-Coverage, and STOOD_FIRM verdict out of DataHub entirely.
export type PublicationStatus = 'pending' | 'published' | 'withheld';

// How a claim artifact's verdict reads in the catalog RIGHT NOW (retrieval.ReadState).
// Four states because an absent verdict is not one fact:
//   complete     the catalog holds the verdict
//   pending-lag  the write LANDED and DataHub's index has not shown it yet. Transient
//                (~2s, measured). NOTHING TO REPAIR — never offer a retry for this
//   incomplete   the write FAILED at a step. No verdict is coming. Repairable
//   unknown      no verdict, and Attest's record cannot say whether one was attempted.
//                NOT a synonym for incomplete — treating it as one is reading absence as
//                an answer, which is the mistake this whole product exists to catch
export type ReadState = 'complete' | 'pending-lag' | 'incomplete' | 'unknown';

/** Which of the three catalog writes did not land. `null` when they all did. */
export type WriteStep = 'upsert' | 'report' | 'tag';

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

/** Whether a human cleared this claim's VERDICT for the catalog. Distinct from the
 *  correction's review — see PublicationStatus. Every audited claim has one of these,
 *  whatever its verdict: a verdict Attest never records is indistinguishable from a claim
 *  Attest never checked, which is the ambiguity the whole product exists to refuse. */
export interface PublicationView {
  status: PublicationStatus;
  reviewer: string;
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
  publication: PublicationView;
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

/** POST /audit/{run_id}/approve — one human's call on one audited claim.
 *
 *  TWO SEPARATE FIELDS, and this mirror carried a single `accept: boolean` for a full
 *  session after the backend split them — which the backend REFUSES (`extra="forbid"`), so
 *  every approve the UI sent came back 422. They are different decisions about different
 *  things: "your claim was wrong (publish that), and the fix you proposed is also wrong
 *  (reject that)" is a position a human can hold, and one boolean could not say it.
 *
 *  Both are optional. `undefined` is "no opinion", which is NOT "no": an unnamed claim
 *  stays pending and the run stays parked, decidable later. */
export interface DecisionRequest {
  claim_index: number;
  /** True publishes this claim's VERDICT to the catalog as a claim artifact; false
   *  withholds it. Every audited claim needs one, whatever its verdict. */
  publish?: boolean;
  /** True accepts the revision the agent proposed for a Contradicted claim. Only
   *  meaningful where there is a proposal. Does NOT publish anything. */
  accept_correction?: boolean;
  reviewer?: string;
  note?: string;
}

/** What the catalog did with a published verdict. Reported separately from the decision: a
 *  write can fail independently while the human decision still stands. */
export interface WriteBackView {
  target_urn: string;
  ok: boolean;
  detail: string;
  /** The claim artifact's URN. Content-addressed, so it names the same artifact every time. */
  claim_urn: string;
  /** WHICH of the three writes did not land. "It failed" is not actionable: a failed
   *  `report` left a claim with no verdict, while a failed `tag` left a verdict that is
   *  entirely correct and merely not findable by search yet. */
  failed_step: WriteStep | null;
}

/** Response of POST /audit/{run_id}/writeback — the repair path. Approves nothing. */
export interface WriteBackResponse {
  run_id: string;
  writebacks: WriteBackView[];
}

// --- GET /claims — what a reader inherits from the catalog -------------------

/** One verdict a claim has had, at one point in time. Append-only: re-auditing a claim
 *  ADDS an event rather than replacing one, so a Supported that later became Contradicted
 *  carries both, in order, with who signed each off. */
export interface VerdictEventView {
  at: string;
  verdict: Verdict;
  audit_run: string;
  reviewer: string;
  decision: string;
  /** The catalog fields this verdict was decided against, STRUCTURED and absence-preserving.
   *  `value === null` is the catalog being SILENT (the justification for
   *  Insufficient-Coverage), kept distinct from '' and []. It used to be a flattened string
   *  with every null row dropped — so a reader could not tell "said nothing" from "said empty". */
  evidence: EvidenceView[];
  /** The identity of the catalog snapshot this verdict was decided against — which state of
   *  the world it held true for. Captured at audit time and stored, never recomputed. */
  snapshot_id: string;
  /** DataHub's own SUCCESS/FAILURE/ERROR. A LOSSY projection for its health rollup —
   *  shown for transparency and read by nothing. The verdict above is authoritative. */
  native_type: string;
}

/** One claim artifact as the CATALOG holds it, plus whether Attest can vouch for it. */
export interface ClaimView {
  claim_urn: string;
  target_urn: string;
  claim_type: ClaimType;
  grain: 'table' | 'column';
  description: string;
  asserted: Record<string, unknown>;
  /** The LATEST verdict, or null when the catalog holds none. Null is NOT a verdict —
   *  read `state` to find out why it is null. */
  verdict: Verdict | null;
  state: ReadState;
  /** Which write failed, when `state` is `incomplete`. From Attest's store: DataHub
   *  cannot know it. This is what makes incomplete actionable and pending-lag not. */
  failed_step: WriteStep | null;
  /** The run to hand POST /audit/{run_id}/writeback to repair this claim. */
  audit_run: string;
  tags: string[];
  /** The verdict TAG disagrees with the latest verdict — detected from the artifact ALONE,
   *  so a store=None reader sees it too. The tag is a derived index written last, so a crash
   *  between `report` and `tag` leaves a correct verdict a verdict-search cannot find.
   *  On-read detection, not a reconciler. False when there is no verdict. */
  stale_tag: boolean;
  history: VerdictEventView[];
  /** The catalog's OWN count of this claim's verdict events. When it exceeds `history.length`
   *  the history was TRUNCATED at the 50-event read cap — the oldest events are absent from
   *  this listing, NOT from the catalog. The run-event twin of `RetrievalView.total`. */
  history_total: number;
  /** True when `history_total > history.length`: older verdicts are not shown. Full
   *  timeseries pagination is deferred; naming the truncation is not. */
  history_truncated: boolean;
}

/** WHERE each predicate was applied. Part of the answer, not diagnostics: "retrievable
 *  from DataHub" is true, "fully queryable in DataHub" is not, and a UI that hid the
 *  difference would let a reader believe the catalog answered what Attest answered. */
export interface RetrievalView {
  entry_point: string;
  pushed_down: string[];
  filtered_locally: string[];
  considered: number;
  /** The catalog's OWN total for this entry point's server-side scope. `total > considered`
   *  means the listing was TRUNCATED at the limit — there are more artifacts the catalog
   *  holds that this page did not return, and `note` says so. A claim past the limit is
   *  absent from this listing, NOT absent from the catalog. */
  total: number;
  note: string;
}

export interface ClaimsResponse {
  claims: ClaimView[];
  retrieval: RetrievalView;
}

/** The filters GET /claims takes. `target_urn` is pushed down to DataHub; with it named,
 *  it is the ONLY one that can be. */
export interface ClaimFilters {
  target_urn?: string;
  verdict?: Verdict | '';
  claim_type?: ClaimType | '';
  reviewer?: string;
  since?: string;
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
  // The DataHub UI origin to deep-link to, supplied by the backend. The built SPA has no
  // runtime env, so this is how `datahubDatasetUrl` learns the host without hardcoding :9002.
  datahub_ui_url: string;
}

// --- derived helpers, shared by components ---------------------------------

export const VERDICT_KEY: Record<Verdict, 'supported' | 'contradicted' | 'insufficient'> = {
  Supported: 'supported',
  Contradicted: 'contradicted',
  'Insufficient-Coverage': 'insufficient',
};

/** Corrections that re-verified clean and are waiting on a human (record.proposals).
 *
 *  NOT the same as what a run is parked on — see `awaitingPublication`. A claim can have no
 *  proposal at all and still be holding the run open, because publishing a verdict is its
 *  own act. */
export function proposals(record: AuditRecord): ClaimRecord[] {
  return record.claims.filter(
    (c) => c.correction.outcome === 'corrected' && c.correction.review === 'pending',
  );
}

/** EVERY claim whose verdict is still waiting on a human. THIS is what parks the run.
 *
 *  The UI used to count `proposals` instead, which was right until Option A and wrong
 *  after it: the checkpoint parks while ANY claim's publication is pending, and only a
 *  correction ever produced a proposal. So a four-claim run with one proposal had one
 *  decision submitted, re-parked on the other three, and could never reach `complete` —
 *  the UI reported it as settled and the run stayed open forever.
 *
 *  It is every claim on purpose. Supported and Insufficient-Coverage are findings a catalog
 *  needs too: a dataset with no Attest verdict is otherwise ambiguous between "we checked
 *  and it was fine" and "nobody ever looked", which is precisely the ambiguity this product
 *  exists to refuse.
 *
 *  This is ONE of the two axes the checkpoint parks on. It is not the whole gate — see
 *  `awaitingDecision`, which adds the correction axis. Counting only this one is the same
 *  class of bug as counting only `proposals`, one axis over. */
export function awaitingPublication(record: AuditRecord): ClaimRecord[] {
  return record.claims.filter((c) => c.publication.status === 'pending');
}

/** EVERY claim the run is still parked on — verdict unpublished OR a proposed correction
 *  unruled. THIS is what the review gate must range over.
 *
 *  The backend parks while EITHER axis is pending
 *  (`report.ClaimAudit.awaits_human = publication.awaits_human OR correction.awaits_human`),
 *  and the checkpoint is a LOOP: anything still PENDING routes straight back and re-parks.
 *  A gate that tracks only `awaitingPublication` goes blind the moment every verdict is
 *  published but a correction is not: `reviewMode` drops to false, every control retires,
 *  and the still-pending correction re-parks the run into a permanent `awaiting-review` with
 *  nothing clickable. That is the exact dead-end this set closes — the same class as the
 *  `proposals()` publication-axis bug, one axis over.
 *
 *  `accept_correction` is therefore REQUIRED-TO-FINISH, not skip-and-finish. "Optional" in
 *  the backend means the two axes are decided INDEPENDENTLY (publish now, rule the fix later;
 *  or publish the verdict AND reject the fix) — not that a corrected proposal can be left
 *  unruled while the run completes. It cannot: the run stays parked until it is decided. */
export function awaitingDecision(record: AuditRecord): ClaimRecord[] {
  return record.claims.filter(
    (c) =>
      c.publication.status === 'pending' ||
      (c.correction.outcome === 'corrected' && c.correction.review === 'pending'),
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

// The DataHub UI origin. Defaults to the local quickstart (:9002) so a bare load before
// /health returns still links somewhere sane; `setDatahubUiUrl` overrides it once the
// backend has reported its own configured value (App.tsx, on the startup health fetch).
// A module-level value rather than prop-drilling through every card that deep-links: it is
// one origin fixed for the life of the page, and the alternative is threading it through
// three component trees for a single string the backend already knows.
let datahubUiBase = 'http://localhost:9002';

/** Adopt the backend's configured DataHub UI origin (from GET /health). Called once at
 *  startup; a trailing slash is trimmed so the join below is exact. */
export function setDatahubUiUrl(url: string): void {
  if (url) datahubUiBase = url.replace(/\/+$/, '');
}

/** The DataHub UI page for a dataset URN — the live catalog behind the audit. The origin is
 *  backend-supplied (see `setDatahubUiUrl`), not hardcoded, so the deep-link follows a
 *  deployed instance instead of breaking on :9002. The URN is URL-encoded whole; DataHub
 *  decodes it on the dataset route. */
export function datahubDatasetUrl(urn: string): string {
  return `${datahubUiBase}/dataset/${encodeURIComponent(urn)}`;
}
