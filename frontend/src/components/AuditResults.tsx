import { motion } from 'framer-motion';
import { ArrowLeft, BarChart3, ShieldCheck, AlertTriangle } from 'lucide-react';
import type { AuditRecord, HealthResponse } from '../api/types';
import { verdictCounts } from '../api/types';
import ReceiptsStrip from './ReceiptsStrip';
import ClaimCard from './ClaimCard';

export default function AuditResults({
  record,
  health,
  onBack,
  onShowBenchmark,
}: {
  record: AuditRecord;
  health: HealthResponse | null;
  onBack: () => void;
  onShowBenchmark: () => void;
}) {
  const counts = verdictCounts(record);

  return (
    <div className="min-h-screen bg-ink-950">
      {/* Header */}
      <header className="sticky top-0 z-20 border-b border-ink-700/40 bg-ink-950/80 backdrop-blur-md">
        <div className="max-w-4xl mx-auto px-6 lg:px-8 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={onBack} className="btn-ghost text-sm px-2">
              <ArrowLeft size={16} />
            </button>
            <div className="flex items-center gap-2">
              <ShieldCheck size={18} className="text-supported" strokeWidth={2.5} />
              <span className="font-serif text-base font-medium tracking-tight">Attest</span>
            </div>
            <span className="text-ink-500 mx-1">/</span>
            <span className="text-sm text-ink-300">Audit Results</span>
          </div>
          <button onClick={onShowBenchmark} className="btn-ghost text-sm">
            <BarChart3 size={15} />
            Benchmark
          </button>
        </div>
      </header>

      <div className="max-w-4xl mx-auto px-6 lg:px-8 py-8 space-y-6">
        {/* Headline */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <h1 className="font-serif text-headline font-light text-ink-50">Audit Complete</h1>
          <p className="mt-2 text-ink-300 text-sm">
            {record.claims.length} claim{record.claims.length === 1 ? '' : 's'} verified against the
            DataHub catalog. Expand any claim to review the cited evidence.
          </p>
        </motion.div>

        {/* Receipts strip */}
        <ReceiptsStrip
          receipts={record.receipts}
          status={record.status}
          model={health?.model ?? '—'}
          datahub={health ? health.datahub : '—'}
          claimCount={record.claims.length}
        />

        {/* Summary breakdown */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3 }}
          className="flex items-center gap-4 text-sm"
        >
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-supported" />
            <span className="font-mono-nums text-ink-200">{counts.Supported} Supported</span>
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-contradicted" />
            <span className="font-mono-nums text-ink-200">{counts.Contradicted} Contradicted</span>
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-insufficient" />
            <span className="font-mono-nums text-ink-200">
              {counts['Insufficient-Coverage']} Insufficient
            </span>
          </span>
        </motion.div>

        {/* Claim cards */}
        <div className="space-y-3 pt-2">
          {record.claims.map((claim, i) => (
            <ClaimCard key={claim.index} claim={claim} index={i} />
          ))}
        </div>

        {/* Claims that could not be checked at all — NOT verdicts, surfaced not swallowed. */}
        {record.errors.length > 0 && (
          <div className="surface-card p-5 border-contradicted/20">
            <div className="flex items-center gap-2 mb-2 text-sm text-contradicted">
              <AlertTriangle size={14} /> {record.errors.length} claim
              {record.errors.length === 1 ? '' : 's'} could not be checked
            </div>
            <p className="text-xs text-ink-400 mb-3">
              The question was malformed (e.g. the entity does not exist). This is not a verdict —
              the catalog neither agrees nor is silent.
            </p>
            <div className="space-y-2">
              {record.errors.map((e) => (
                <div key={e.index} className="text-xs">
                  <span className="font-mono-nums text-ink-300 break-words">{e.target_urn}</span>
                  <span className="text-ink-400"> — {e.error}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="pt-8 pb-12 text-center">
          <button onClick={onBack} className="btn-ghost text-sm">
            <ArrowLeft size={15} />
            Run Another Audit
          </button>
        </div>
      </div>
    </div>
  );
}
