// THE REPLAY CLIENT. The identical exported surface of `api/client.ts`, answered from JSON
// captured off one real run instead of from `fetch`.
//
// It is swapped in by ALIAS, not by a runtime flag: `vite build --mode replay` resolves every
// `../api/client` import to this file (vite.config.ts). No component imports it, no component
// knows about it, and the production bundle's module graph never contains it — which is what
// keeps the shipped build byte-identical to the one the submission was verified against.
//
// WHAT MAKES A REPLAY HONEST. Every value below came off the wire in `spikes/capture_replay.py`
// and is served verbatim. Nothing is simulated, interpolated, or filled in:
//
//   * `health.datahub_ui_url` names `localhost:9002`, so the "View in DataHub" links point at
//     the local catalog that produced this record and are dead for a remote reader. That is
//     the real response. Rewriting it to something that resolves would be a doctored body.
//   * A DECISION SET OTHER THAN THE RECORDED ONE IS REFUSED, not simulated. The recording
//     carries one outcome; there is no honest way to show a different one.
//   * A `GET /claims` filter outside the recorded matrix is REFUSED, never answered with an
//     empty list. "Nothing was recorded for this query" and "the catalog holds nothing" are
//     different facts, and collapsing them is the exact mistake this product exists to catch —
//     it would be committing it in its own read path.
//
// The one thing this does compute is `reviewer` / `since`, applied over a captured page. That
// is not a shortcut: neither predicate can be pushed down at EITHER DataHub entry point, so
// the real backend filters them locally too, and `retrieval.filtered_locally` says so.

import health_ from '../replay/health.json';
import parked_ from '../replay/audit-parked.json';
import approval_ from '../replay/approval.json';
import claims_ from '../replay/claims.json';
import decisions_ from '../replay/decisions.json';
import manifest_ from '../replay/manifest.json';

import type {
  ApprovalResponse,
  AuditRecord,
  ClaimFilters,
  ClaimsResponse,
  ClaimView,
  DecisionRequest,
  HealthResponse,
  RetrievalView,
  WriteBackResponse,
} from './types';

// The fixtures are typed by their JSON shape, which TypeScript widens (`string` where the
// wire type wants a union). They came off the real API and are pinned to the real pydantic
// response models by tests/test_replay_fixtures.py — Python-side, where the models live — so
// the assertion here is checked, just not by tsc.
const RECORDED_HEALTH = health_ as HealthResponse;
const RECORDED_PARKED = parked_ as unknown as AuditRecord;
const RECORDED_APPROVAL = approval_ as unknown as ApprovalResponse;
const RECORDED_DECISIONS = decisions_ as unknown as DecisionRequest[];
const RECORDED_CLAIMS = claims_ as unknown as {
  filters: Record<string, string>;
  response: ClaimsResponse;
}[];

/** When this recording was made, and against what. Rendered by the replay banner.
 *
 *  `capturing_commit_dirty` is in this type because the banner has to be ABLE to say it. It is
 *  false on any shipped recording — `capture_replay.py` refuses a dirty tree and
 *  `test_replay_fixtures.py` refuses to ship a capture taken past that refusal with
 *  `--allow-dirty` — but a field the UI cannot read is a guarantee resting on two checks
 *  nobody can see from the page. Omitting it was how the flag stayed true for every capture
 *  ever taken without anything rendering a word about it. */
export const replayManifest = manifest_ as {
  captured_at: string;
  run_id: string;
  datahub_core_version: string;
  model: string;
  capturing_commit: string;
  capturing_commit_dirty: boolean;
  files: Record<string, string>;
};

/** The agent output this recording audited. The replay Hero shows THIS in its textarea —
 *  showing one text and replaying another would be the single lie this build cannot contain. */
export const replaySourceText = RECORDED_PARKED.source_text;

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = 'ApiError';
  }
}

/** A recorded response, handed back after a beat so the UI's own in-flight states are real
 *  states and not skipped frames. The delay is the browser genuinely waiting; it is never
 *  presented as the run's duration — `receipts.latency_ms` on the record is that, measured. */
