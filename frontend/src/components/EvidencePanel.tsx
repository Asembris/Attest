import { Clock, User, Columns3, ShieldAlert, ShieldCheck, Tag } from 'lucide-react';
import type { ClaimType, EvidenceView } from '../api/types';

// Phase 3: the evidence panel renders ONLY what the checker cited to decide this claim —
// the `EvidenceView(field, value, note)` tuples, framed by claim type. There is deliberately
// no full dataset receipt: a freshness verdict is decided by a timestamp, so a freshness
// card foregrounds the timestamp and nothing else; a classification card foregrounds the PII
// signals and the columns it read. Showing fields that did not decide the verdict would be
// evidence theater and would betray the one thing the product guarantees — that every verdict
// traces to the specific fact behind it.

const HEAD: Record<ClaimType, { icon: typeof Clock; label: string }> = {
  freshness: { icon: Clock, label: 'Freshness evidence' },
  ownership: { icon: User, label: 'Ownership evidence' },
  classification: { icon: ShieldAlert, label: 'Classification evidence' },
  schema: { icon: Columns3, label: 'Schema evidence' },
};

type Parsed = { kind: string; label: string };

function parseUrn(raw: string): Parsed | null {
  if (!raw.startsWith('urn:li:')) return null;
  const parts = raw.split(':');
  // urn:li:<kind>:<id...>  — id may itself contain ':' (or dots for glossary terms)
  const kind = parts[2] ?? 'urn';
  const label = parts.slice(3).join(':') || raw;
  return { kind, label };
}

/** PII / Sensitive read as a danger signal; NonPII / Verified read as reassurance. */
function toneFor(label: string): 'danger' | 'safe' | 'neutral' {
  if (/pii(?!.*non)|sensitive/i.test(label) && !/nonpii/i.test(label)) return 'danger';
  if (/nonpii|verified/i.test(label)) return 'safe';
  return 'neutral';
}

function Chip({ text, tone }: { text: string; tone: 'danger' | 'safe' | 'neutral' }) {
  const cls =
    tone === 'danger'
      ? 'text-contradicted bg-contradicted/10 border-contradicted/30'
      : tone === 'safe'
        ? 'text-supported bg-supported/10 border-supported/30'
        : 'text-ink-200 bg-ink-700/40 border-ink-600/40';
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-mono-nums ${cls}`}>
      {tone === 'danger' && <ShieldAlert size={10} />}
      {tone === 'safe' && <ShieldCheck size={10} />}
      {text}
    </span>
  );
}

function isIsoTimestamp(v: string): boolean {
  return /^\d{4}-\d{2}-\d{2}T/.test(v);
}

/** Render one evidence value in the shape it deserves: URNs as chips, timestamps as a date,
 *  column lists as mono chips, a null as an explicit "catalog silent". */
function EvidenceValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return (
      <span className="text-[0.625rem] uppercase tracking-wide text-insufficient border border-insufficient/30 rounded px-1.5 py-0.5">
        catalog silent
      </span>
    );
  }

  const items = Array.isArray(value) ? value : [value];
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item, i) => {
        const s = String(item);
        const urn = parseUrn(s);
        if (urn) {
          const icon = urn.kind === 'corpuser' ? '' : urn.kind === 'glossaryTerm' ? 'term: ' : '';
          return <Chip key={i} text={`${icon}${urn.label}`} tone={toneFor(urn.label)} />;
        }
        if (isIsoTimestamp(s)) {
          return (
            <span key={i} className="font-mono-nums text-sm text-ink-100">
              {new Date(s).toISOString().replace('T', ' ').slice(0, 19)} UTC
            </span>
          );
        }
        return <Chip key={i} text={s} tone="neutral" />;
      })}
    </div>
  );
}

export default function EvidencePanel({
  evidence,
  claimType,
}: {
  evidence: EvidenceView[];
  claimType: ClaimType;
}) {
  const head = HEAD[claimType];
  const Icon = head.icon;

  return (
    <div className="mt-4 surface-raised p-5 space-y-4">
      <div className="flex items-center justify-between pb-3 border-b border-ink-700/40">
        <div className="flex items-center gap-2">
          <Icon size={14} className="text-ink-300" />
          <span className="text-sm text-ink-100">{head.label}</span>
        </div>
        <span className="text-label-sm">cited-only</span>
      </div>

      <div className="space-y-4">
        {evidence.map((e, i) => (
          <div key={i} className="space-y-1.5">
            <EvidenceValue value={e.value} />
            {e.note && <div className="text-sm text-ink-200 leading-relaxed">{e.note}</div>}
            <div className="flex items-center gap-1 text-[0.6875rem] text-ink-500 font-mono-nums">
              <Tag size={9} />
              {e.field}
            </div>
          </div>
        ))}
        {evidence.length === 0 && (
          <div className="text-xs text-ink-400">No evidence fields were cited for this claim.</div>
        )}
      </div>
    </div>
  );
}
