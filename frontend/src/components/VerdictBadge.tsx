import { motion } from 'framer-motion';
import { Check, X, HelpCircle } from 'lucide-react';
import type { Verdict } from '../data/mockData';

const config: Record<
  Verdict,
  { label: string; color: string; bg: string; border: string; icon: typeof Check }
> = {
  supported: {
    label: 'Supported',
    color: 'text-supported',
    bg: 'bg-supported/10',
    border: 'border-supported/30',
    icon: Check,
  },
  contradicted: {
    label: 'Contradicted',
    color: 'text-contradicted',
    bg: 'bg-contradicted/10',
    border: 'border-contradicted/30',
    icon: X,
  },
  insufficient: {
    label: 'Insufficient Coverage',
    color: 'text-insufficient',
    bg: 'bg-insufficient/10',
    border: 'border-insufficient/30',
    icon: HelpCircle,
  },
};

export default function VerdictBadge({
  verdict,
  size = 'md',
}: {
  verdict: Verdict;
  size?: 'sm' | 'md';
}) {
  const c = config[verdict];
  const Icon = c.icon;
  const sz = size === 'sm' ? 'text-xs px-2.5 py-1' : 'text-sm px-3 py-1.5';
  const iconSz = size === 'sm' ? 12 : 14;

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border font-medium ${sz} ${c.color} ${c.bg} ${c.border}`}
    >
      <Icon size={iconSz} strokeWidth={2.5} />
      {c.label}
    </span>
  );
}

export function VerdictDot({ verdict }: { verdict: Verdict }) {
  const colors: Record<Verdict, string> = {
    supported: 'bg-supported',
    contradicted: 'bg-contradicted',
    insufficient: 'bg-insufficient',
  };
  return (
    <motion.span
      layout
      className={`inline-block w-2 h-2 rounded-full ${colors[verdict]}`}
    />
  );
}
