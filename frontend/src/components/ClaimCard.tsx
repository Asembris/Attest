import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronDown, Database, Sparkles, FileText } from 'lucide-react';
import type { ClaimRecord } from '../api/types';
import VerdictBadge from './VerdictBadge';
import EvidencePanel from './EvidencePanel';
import CorrectionPanel from './CorrectionPanel';

export default function ClaimCard({ claim, index }: { claim: ClaimRecord; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const modelAuthored = claim.explanation_source === 'model';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 24 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.12, duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className="surface-card overflow-hidden"
    >
      {/* Card header — always visible */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-4 p-5 text-left hover:bg-ink-800/40 transition-colors"
      >
        <div className="flex items-center gap-3 pt-0.5 shrink-0">
          <span className="font-mono-nums text-xs text-ink-400 w-6">
            {String(index + 1).padStart(2, '0')}
          </span>
        </div>

        <div className="flex-1 min-w-0">
          <p className="text-sm lg:text-[0.95rem] text-ink-100 leading-relaxed">{claim.raw_text}</p>
          <div className="flex items-center gap-3 mt-3 flex-wrap">
            <VerdictBadge verdict={claim.verdict} size="sm" />
            {/* The real signal is model-authored vs. deterministic template (the guard-fire),
                NOT a made-up confidence score. */}
            <span className="flex items-center gap-1 text-xs text-ink-400">
              {modelAuthored ? (
                <>
                  <Sparkles size={10} /> Model-authored
                </>
              ) : (
                <>
                  <FileText size={10} /> Safe template
                </>
              )}
            </span>
            <span className="font-mono-nums text-xs text-ink-500">{claim.claim_type}</span>
          </div>
        </div>

        <motion.div animate={{ rotate: expanded ? 180 : 0 }} className="pt-1 shrink-0 text-ink-400">
          <ChevronDown size={18} />
        </motion.div>
      </button>

      {/* Expandable section */}
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-5">
              <div className="divider mb-4" />

              {/* Explanation */}
              <div className="mb-4">
                <div className="flex items-center gap-1.5 text-label-sm mb-2">
                  {modelAuthored ? <Sparkles size={11} /> : <FileText size={11} />}
                  Explanation
                </div>
                <p className="text-sm text-ink-200 leading-relaxed">{claim.explanation}</p>
              </div>

              {/* Cited evidence */}
              <div className="flex items-center gap-1.5 text-label-sm mb-1">
                <Database size={11} /> Evidence
              </div>
              <EvidencePanel evidence={claim.evidence} claimType={claim.claim_type} />

              {/* Correction loop outcome (read-only in Phase 2) */}
              <CorrectionPanel claim={claim} />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
