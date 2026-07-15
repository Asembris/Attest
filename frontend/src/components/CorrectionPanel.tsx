import { motion, AnimatePresence } from 'framer-motion';
import { GitBranch, ShieldAlert, ShieldCheck, ExternalLink, XCircle, Check } from 'lucide-react';
import type { ClaimRecord, CorrectionOutcome, WriteBackView } from '../api/types';

// Phase 4: the correction loop's outcome AND the real human checkpoint. `outcome` is one of
// six names, not a boolean. Only `corrected` (a revision that re-verified clean against the
// same snapshot) is a proposal a human can accept; the rest are honest dead-ends shown as
// what they are, never as corrections.
//
// Approve/reject is the REAL durable flow: the decision is collected here, submitted to
// POST /audit/{run_id}/approve (App), which resumes the parked graph through the
// human_checkpoint node and, on accept, writes the verdict back to DataHub. The write-back
// result is reported truthfully — a write that failed says so while the decision still stands.

const OUTCOME: Record<
  CorrectionOutcome,
  { label: string; blurb: string; tone: 'proposal' | 'firm' | 'none' }
> = {
  corrected: {
    label: 'Correction proposed',
    blurb: 'The agent revised the claim and it re-verified against the same snapshot.',
    tone: 'proposal',
  },
  'stood-firm': {
    label: 'Stood by the claim',
    blurb: 'The evidence does not say what the truth is, so no honest revision exists. The claim stands, and is marked wrong.',
    tone: 'firm',
  },
  refused: { label: 'Declined to revise', blurb: 'The agent did not propose a revision.', tone: 'firm' },
  exhausted: {
    label: 'Retry limit reached',
    blurb: 'The correction loop hit its retry cap without producing a claim that re-verified.',
    tone: 'firm',
  },
  'not-corrected': { label: 'Not corrected', blurb: 'A revision was attempted but did not re-verify.', tone: 'firm' },
  'not-attempted': { label: '', blurb: '', tone: 'none' },
};

function datahubUrl(urn: string): string {
  return `http://localhost:9002/dataset/${encodeURIComponent(urn)}`;
}

export default function CorrectionPanel({
  claim,
  reviewable,
  decision,
  onDecide,
  writeback,
}: {
  claim: ClaimRecord;
  reviewable: boolean;
  decision: boolean | undefined;
  onDecide: (accept: boolean) => void;
  writeback: WriteBackView | null;
}) {
  const meta = OUTCOME[claim.correction.outcome];
  if (meta.tone === 'none') return null;

  const proposal = claim.correction.proposal;
  const revisedText = typeof proposal?.raw_text === 'string' ? proposal.raw_text : null;
  const isProposal = meta.tone === 'proposal';
  const review = claim.correction.review;

  return (
    <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} className="overflow-hidden">
      <div
        className={`mt-4 border rounded-xl p-5 space-y-4 ${
          isProposal ? 'border-insufficient/25 bg-insufficient/5' : 'border-ink-600/40 bg-ink-800/40'
        }`}
      >
        <div className="flex items-center gap-2">
          <div
            className={`flex items-center justify-center w-6 h-6 rounded-full ${
              isProposal ? 'bg-insufficient/15 text-insufficient' : 'bg-ink-700 text-ink-300'
            }`}
          >
            {isProposal ? <GitBranch size={13} /> : <ShieldCheck size={13} />}
          </div>
          <span className={`text-sm font-medium ${isProposal ? 'text-insufficient' : 'text-ink-200'}`}>
            {meta.label}
          </span>
          <span className="text-label-sm ml-auto">{claim.correction.outcome}</span>
        </div>

        <p
          className={`text-sm text-ink-200 leading-relaxed pl-8 border-l-2 ml-2 ${
            isProposal ? 'border-insufficient/30' : 'border-ink-600/40'
          }`}
        >
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

        {/* The live human checkpoint — only while the run is parked and this is a pending proposal. */}
        {reviewable && (
          <div className="pl-8 pt-1">
            <div className="text-xs text-ink-300 mb-3 italic">
              A human decides before anything is written back. Approving writes the verdict to DataHub.
            </div>
            {/* The selected choice fills SOLID and shifts its label; the unpicked one dims.
                Both stay clickable so the decision is reversible before Submit — clicking the
                other option moves the filled state with it. */}
            <div className="flex items-center gap-3">
              <button
                onClick={() => onDecide(true)}
                aria-pressed={decision === true}
                className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  decision === true
                    ? 'bg-supported text-ink-950 border-supported shadow-sm shadow-supported/30'
                    : decision === false
                      ? 'bg-supported/5 text-supported/50 border-supported/15 opacity-70 hover:opacity-100 hover:bg-supported/15'
                      : 'bg-supported/10 text-supported border-supported/30 hover:bg-supported/20'
                }`}
              >
                {decision === true ? <Check size={15} strokeWidth={3} /> : <ShieldCheck size={14} />}
                {decision === true ? 'Approved' : 'Approve write-back'}
              </button>
              <button
                onClick={() => onDecide(false)}
                aria-pressed={decision === false}
                className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  decision === false
                    ? 'bg-ink-400 text-ink-950 border-ink-300 shadow-sm shadow-ink-900/40'
                    : decision === true
                      ? 'bg-ink-700/40 text-ink-400 border-ink-600/40 opacity-70 hover:opacity-100 hover:bg-ink-700'
                      : 'bg-ink-700/50 text-ink-200 border-ink-600/40 hover:bg-ink-700'
                }`}
              >
                {decision === false ? <Check size={15} strokeWidth={3} /> : <ShieldAlert size={14} />}
                {decision === false ? 'Rejected' : 'Reject'}
              </button>
            </div>
          </div>
        )}

        {/* The settled result — after the run is resumed and the catalog write attempted. */}
        <AnimatePresence>
          {review === 'accepted' && (
            <motion.div key="accepted" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="pl-8">
              {writeback && !writeback.ok ? (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-contradicted/10 border border-contradicted/25">
                  <XCircle size={18} className="text-contradicted shrink-0" />
                  <div className="flex-1">
                    <div className="text-sm text-contradicted font-medium">
                      Approved, but the catalog write failed
                    </div>
                    <div className="text-xs text-ink-300">
                      The decision stands; DataHub was not updated. {writeback.detail}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-supported/10 border border-supported/20">
                  <div className="flex items-center justify-center w-7 h-7 rounded-full bg-supported text-ink-950 shrink-0">
                    <ShieldCheck size={15} strokeWidth={3} />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm text-supported font-medium">Verdict written to catalog</div>
                    <div className="text-xs text-ink-300 break-words">
                      {claim.verdict} persisted to {claim.target_urn}
                    </div>
                  </div>
                  <a
                    href={datahubUrl(claim.target_urn)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-accent-400 hover:text-accent transition-colors font-medium shrink-0"
                  >
                    View in DataHub <ExternalLink size={12} />
                  </a>
                </div>
              )}
            </motion.div>
          )}
          {review === 'rejected' && (
            <motion.div key="rejected" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="pl-8">
              <div className="flex items-center gap-3 p-3 rounded-lg bg-ink-700/40 border border-ink-600/40">
                <div className="flex items-center justify-center w-7 h-7 rounded-full bg-ink-500 text-ink-200 shrink-0">
                  <ShieldAlert size={15} />
                </div>
                <div className="flex-1">
                  <div className="text-sm text-ink-100 font-medium">Write-back rejected</div>
                  <div className="text-xs text-ink-400">No changes were written to the catalog.</div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}
