// The API client, one call per endpoint in app.py. Paths are relative, so the same code
// works static-mounted on :8003 and behind the Vite dev proxy (vite.config.ts) — which is
// why there is no base URL and no env var. Add a new path PREFIX and it must also be added
// to the proxy, or dev 404s while the built demo works.
//
// No client-side flow is faked here. `submitAudit` runs the real durable audit; `approve`
// hits the real human-checkpoint endpoint that resumes the parked graph and writes verdicts
// back to DataHub; `listClaims` reads them back OUT OF THE CATALOG, not out of Attest's
// store. The UI shows what the backend actually did, including a write-back that failed.
//
// The two halves of the thesis are `approve` and `listClaims`: one publishes a verdict to
// DataHub, the other is a reader inheriting it.

import type {
  ApprovalResponse,
  AuditRecord,
  ClaimFilters,
  ClaimsResponse,
  ClaimView,
  DecisionRequest,
  HealthResponse,
  WriteBackResponse,
} from './types';

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = 'ApiError';
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    // FastAPI errors are {detail: "..."}; validation errors nest detail as an array.
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* non-JSON error body; keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function health(): Promise<HealthResponse> {
  return parse<HealthResponse>(await fetch('/health'));
}

/** Submit an agent's output for audit. Returns the complete record; if it produced
 *  corrections, `status` is `awaiting-review` and they are PENDING — nothing has reached
 *  DataHub. */
export async function submitAudit(
  agentOutput: string,
  sourceAgent = '',
): Promise<AuditRecord> {
  const res = await fetch('/audit', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent_output: agentOutput, source_agent: sourceAgent }),
  });
  return parse<AuditRecord>(res);
}

export async function getAudit(runId: string): Promise<AuditRecord> {
  return parse<AuditRecord>(await fetch(`/audit/${runId}`));
}

/** The human checkpoint. Publish verdicts, and rule on any proposed corrections.
 *  Only what you name is settled — a claim you leave out stays PENDING and the run stays
 *  parked, decidable on a later call. There is no "approve all", by design. */
export async function approve(
  runId: string,
  decisions: DecisionRequest[],
): Promise<ApprovalResponse> {
  const res = await fetch(`/audit/${runId}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decisions }),
  });
  return parse<ApprovalResponse>(res);
}

/** Re-run the catalog write for claims a human ALREADY published. The repair path.
 *
 *  **It approves nothing and cannot.** It re-executes the side effect of decisions already
 *  in the append-only log — a claim nobody published is not reachable from here. It exists
 *  because re-approving cannot repair a write: a settled claim is skipped, and a fully
 *  decided run is complete and not resumable.
 *
 *  Safe to call repeatedly: every write is idempotent. The claim's URN is derived from its
 *  content so a re-run lands on the same artifact, and the verdict is keyed by the run's own
 *  timestamp so re-reporting collapses onto the same row rather than appending a duplicate.
 *
 *  For a claim with a RECORDED FAILED WRITE — which is not the same as `state === incomplete`
 *  and the difference is load-bearing. An artifact is content-addressed, so a claim audited
 *  before and re-audited today, whose NEW write failed, still shows the older verdict and
 *  reads `complete`; it is repairable and gating on the state would strand it. What must
 *  never reach this are `pending-lag` (the write landed; the index is ~2s behind; there is
 *  nothing to repair) and `unknown` (no recorded write to re-run) — and neither can, because
 *  both have a null `failed_step`. See ClaimArtifactCard's `repairable`. */
export async function retryWriteback(runId: string): Promise<WriteBackResponse> {
  const res = await fetch(`/audit/${runId}/writeback`, { method: 'POST' });
  return parse<WriteBackResponse>(res);
}

// --- reading the catalog back ------------------------------------------------

/** Claim artifacts, READ FROM DATAHUB. What the next agent inherits.
 *
 *  Read the `retrieval` field of the response: it says which predicates DataHub's index
 *  applied and which Attest applied over the result. Naming `target_urn` scopes the query
 *  server-side and is the thesis query — every claim on a dataset, in one round trip. */
export async function listClaims(filters: ClaimFilters = {}): Promise<ClaimsResponse> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, String(value));
  }
  const query = params.toString();
  return parse<ClaimsResponse>(await fetch(`/claims${query ? `?${query}` : ''}`));
}

/** One claim artifact and its whole verdict history.
 *
 *  The URN is encoded whole: an assertion URN contains colons, and the dataset URN nested
 *  inside a claim's identity contains commas and parentheses. The route takes it as a path
 *  segment (`{claim_urn:path}`) rather than making callers double-encode. */
export async function getClaim(claimUrn: string): Promise<ClaimView> {
  return parse<ClaimView>(await fetch(`/claims/${claimUrn}`));
}
