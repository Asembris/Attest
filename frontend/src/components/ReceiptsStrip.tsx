import { motion } from 'framer-motion';
import { ShieldCheck, Clock3, Clock, Coins, Cpu, FileCheck, Zap } from 'lucide-react';
import type { Receipts, RunStatus } from '../api/types';

function Metric({
  icon: Icon,
  label,
  value,
  delay,
}: {
  icon: typeof Clock;
  label: string;
  value: string;
  delay: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay, duration: 0.4 }}
      className="flex items-center gap-3"
    >
      <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-ink-700/50 text-ink-300">
        <Icon size={15} strokeWidth={2} />
      </div>
      <div className="flex flex-col">
        <span className="text-label-sm">{label}</span>
        <span className="font-mono-nums text-sm text-ink-100 font-medium">{value}</span>
      </div>
    </motion.div>
  );
}

export default function ReceiptsStrip({
  receipts,
  status,
  model,
  datahub,
  claimCount,
}: {
  receipts: Receipts;
  status: RunStatus;
  model: string;
  datahub: string;
  claimCount: number;
}) {
  const seconds = (receipts.latency_ms / 1000).toFixed(2);
  const tokens = (receipts.input_tokens + receipts.output_tokens).toLocaleString();
  // null, never 0, when a model in the run had no price (cost.py). Rendering it as $0 would
  // be the fabricated measurement this whole product exists to catch — so it says "unknown".
  const cost = receipts.usd === null ? 'unknown' : `$${receipts.usd.toFixed(4)}`;

  const settled = status === 'complete';
  const statusLabel = settled ? 'Complete' : 'Awaiting review';
  const statusColor = settled ? 'text-supported' : 'text-insufficient';
  const statusBg = settled ? 'bg-supported/15 text-supported' : 'bg-insufficient/15 text-insufficient';

  return (
    <motion.div
      initial={{ opacity: 0, y: -12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.5 }}
      className="surface-card px-6 py-4"
    >
      <div className="flex flex-wrap items-center gap-x-8 gap-y-4">
        <div className="flex items-center gap-3 pr-6 border-r border-ink-700/60">
          <div className="relative">
            <div className={`flex items-center justify-center w-8 h-8 rounded-lg ${statusBg}`}>
              {settled ? <ShieldCheck size={16} strokeWidth={2.5} /> : <Clock3 size={16} strokeWidth={2.5} />}
            </div>
            {settled && (
              <motion.div
                animate={{ scale: [1, 1.4, 1], opacity: [0.6, 0, 0.6] }}
                transition={{ duration: 2.5, repeat: Infinity }}
                className="absolute inset-0 rounded-lg bg-supported/20"
              />
            )}
          </div>
          <div className="flex flex-col">
            <span className="text-label-sm">Status</span>
            <span className={`text-sm font-medium ${statusColor}`}>{statusLabel}</span>
          </div>
        </div>

        <Metric icon={FileCheck} label="Claims Audited" value={String(claimCount)} delay={0.05} />
        <Metric icon={Clock} label="Time Taken" value={`${seconds}s`} delay={0.1} />
        <Metric icon={Cpu} label="Tokens Used" value={tokens} delay={0.15} />
        <Metric icon={Coins} label="Cost" value={cost} delay={0.2} />
        <Metric icon={Zap} label="Model" value={model} delay={0.25} />

        <div className="ml-auto flex items-center gap-2 text-xs">
          <span
            className={`w-1.5 h-1.5 rounded-full ${
              datahub === 'reachable' || datahub === 'up' ? 'bg-supported' : 'bg-insufficient'
            }`}
          />
          <span className="font-mono-nums text-ink-400">DataHub {datahub}</span>
        </div>
      </div>
    </motion.div>
  );
}
