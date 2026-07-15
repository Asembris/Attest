import { useState } from 'react';
import { motion } from 'framer-motion';
import { ArrowLeft, BarChart3, ShieldCheck, AlertTriangle, GitBranch } from 'lucide-react';
import type { AuditRecord, DecisionRequest, HealthResponse, WriteBackView } from '../api/types';
import { verdictCounts, proposals as findProposals } from '../api/types';
import ReceiptsStrip from './ReceiptsStrip';
import ClaimCard from './ClaimCard';

export default function AuditResults({
  record,
  health,
  writebacks,
  approving,
  approveError,
  onApprove,
  onBack,
  onShowBenchmark,
}: {
  record: AuditRecord;
  health: HealthResponse | null;
  writebacks: WriteBackView[] | null;
  approving: boolean;
  approveError: string | null;
  onApprove: (decisions: DecisionRequest[]) => void;
  onBack: () => void;
  onShowBenchmark: () => void;
}) {
  const counts = verdictCounts(record);
  const proposals = findProposals(record);
  const reviewMode = record.status === 'awaiting-review' && proposals.length > 0;

  const [decisions, setDecisions] = useState<Record<number, boolean>>({});
  const allDecided = proposals.every((p) => decisions[p.index] !== undefined);

  function submit() {
    const payload: DecisionRequest[] = proposals.map((p) => ({
      claim_index: p.index,
      accept: decisions[p.index],
      reviewer: 'attest-ui',
    }));
    onApprove(payload);
  }

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
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
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

        {/* Review bar — the human checkpoint, only while the run is parked with proposals. */}
        {reviewMode && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="surface-card p-5 border-insufficient/25 bg-insufficient/5"
          >
            <div className="flex items-center gap-2 text-sm text-insufficient mb-1">
              <GitBranch size={15} />
              <span className="font-medium">
                {proposals.length} correction{proposals.length === 1 ? '' : 's'} awaiting your decision
              </span>
            </div>
            <p className="text-xs text-ink-300 mb-4">
              Decide each proposal below. Approving writes the verdict back to DataHub; nothing is
              written until you submit. The run is parked at a durable checkpoint until then.
            </p>
            <div className="flex items-center gap-3">
              <button
                onClick={submit}
                disabled={!allDecided || approving}
                className="btn-primary text-sm disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {approving ? 'Submitting…' : 'Submit decisions'}
              </button>
              <span className="text-xs text-ink-400">
                {Object.keys(decisions).length}/{proposals.length} decided
              </span>
            </div>
            {approveError && (
              <div className="mt-3 flex items-center gap-2 text-xs text-contradicted">
                <AlertTriangle size={13} /> {approveError}
              </div>
            )}
          </motion.div>
        )}

        {/* Claim cards */}
        <div className="space-y-3 pt-2">
          {record.claims.map((claim, i) => (
            <ClaimCard
              key={claim.index}
              claim={claim}
              index={i}
              reviewable={
                reviewMode &&
                claim.correction.outcome === 'corrected' &&
                claim.correction.review === 'pending'
              }
              decision={decisions[claim.index]}
              onDecide={(accept) => setDecisions((d) => ({ ...d, [claim.index]: accept }))}
              writeback={writebacks?.find((w) => w.target_urn === claim.target_urn) ?? null}
            />
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
