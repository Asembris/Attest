import { motion, AnimatePresence } from 'framer-motion';
import { Check, ExternalLink, ShieldAlert, ShieldCheck, XCircle } from 'lucide-react';
import type { ClaimRecord, WriteBackView } from '../api/types';
import { datahubDatasetUrl } from '../api/types';

// THE PUBLICATION ACT — and it had no control at all until now.
//
// Publishing a verdict is its OWN decision, separate from accepting a correction, and that
// separation is the whole of the Option A decision (report.PublicationStatus). The UI used
// to offer one button, on corrected claims only, that meant both — so the only verdict a
// person could ever send to the catalog was a contradiction the agent had already
// self-corrected. A STOOD_FIRM contradiction (the agent was wrong and refused to fix it —
// the most damning thing Attest can find) had no control, and neither did any Supported or
// Insufficient-Coverage verdict.
//
// EVERY CLAIM GETS THIS, whatever its verdict, and that is the point rather than an
// inconvenience. Attest's whole thesis is that ABSENCE IS NOT AN ANSWER: an untagged table
// does not mean "PII-free", it means nobody looked. A dataset with no Attest verdict was
// ambiguous between "we checked and it was fine" and "we never checked" — Attest committing
// its own cardinal sin in its own output. Publishing a Supported verdict is how the catalog
// learns that someone looked.
//
// There is deliberately no "publish all". That is the Session 3 accountability decision and
// a review UI is exactly where it would get traded away for convenience.

export default function PublicationPanel({
  claim,
  reviewable,
  publish,
  onPublish,
  writeback,
}: {
  claim: ClaimRecord;
  reviewable: boolean;
  publish: boolean | undefined;
  onPublish: (publish: boolean) => void;
  writeback: WriteBackView | null;
}) {
  const status = claim.publication.status;

  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="overflow-hidden"
    >
      <div className="mt-4 border border-ink-600/40 bg-ink-800/40 rounded-xl p-5 space-y-4">
        <div className="flex items-center gap-2">
          <div className="flex items-center justify-center w-6 h-6 rounded-full bg-ink-700 text-ink-300">
            <ShieldCheck size={13} />
          </div>
          <span className="text-sm font-medium text-ink-200">Publish this verdict</span>
          <span className="text-label-sm ml-auto">{status}</span>
        </div>

        <p className="text-sm text-ink-300 leading-relaxed pl-8 border-l-2 border-ink-600/40 ml-2">
          Writes <span className="text-ink-100">{claim.verdict}</span> to the catalog as this
          claim's own artifact — what it asserts, at what grain, and who signed it off.
          Withholding writes nothing. A verdict Attest never records is indistinguishable
          from a claim Attest never checked.
        </p>

        {reviewable && status === 'pending' && (
          <div className="pl-8 pt-1">
            {/* Only while this VERDICT is still pending. The card can be live for its
                correction after its verdict is published (a correction-only re-park), and
                offering to publish an already-published verdict there would be nonsense. */}
            {/* Same idiom as the correction control: the picked choice fills solid, the other
                dims but stays clickable, so a decision is reversible before Submit. */}
            <div className="flex items-center gap-3">
              <button
                onClick={() => onPublish(true)}
                aria-pressed={publish === true}
                className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  publish === true
                    ? 'bg-supported text-ink-950 border-supported shadow-sm shadow-supported/30'
                    : publish === false
                      ? 'bg-supported/5 text-supported/50 border-supported/15 opacity-70 hover:opacity-100 hover:bg-supported/15'
                      : 'bg-supported/10 text-supported border-supported/30 hover:bg-supported/20'
                }`}
              >
                {publish === true ? <Check size={15} strokeWidth={3} /> : <ShieldCheck size={14} />}
                {publish === true ? 'Publishing' : 'Publish verdict'}
              </button>
              <button
                onClick={() => onPublish(false)}
                aria-pressed={publish === false}
                className={`inline-flex items-center gap-2 px-4 py-2 rounded-lg border text-sm font-medium transition-all ${
                  publish === false
                    ? 'bg-ink-400 text-ink-950 border-ink-300 shadow-sm shadow-ink-900/40'
                    : publish === true
                      ? 'bg-ink-700/40 text-ink-400 border-ink-600/40 opacity-70 hover:opacity-100 hover:bg-ink-700'
                      : 'bg-ink-700/50 text-ink-200 border-ink-600/40 hover:bg-ink-700'
                }`}
              >
                {publish === false ? <Check size={15} strokeWidth={3} /> : <ShieldAlert size={14} />}
                {publish === false ? 'Withheld' : 'Withhold'}
              </button>
            </div>
          </div>
        )}

        {/* The settled result. The write-back is reported apart from the decision because
            they fail apart: the human decided, and the catalog may not have heard. */}
        <AnimatePresence>
          {status === 'published' && (
            <motion.div key="published" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="pl-8">
              {/* THREE honest states, not two. A null write-back is NOT a success: it is the
                  absence of a receipt (a settled run reloaded from GET /audit carries none),
                  and rendering it green claimed "written to catalog" for a write this view
                  never saw land — the exact absence-read-as-confirmation this product exists to
                  refuse. Only a writeback.ok === true receipt earns the green. */}
              {!writeback ? (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-ink-700/40 border border-ink-600/40">
                  <div className="flex items-center justify-center w-7 h-7 rounded-full bg-ink-500 text-ink-100 shrink-0">
                    <ShieldCheck size={15} strokeWidth={2.5} />
                  </div>
                  <div className="flex-1">
                    <div className="text-sm text-ink-100 font-medium">Verdict published</div>
                    <div className="text-xs text-ink-400 break-words">
                      The decision is recorded. This view carries no catalog write-back receipt —
                      read it back from the catalog (<span className="font-mono-nums">GET /claims</span>)
                      to confirm the artifact.
                    </div>
                  </div>
                  <a
                    href={datahubDatasetUrl(claim.target_urn)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-accent-400 hover:text-accent transition-colors font-medium shrink-0"
                  >
                    View in DataHub <ExternalLink size={12} />
                  </a>
                </div>
              ) : !writeback.ok ? (
                <div className="flex items-center gap-3 p-3 rounded-lg bg-contradicted/10 border border-contradicted/25">
                  <XCircle size={18} className="text-contradicted shrink-0" />
                  <div className="flex-1">
                    <div className="text-sm text-contradicted font-medium">
                      Published, but the catalog write failed
                      {writeback.failed_step ? ` at ${writeback.failed_step}` : ''}
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
                      {claim.verdict} persisted as a claim artifact on {claim.target_urn}
                    </div>
                  </div>
                  <a
                    href={datahubDatasetUrl(claim.target_urn)}
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
          {status === 'withheld' && (
            <motion.div key="withheld" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="pl-8">
              <div className="flex items-center gap-3 p-3 rounded-lg bg-ink-700/40 border border-ink-600/40">
                <div className="flex items-center justify-center w-7 h-7 rounded-full bg-ink-500 text-ink-200 shrink-0">
                  <ShieldAlert size={15} />
                </div>
                <div className="flex-1">
                  <div className="text-sm text-ink-100 font-medium">Verdict withheld</div>
                  <div className="text-xs text-ink-400">
                    A human looked and declined to publish. Nothing was written to the catalog.
                  </div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}
