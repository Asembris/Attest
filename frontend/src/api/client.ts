// The API client. Four calls, matching app.py's four endpoints. Paths are relative, so the
// same code works static-mounted on :8003 and behind the Vite dev proxy (vite.config.ts).
//
// No client-side flow is faked here. `submitAudit` runs the real durable audit; `approve`
// hits the real human-checkpoint endpoint that resumes the parked graph and writes verdicts
// back to DataHub. The UI shows what the backend actually did, including a write-back that
// failed.

import type {
  ApprovalResponse,
  AuditRecord,
  DecisionRequest,
  HealthResponse,
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

/** The human checkpoint. Settle proposals; accepted ones are written back to the catalog.
 *  Only what you name is settled — an empty list leaves every proposal PENDING. */
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
