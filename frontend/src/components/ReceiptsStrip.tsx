import { motion } from 'framer-motion';
import { ShieldCheck, Clock, Coins, Cpu, FileCheck, Zap } from 'lucide-react';
import type { AuditReceipt } from '../data/mockData';

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

export default function ReceiptsStrip({ receipt }: { receipt: AuditReceipt }) {
  const seconds = (receipt.timeTakenMs / 1000).toFixed(2);

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
            <div className="flex items-center justify-center w-8 h-8 rounded-lg bg-supported/15 text-supported">
              <ShieldCheck size={16} strokeWidth={2.5} />
            </div>
            <motion.div
              animate={{ scale: [1, 1.4, 1], opacity: [0.6, 0, 0.6] }}
              transition={{ duration: 2.5, repeat: Infinity }}
              className="absolute inset-0 rounded-lg bg-supported/20"
            />
          </div>
          <div className="flex flex-col">
            <span className="text-label-sm">Status</span>
            <span className="text-sm text-supported font-medium">Verified</span>
          </div>
        </div>

        <Metric icon={FileCheck} label="Claims Audited" value={String(receipt.totalClaims)} delay={0.05} />
        <Metric icon={Clock} label="Time Taken" value={`${seconds}s`} delay={0.1} />
        <Metric icon={Cpu} label="Tokens Used" value={receipt.tokensUsed.toLocaleString()} delay={0.15} />
        <Metric icon={Coins} label="Cost" value={receipt.cost} delay={0.2} />
        <Metric icon={Zap} label="Model" value={receipt.modelVersion} delay={0.25} />

        <div className="ml-auto flex items-center gap-2 text-xs text-ink-400">
          <span className="font-mono-nums">{receipt.catalogVersion}</span>
        </div>
      </div>
    </motion.div>
  );
}
