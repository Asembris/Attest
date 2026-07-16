import { useState } from 'react';
import { motion } from 'framer-motion';
import { ArrowLeft, BarChart3, ShieldCheck, AlertTriangle, GitBranch } from 'lucide-react';
import type { AuditRecord, DecisionRequest, HealthResponse, WriteBackView } from '../api/types';
import { verdictCounts, awaitingPublication } from '../api/types';
import ReceiptsStrip from './ReceiptsStrip';
import ClaimCard from './ClaimCard';
import AuditInternals from './AuditInternals';

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
  // WHAT THE RUN IS ACTUALLY PARKED ON: every claim whose VERDICT is still unpublished —
  // not just the ones with a correction proposal. This counted `proposals` until Session 16
  // and had been wrong since Option A landed: the checkpoint parks while any claim's
  // publication is pending, and only a correction ever produces a proposal. So a four-claim
  // run with one proposal submitted one decision, re-parked on the other three, and could
  // never reach `complete` — while this bar reported it settled.
  const pending = awaitingPublication(record);
  const reviewMode = record.status === 'awaiting-review' && pending.length > 0;

  // Two decisions per claim, never one flag. `undefined` is "no opinion", which is not "no":
  // an unnamed claim stays pending and the run stays parked, decidable on a later call.
  const [decisions, setDecisions] = useState<
    Record<number, { publish?: boolean; accept_correction?: boolean }>
  >({});

  // The gate is PUBLICATION, and only publication. A correction is optional to rule on —
  // there may not be one, and "no opinion on the fix" is a legitimate position — but a claim
  // whose verdict nobody decided is a claim the run is still waiting on. Measured over
  // `pending` rather than over the whole decisions map, so the count cannot outrun what is
  // actually decidable.
  const decided = pending.filter((c) => decisions[c.index]?.publish !== undefined).length;
  const allDecided = decided === pending.length;

  // Bug 2: the DataHub indicator reflects THIS run's actual catalog reachability, not the
  // mount-time health probe (which is fetched once and sticks — a transient warm-up failure
  // would report a false outage for the whole session). A ClaimRecord exists only for an
  // entity that resolved: the graph drops an unresolvable URN into `errors`, never into
  // `claims`. So any verdict at all is proof the catalog was reached during the run. A
  // genuine per-URN failure is not hidden — it still surfaces below as a claim that could
  // not be checked.
  const catalogReached = record.claims.length > 0;
  const datahubStatus = catalogReached ? 'reachable' : health?.datahub ?? '—';

  function submit() {
    // One decision per claim still awaiting publication. `accept_correction` is only sent
    // when a view was actually taken on it — omitting it means "no opinion", and sending
    // `false` would RECORD a rejection nobody made, in an append-only log.
    const payload: DecisionRequest[] = pending.map((c) => {
      const d = decisions[c.index] ?? {};
      return {
        claim_index: c.index,
        publish: d.publish,
        ...(d.accept_correction === undefined
          ? {}
          : { accept_correction: d.accept_correction }),
        reviewer: 'attest-ui',
      };
    });
    onApprove(payload);
  }

  function decide(index: number, patch: { publish?: boolean; accept_correction?: boolean }) {
    setDecisions((d) => ({ ...d, [index]: { ...d[index], ...patch } }));
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
          datahub={datahubStatus}
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
                {pending.length} verdict{pending.length === 1 ? '' : 's'} awaiting your decision
              </span>
            </div>
            <p className="text-xs text-ink-300 mb-4">
              Every claim needs a publish decision, whatever its verdict — a Supported finding
              is how the catalog learns that someone looked, and a verdict Attest never
              records is indistinguishable from a claim it never checked. Where the agent
              proposed a fix you can rule on that separately. Nothing is written until you
              submit; the run is parked at a durable checkpoint until then.
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
                {decided}/{pending.length} decided
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
              // Reviewable per CLAIM now, not per proposal: every claim whose verdict is
              // still pending gets a control, which is what Option A means.
              reviewable={reviewMode && claim.publication.status === 'pending'}
              publish={decisions[claim.index]?.publish}
              onPublish={(publish) => decide(claim.index, { publish })}
              acceptCorrection={decisions[claim.index]?.accept_correction}
              onAcceptCorrection={(accept) => decide(claim.index, { accept_correction: accept })}
              // KNOWN GAP, and named rather than half-fixed: this matches by TARGET URN, so
              // two claims about one dataset both show the last write's result. The backend
              // has this right — it keys its decision log by claim index precisely because
              // keying by URN attributed a write to the wrong decision — but the wire type
              // carries `claim_urn` and no claim index, and the UI cannot derive the URN
              // (it is a sha256 over the claim's canonical JSON; re-implementing that
              // identity rule in TypeScript would be a worse bug than this one). The fix is
              // a claim index on WriteBackView, which is the write path's shape to change.
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

        {/* The auditor's evidence trail — reachable, but not front-and-center. */}
        <AuditInternals record={record} />

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