function served<T>(value: T, ms = 0): Promise<T> {
  if (ms <= 0) return Promise.resolve(value);
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

function notRecorded(detail: string): never {
  throw new ApiError(
    409,
    `REPLAY · not recorded. ${detail} Nothing here is simulated, so there is nothing honest ` +
      'to show for it.',
  );
}

export async function health(): Promise<HealthResponse> {
  return served(RECORDED_HEALTH, 120);
}

/** The recorded audit, replayed. The text is ignored ONLY in the sense that the recording is
 *  of one specific text — an edited textarea gets the honest refusal below, never a verdict
 *  about a sentence nobody audited. */
export async function submitAudit(
  agentOutput: string,
  _sourceAgent = '',
): Promise<AuditRecord> {
  if (agentOutput.trim() !== replaySourceText.trim()) {
    notRecorded(
      'This replay carries one recorded audit, of the agent output shown when the page ' +
        'loaded. Auditing edited text would need a live catalog, a live model and a real ' +
        'run — none of which a static page has. Reload to restore the recorded text.',
    );
  }
  // Long enough for AuditProgress's indeterminate phase — the honest "no numbers until the
  // run returns them" screen — to be a screen a judge sees rather than a frame that flickers.
  return served(RECORDED_PARKED, 1500);
}

export async function getAudit(runId: string): Promise<AuditRecord> {
  // The SETTLED record: `GET /audit/{id}` serves what the store holds, and by the end of the
  // recorded run that is the settled one. No component calls this; the mirror is complete
  // because the surface is the contract, not because something exercises it.
  if (runId !== RECORDED_APPROVAL.audit.run_id) {
    notRecorded(`This replay holds one run (${RECORDED_APPROVAL.audit.run_id}); ${runId} is not it.`);
  }
  return served(RECORDED_APPROVAL.audit, 120);
}

/** One decision axis as a comparable key. `undefined` and absent are the same thing — the
 *  backend reads a missing field as "no opinion" — so they must compare equal here too. */
function axes(d: DecisionRequest): string {
  return `${d.claim_index}:${String(d.publish)}:${String(d.accept_correction)}`;
}

function sameDecisions(sent: DecisionRequest[], recorded: DecisionRequest[]): boolean {
  const a = sent.map(axes).sort();
  const b = recorded.map(axes).sort();
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/** The human checkpoint, replayed — and REFUSED for any decision set but the recorded one.
 *
 *  This is the sharpest thing in the replay, so it is worth being plain about. The captured
 *  response is what one human's decision really produced, including the three catalog writes.
 *  A different decision has a different outcome, and this page has no catalog to produce it
 *  against. Returning the recorded result anyway would show a judge a "published" verdict for
 *  a claim they chose to withhold — a fabricated consequence, in the flow whose entire purpose
 *  is that consequences are real and a human owns them. So it refuses, and says why. */
export async function approve(
  runId: string,
  decisions: DecisionRequest[],
): Promise<ApprovalResponse> {
  if (runId !== RECORDED_APPROVAL.audit.run_id) {
    notRecorded(`This replay holds one run (${RECORDED_APPROVAL.audit.run_id}); ${runId} is not it.`);
  }
  if (!sameDecisions(decisions, RECORDED_DECISIONS)) {
    throw new ApiError(
      409,
      'REPLAY · this recording carries one recorded outcome: all three verdicts published, ' +
        'the correction accepted. A different decision set has no recorded result — nothing ' +
        'here is simulated, so there is nothing honest to show for it. Publish all three and ' +
        'accept the correction to follow the recorded run, or run Attest locally to make your ' +
        'own decision against a real catalog.',
    );
  }
  // The write-backs in this response are three real catalog writes that really landed.
  return served(RECORDED_APPROVAL, 900);
}

export async function retryWriteback(runId: string): Promise<WriteBackResponse> {
  notRecorded(
    `The repair path re-runs a catalog write, and this page has no catalog. Every write in ` +
      `run ${runId} landed, so nothing in this recording needs repairing — the control that ` +
      'reaches here is not shown for any of its claims.',
  );
}

// --- reading the catalog back ------------------------------------------------

/** The predicates a capture is keyed by — the ones that reach DataHub. */
const RECORDED_KEYS = ['target_urn', 'verdict', 'claim_type'] as const;
/** The predicates Attest applies itself, at either entry point. Replayed the same way. */
const LOCAL_KEYS = ['reviewer', 'since'] as const;

/** Mirror `client.listClaims`'s own rule: a falsy filter value is not sent at all. */
function present(filters: ClaimFilters, key: string): string {
  const value = (filters as Record<string, string | undefined>)[key];
  return value ? String(value) : '';
}

function findCapture(filters: ClaimFilters): ClaimsResponse | null {
  const wanted = RECORDED_KEYS.map((k) => present(filters, k));
  const hit = RECORDED_CLAIMS.find((entry) =>
    RECORDED_KEYS.every((k, i) => (entry.filters[k] ?? '') === wanted[i]),
  );
  return hit ? hit.response : null;
}

/** Claim artifacts as the catalog held them, from a recorded read.
 *
 *  `reviewer` and `since` are applied HERE, over the captured page, and reported in
 *  `filtered_locally` — which is exactly what the live backend does with them, because an
 *  assertion indexes neither. `considered` and `total` are DataHub's own counts and are left
 *  alone: they describe what the catalog returned, which a local filter does not change. */
export async function listClaims(filters: ClaimFilters = {}): Promise<ClaimsResponse> {
  const capture = findCapture(filters);
  if (!capture) {
    notRecorded(
      'This recording covers the catalog-wide read, the two datasets the audited claims were ' +
        'about, and every verdict and claim-type filter over them. This combination is ' +
        'outside it. That is NOT a statement that the catalog holds no such claim — it is the ' +
        'absence of a recording, and the two are different facts.',
    );
  }

  const applied = LOCAL_KEYS.filter((k) => present(filters, k));
  if (!applied.length) return served(capture, 250);

  const reviewer = present(filters, 'reviewer').toLowerCase();
  const since = present(filters, 'since');
  const kept = capture.claims.filter((claim) => {
    const latest = claim.history[0];
    if (reviewer && !(latest?.reviewer ?? '').toLowerCase().includes(reviewer)) return false;
    if (since && !(latest && latest.at >= since)) return false;
    return true;
  });

  const retrieval: RetrievalView = {
    ...capture.retrieval,
    filtered_locally: [...capture.retrieval.filtered_locally, ...applied],
    note:
      `${capture.retrieval.note} Neither \`reviewer\` nor \`since\` can be pushed down at ` +
      'either entry point, so Attest applies them over the page DataHub returned — here, over ' +
      'the recorded page. The counts above are DataHub’s own and a local filter does not ' +
      'move them.',
  };
  return served({ claims: kept, retrieval }, 250);
}

export async function getClaim(claimUrn: string): Promise<ClaimView> {
  const capture = findCapture({});
  const hit = capture?.claims.find((c) => c.claim_urn === claimUrn);
  if (!hit) notRecorded(`No artifact with URN ${claimUrn} is in this recording.`);
  return served(hit, 150);
}

// --- catalog discovery, which this page cannot do at all ---------------------

/** REFUSED, and consistently so: this build already refuses an edited agent output.
 *
 *  Catalog discovery is a live query to a live MCP server against a live catalog, and a
 *  static page has none of those. The two dishonest alternatives are both worse than a
 *  refusal: replaying a recorded result list would present a query nobody ran as if it had
 *  been run, and falling back to a hardcoded list of seeded URNs would show a judge a
 *  "catalog" that is a file in this bundle.
 *
 *  It also has nowhere to go. Picking a URN edits the agent output, and `submitAudit` refuses
 *  edited text because the recording carries one audit of one sentence. A picker that
 *  inserted a URN here would only lead to that second refusal. */
export async function searchCatalog(q: string, _limit = 10): Promise<never> {
  notRecorded(
    `Catalog discovery asks a live DataHub MCP Server what matches "${q}", against a live ` +
      'catalog. This page has neither. Nothing here is simulated, and a list of datasets ' +
      'shipped inside this bundle would not be a catalog — it would be a file. Run Attest ' +
      'locally to search your own catalog.',
  );
}
