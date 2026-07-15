import { motion } from 'framer-motion';
import { ArrowLeft, BarChart3, ShieldCheck } from 'lucide-react';
import { claims, auditReceipt } from '../data/mockData';
import ReceiptsStrip from './ReceiptsStrip';
import ClaimCard from './ClaimCard';

export default function AuditResults({
  onBack,
  onShowBenchmark,
}: {
  onBack: () => void;
  onShowBenchmark: () => void;
}) {
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
          <h1 className="font-serif text-headline font-light text-ink-50">
            Audit Complete
          </h1>
          <p className="mt-2 text-ink-300 text-sm">
            {auditReceipt.totalClaims} claims verified against your data catalog. Expand any claim to review evidence.
          </p>
        </motion.div>

        {/* Receipts strip */}
        <ReceiptsStrip receipt={auditReceipt} />

        {/* Summary breakdown */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.3 }}
          className="flex items-center gap-4 text-sm"
        >
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-supported" />
            <span className="font-mono-nums text-ink-200">{auditReceipt.supported} Supported</span>
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-contradicted" />
            <span className="font-mono-nums text-ink-200">{auditReceipt.contradicted} Contradicted</span>
          </span>
          <span className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-insufficient" />
            <span className="font-mono-nums text-ink-200">{auditReceipt.insufficient} Insufficient</span>
          </span>
        </motion.div>

        {/* Claim cards */}
        <div className="space-y-3 pt-2">
          {claims.map((claim, i) => (
            <ClaimCard key={claim.id} claim={claim} index={i} />
          ))}
        </div>

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
