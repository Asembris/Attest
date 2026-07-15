import { Database } from 'lucide-react';
import type { ClaimType, EvidenceView } from '../api/types';

// Phase 2: a plain, truthful rendering of the evidence the checker actually cited — the
// `EvidenceView(field, value, note)` tuples the record carries, and nothing more. There is
// deliberately no full dataset panel here (no row count, no zone, no owner-for-a-freshness-
// claim): a verdict traces to the specific fact that decided it, and showing fields that did
// not decide it would be evidence theater. Phase 3 gives this the claim-type-specific layout;
// this version is already cited-only.

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (Array.isArray(value)) return value.join(', ');
  return String(value);
}

export default function EvidencePanel({
  evidence,
  claimType,
}: {
  evidence: EvidenceView[];
  claimType: ClaimType;
}) {
  return (
    <div className="mt-4 surface-raised p-5 space-y-4">
      <div className="flex items-center justify-between pb-3 border-b border-ink-700/40">
        <div className="flex items-center gap-2">
          <Database size={14} className="text-ink-300" />
          <span className="text-sm text-ink-100">Cited evidence</span>
        </div>
        <span className="text-label-sm">{claimType}</span>
      </div>

      <div className="space-y-3">
        {evidence.map((e, i) => {
          const silent = e.value === null;
          return (
            <div key={i} className="space-y-1">
              <div className="flex items-center gap-2">
                <span className="font-mono-nums text-xs text-ink-400 truncate">{e.field}</span>
                {silent && (
                  <span className="text-[0.625rem] uppercase tracking-wide text-insufficient">
                    catalog silent
                  </span>
                )}
              </div>
              <div className="font-mono-nums text-sm text-ink-100 break-words">
                {renderValue(e.value)}
              </div>
              {e.note && <div className="text-xs text-ink-300">{e.note}</div>}
            </div>
          );
        })}
        {evidence.length === 0 && (
          <div className="text-xs text-ink-400">No evidence fields were cited for this claim.</div>
        )}
      </div>
    </div>
  );
}
