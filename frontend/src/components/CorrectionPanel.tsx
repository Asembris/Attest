import { motion } from 'framer-motion';
import { GitBranch, ShieldAlert, ShieldCheck } from 'lucide-react';
import type { ClaimRecord, CorrectionOutcome } from '../api/types';

// Phase 2: a READ-ONLY rendering of the correction loop's outcome. The real approve/reject
// buttons and the write-back result are wired in Phase 4 — nothing here fakes a decision or
// a catalog write. `outcome` is one of six names, not a boolean, and each is shown as what it
// is: only `corrected` (a revision that re-verified clean) is a proposal a human can accept;
// the rest are honest dead-ends the loop reached and are shown as such, never as corrections.

const OUTCOME: Record<
  CorrectionOutcome,
  { label: string; blurb: string; tone: 'proposal' | 'firm' | 'none' }
> = {
  corrected: {
    label: 'Correction proposed',
    blurb: 'The agent revised the claim and it re-verified against the same snapshot. Awaiting a human.',
    tone: 'proposal',
  },
  'stood-firm': {
    label: 'Stood by the claim',
    blurb: 'The evidence does not say what the truth is, so no honest revision exists. The claim stands, and is marked wrong.',
    tone: 'firm',
  },
  refused: {
    label: 'Declined to revise',
    blurb: 'The agent did not propose a revision.',
    tone: 'firm',
  },
  exhausted: {
    label: 'Retry limit reached',
    blurb: 'The correction loop hit its retry cap without producing a claim that re-verified.',
    tone: 'firm',
  },
  'not-corrected': {
    label: 'Not corrected',
    blurb: 'A revision was attempted but did not re-verify.',
    tone: 'firm',
  },
  'not-attempted': { label: '', blurb: '', tone: 'none' },
};

export default function CorrectionPanel({ claim }: { claim: ClaimRecord }) {
  const meta = OUTCOME[claim.correction.outcome];
  if (meta.tone === 'none') return null;

  const proposal = claim.correction.proposal;
  const revisedText = typeof proposal?.raw_text === 'string' ? proposal.raw_text : null;
  const isProposal = meta.tone === 'proposal';

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="overflow-hidden"
    >
      <div className={`mt-4 border rounded-xl p-5 space-y-4 ${isProposal ? 'border-insufficient/25 bg-insufficient/5' : 'border-ink-600/40 bg-ink-800/40'}`}>
        <div className="flex items-center gap-2">
          <div className={`flex items-center justify-center w-6 h-6 rounded-full ${isProposal ? 'bg-insufficient/15 text-insufficient' : 'bg-ink-700 text-ink-300'}`}>
            {isProposal ? <GitBranch size={13} /> : <ShieldCheck size={13} />}
          </div>
          <span className={`text-sm font-medium ${isProposal ? 'text-insufficient' : 'text-ink-200'}`}>
            {meta.label}
          </span>
          <span className="text-label-sm ml-auto">{claim.correction.outcome}</span>
        </div>

        <p className={`text-sm text-ink-200 leading-relaxed pl-8 border-l-2 ml-2 ${isProposal ? 'border-insufficient/30' : 'border-ink-600/40'}`}>
          {meta.blurb}
        </p>

        {revisedText && (
          <div className="space-y-2 pl-8">
            <div className="text-xs text-ink-300">
              <span className="text-label-sm mr-2">Original</span>
              <span className="text-ink-200 line-through decoration-ink-500">{claim.raw_text}</span>
            </div>
            <div className="text-xs text-ink-300">
              <span className="text-label-sm mr-2">Proposed</span>
              <span className="text-ink-100">{revisedText}</span>
            </div>
          </div>
        )}

        {isProposal && (
          <div className="pl-8 flex items-center gap-2 text-xs text-ink-400 italic">
            <ShieldAlert size={12} />
            A human decides before anything is written back. (Approve/reject is wired in Phase 4.)
          </div>
        )}
      </div>
    </motion.div>
  );
}
