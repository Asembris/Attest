import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  ChevronDown,
  Database,
  User,
  Clock,
  ShieldAlert,
  ShieldCheck,
  Columns3,
  Layers,
  Sparkles,
  FileText,
} from 'lucide-react';
import type { Claim, Evidence } from '../data/mockData';
import VerdictBadge from './VerdictBadge';

function formatNumber(n: number): string {
  if (n === 0) return '—';
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function EvidenceReceipt({ evidence }: { evidence: Evidence }) {
  return (
    <div className="mt-4 surface-raised p-5 space-y-4">
      {/* Receipt header */}
      <div className="flex items-center justify-between pb-3 border-b border-ink-700/40">
        <div className="flex items-center gap-2">
          <Database size={14} className="text-ink-300" />
          <span className="font-mono-nums text-sm text-ink-100">{evidence.table}</span>
        </div>
        <span className="text-label-sm">
          {evidence.zone.charAt(0).toUpperCase() + evidence.zone.slice(1)} Zone
        </span>
      </div>

      {/* Receipt metadata grid */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div>
          <div className="flex items-center gap-1.5 text-label-sm mb-1">
            <User size={11} /> Owner
          </div>
          <div className="text-sm text-ink-100">{evidence.owner}</div>
          <div className="text-xs text-ink-400">{evidence.steward}</div>
        </div>
        <div>
          <div className="flex items-center gap-1.5 text-label-sm mb-1">
            <Clock size={11} /> Freshness
          </div>
          <div className="text-sm text-ink-100 font-mono-nums">{evidence.freshness}</div>
          <div className="text-xs text-ink-400 font-mono-nums">
            {new Date(evidence.lastUpdated).toISOString().split('T')[0]}
          </div>
        </div>
        <div>
          <div className="flex items-center gap-1.5 text-label-sm mb-1">
            {evidence.piiTag ? (
              <ShieldAlert size={11} className="text-contradicted" />
            ) : (
              <ShieldCheck size={11} className="text-supported" />
            )}
            PII Tag
          </div>
          <div className={`text-sm font-medium ${evidence.piiTag ? 'text-contradicted' : 'text-supported'}`}>
            {evidence.piiTag ? 'Contains PII' : 'No PII Detected'}
          </div>
          <div className="text-xs text-ink-400">{evidence.sourceSystem}</div>
        </div>
        <div>
          <div className="flex items-center gap-1.5 text-label-sm mb-1">
            <Layers size={11} /> Row Count
          </div>
          <div className="text-sm text-ink-100 font-mono-nums">{formatNumber(evidence.rowCount)}</div>
        </div>
      </div>

      {/* Columns */}
      <div>
        <div className="flex items-center gap-1.5 text-label-sm mb-2">
          <Columns3 size={11} /> Columns
        </div>
        <div className="space-y-1.5">
          {evidence.columns.map((col) => (
            <div
              key={col.name}
              className="flex items-center gap-3 text-xs py-1.5 px-2 rounded-md hover:bg-ink-700/30 transition-colors"
            >
              <span className="font-mono-nums text-ink-100 w-32 lg:w-44 truncate">{col.name}</span>
              <span className="font-mono-nums text-ink-400 w-28 lg:w-32 shrink-0">{col.type}</span>
              {col.pii ? (
                <span className="inline-flex items-center gap-1 text-contradicted shrink-0">
                  <ShieldAlert size={10} /> PII
                </span>
              ) : (
                <span className="w-8 shrink-0" />
              )}
              <span className="text-ink-300 truncate flex-1">{col.description}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function CorrectionPanel({ claim }: { claim: Claim }) {
  const [decision, setDecision] = useState<'pending' | 'approved' | 'rejected'>('pending');

  if (!claim.correctionAttempt) return null;
  const ca = claim.correctionAttempt;

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      exit={{ opacity: 0, height: 0 }}
      className="overflow-hidden"
    >
      <div className="mt-4 border border-contradicted/20 rounded-xl bg-contradicted/5 p-5 space-y-4">
        <div className="flex items-center gap-2">
          <div className="flex items-center justify-center w-6 h-6 rounded-full bg-contradicted/15 text-contradicted">
            <ShieldAlert size={13} />
          </div>
          <span className="text-sm font-medium text-contradicted">
            Deceptive Correction Detected — {ca.deceptionType}
          </span>
        </div>

        <div className="space-y-2 pl-8">
          <div className="text-xs text-ink-300">
            <span className="text-label-sm mr-2">Original</span>
            <span className="text-ink-200 line-through decoration-contradicted/50">{ca.originalText}</span>
          </div>
          <div className="text-xs text-ink-300">
            <span className="text-label-sm mr-2">Corrected To</span>
            <span className="text-ink-100">{ca.correctedText}</span>
          </div>
        </div>

        <p className="text-sm text-ink-200 leading-relaxed pl-8 border-l-2 border-contradicted/30 ml-2">
          {ca.explanation}
        </p>

        {decision === 'pending' && (
          <div className="pl-8 pt-1">
            <div className="text-xs text-ink-300 mb-3 italic">
              A human decides before anything is written back.
            </div>
            <div className="flex items-center gap-3">
              <button
                onClick={() => setDecision('approved')}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-supported/15 text-supported border border-supported/30 hover:bg-supported/25 transition-colors text-sm font-medium"
              >
                <ShieldCheck size={14} /> Approve Write-Back
              </button>
              <button
                onClick={() => setDecision('rejected')}
                className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-ink-700/50 text-ink-200 border border-ink-600/40 hover:bg-ink-700 transition-colors text-sm font-medium"
              >
                <ShieldAlert size={14} /> Reject
              </button>
            </div>
          </div>
        )}

        <AnimatePresence mode="wait">
          {decision === 'approved' && (
            <motion.div
              key="approved"
              initial={{ opacity: 0, scale: 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              className="pl-8"
            >
              <motion.div
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                className="flex items-center gap-3 p-3 rounded-lg bg-supported/10 border border-supported/20"
              >
                <motion.div
                  initial={{ scale: 0, rotate: -45 }}
                  animate={{ scale: 1, rotate: 0 }}
                  transition={{ delay: 0.1, type: 'spring', stiffness: 200 }}
                  className="flex items-center justify-center w-7 h-7 rounded-full bg-supported text-ink-950"
                >
                  <ShieldCheck size={15} strokeWidth={3} />
                </motion.div>
                <div className="flex-1">
                  <div className="text-sm text-supported font-medium">
                    Verdict written to catalog
                  </div>
                  <div className="text-xs text-ink-300">
                    Contradicted flag persisted to {claim.evidence.table}
                  </div>
                </div>
                <a
                  href="#"
                  onClick={(e) => e.preventDefault()}
                  className="text-xs text-accent-400 hover:text-accent-400 transition-colors font-medium"
                >
                  View in DataHub →
                </a>
              </motion.div>
            </motion.div>
          )}
          {decision === 'rejected' && (
            <motion.div
              key="rejected"
              initial={{ opacity: 0, scale: 0.96 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0 }}
              className="pl-8"
            >
              <div className="flex items-center gap-3 p-3 rounded-lg bg-ink-700/40 border border-ink-600/40">
                <div className="flex items-center justify-center w-7 h-7 rounded-full bg-ink-500 text-ink-200">
                  <ShieldAlert size={15} />
                </div>
                <div className="flex-1">
                  <div className="text-sm text-ink-100 font-medium">Write-back rejected</div>
                  <div className="text-xs text-ink-400">No changes written to the catalog.</div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

export default function ClaimCard({ claim, index }: { claim: Claim; index: number }) {
  const [expanded, setExpanded] = useState(false);

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
          <p className="text-sm lg:text-[0.95rem] text-ink-100 leading-relaxed">{claim.text}</p>
          <div className="flex items-center gap-3 mt-3 flex-wrap">
            <VerdictBadge verdict={claim.verdict} size="sm" />
            <span className="font-mono-nums text-xs text-ink-400">
              {(claim.confidence * 100).toFixed(0)}% confidence
            </span>
            <span className="flex items-center gap-1 text-xs text-ink-400">
              {claim.explanationSource === 'ai' ? (
                <>
                  <Sparkles size={10} /> AI-written
                </>
              ) : (
                <>
                  <FileText size={10} /> Safe template
                </>
              )}
            </span>
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
                  {claim.explanationSource === 'ai' ? <Sparkles size={11} /> : <FileText size={11} />}
                  Explanation
                </div>
                <p className="text-sm text-ink-200 leading-relaxed">{claim.explanation}</p>
              </div>

              {/* Evidence receipt */}
              <div className="flex items-center gap-1.5 text-label-sm mb-1">
                <Database size={11} /> Evidence
              </div>
              <EvidenceReceipt evidence={claim.evidence} />

              {/* Correction panel for contradicted claims with correction attempts */}
              {claim.correctionAttempt && <CorrectionPanel claim={claim} />}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
