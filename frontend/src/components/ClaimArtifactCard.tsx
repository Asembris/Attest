import { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { ChevronDown, ExternalLink, History, RefreshCw, Tag } from 'lucide-react';
import type { ClaimView } from '../api/types';
import { corpuserId, datahubDatasetUrl } from '../api/types';
import VerdictBadge from './VerdictBadge';
import ReadStateBadge, { readStateBlurb } from './ReadStateBadge';

// ONE CLAIM ARTIFACT, as the CATALOG holds it. Not an audit result — a published fact that
// any reader can fetch out of DataHub without Attest running at all.
//
// The verdict history is the part a last-write-wins field could never hold: `assertionRunEvent`
// is a timeseries aspect, so re-auditing a claim APPENDS. A claim that was Supported in March
// and Contradicted in July carries both, in order, each with the run that reached it and the
// human who signed it off. "Was this ever contradicted before someone fixed the tag" is
// answerable here, from the catalog alone.
//
// Re-typeset to the design pass (the lineage rail with drawn dots), but every semantic element
// is preserved from the working version: the four read-states, repair gated on the FAILED
// WRITE not the state, structured absence-preserving evidence, the stale-tag chip and the
// snapshot identity. The design-pass mockup regressed all four (evidence flattened to a
// string, stale_tag/snapshot_id dropped) — those regressions are not ported.

function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function shortUrn(urn: string): string {
  // `urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.profile,PROD)` ->
  // `analytics.customers.profile`. The whole URN is still on the card; this is the header.
  const parts = urn.split(',');
  return parts.length >= 2 ? parts[1] : urn;
}

// One evidence value, with the ABSENCE preserved rather than blanked. `null` is the catalog
// being SILENT — the justification for Insufficient-Coverage — and it must read as such, not
// as an empty cell that looks like the catalog said "" or []. See VerdictEventView.evidence.
function evidenceValue(value: unknown): string {
  if (value === null || value === undefined) return '∅ (absent — the catalog is silent)';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function verdictHex(v: string): string {
  return v === 'Supported' ? '#5FB98D' : v === 'Contradicted' ? '#D96A64' : '#C2A265';
}

export default function ClaimArtifactCard({
  claim,
  index,
  onRetry,
  retrying,
}: {
  claim: ClaimView;
  index: number;
  onRetry: (runId: string) => void;
  retrying: boolean;
}) {
  const [expanded, setExpanded] = useState(false);

  // THE RETRY IS OFFERED FOR A FAILED WRITE — not for a state.
  //
  // Gating on `state === 'incomplete'` looks equivalent and is not. A claim artifact is
  // content-addressed, so re-auditing a claim appends to the artifact it already has: a
  // claim audited last month and re-audited today, whose new verdict FAILED to write, still
  // shows last month's verdict. It reads `complete` — the catalog does hold a verdict — while
  // the latest write sits recorded as broken. Gated on the state, that claim could never be
  // repaired from here and the stale verdict would stand indefinitely.
  //
  // What must never get this control is `pending-lag`: its write landed, there is nothing to
  // repair, and a retry there invites a human to "fix" a two-second wait. `unknown` gets none
  // either — there is no recorded write to re-run. Both follow from keying on failed_step,
  // which is set for neither.
  const repairable = Boolean(claim.failed_step) && Boolean(claim.audit_run);
  // A failed write on a claim that still shows an OLDER verdict. Worth saying out loud: the
  // verdict on screen is real and is simply not the newest audit's.
  const stale = repairable && claim.state === 'complete';

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 22 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: Math.min(index * 0.05, 0.35), duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className="rounded-2xl border overflow-hidden transition-colors"
      style={{
        borderColor: expanded ? 'rgba(150,158,168,0.3)' : 'rgba(150,158,168,0.14)',
        background: expanded ? 'rgba(150,158,168,0.04)' : 'rgba(150,158,168,0.02)',
      }}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-start gap-4 p-5 text-left hover:bg-ink-800/30 transition-colors"
      >
        <span className="font-mono-nums text-xs text-ink-500 w-6 pt-1 shrink-0">
          {String(index + 1).padStart(2, '0')}
        </span>

        <div className="flex-1 min-w-0">
          <p className="text-sm lg:text-[0.97rem] text-ink-100 leading-relaxed">
            {claim.description || claim.claim_urn}
          </p>
          <div className="flex items-center gap-2.5 mt-3.5 flex-wrap">
            {/* The verdict, when the catalog has one. When it does not, the read-state is
                the whole answer — and a null verdict is NEVER rendered as a verdict. */}
            {claim.verdict ? <VerdictBadge verdict={claim.verdict} size="sm" /> : null}
            <ReadStateBadge state={claim.state} />
            {/* The verdict tag lags the latest verdict — detected from the catalog alone, so a
                store=None reader sees it too. A verdict-filtered search cannot find this claim
                until the tag is repaired, though a dataset read (like this one) can. */}
            {claim.stale_tag && (
              <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-contradicted/10 text-contradicted font-mono-nums text-[0.7rem]">
                <Tag size={9} /> stale tag
              </span>
            )}
            <span className="font-mono-nums text-xs text-ink-500">{claim.claim_type}</span>
            <span className="font-mono-nums text-xs text-ink-500">{claim.grain}</span>
            <span className="font-mono-nums text-xs text-ink-500 truncate">
              {shortUrn(claim.target_urn)}
            </span>
            {claim.history.length > 0 && (
              <span className="inline-flex items-center gap-1 text-xs text-ink-400">
                <History size={10} />
                {claim.history.length} verdict{claim.history.length === 1 ? '' : 's'}
              </span>
            )}
          </div>
        </div>

        <motion.div animate={{ rotate: expanded ? 180 : 0 }} className="pt-1 shrink-0 text-ink-400">
          <ChevronDown size={18} />
        </motion.div>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            <div className="px-5 pb-6 pl-[3.75rem]">
              <div className="divider mb-5" />

              {/* What this claim's state actually means, in words. The badge is a label; a
                  reader deciding whether to act needs the sentence. */}
              <p className="mb-5 text-xs text-ink-400 leading-relaxed max-w-[82ch]">
                {readStateBlurb(claim.state)}
              </p>

              {/* THE REPAIR. Gated on `repairable` — the FAILED WRITE, not the read-state:
                  a re-audited claim whose new write failed still shows its OLDER verdict and
                  reads `complete`, and gating on the state would strand it forever. */}
              {repairable && (
                <div className="mb-5 flex items-center gap-3 p-4 rounded-xl bg-contradicted/[0.08] border border-contradicted/30">
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-contradicted font-medium">
                      The write stopped at {claim.failed_step}
                    </div>
                    <div className="text-xs text-ink-300 leading-relaxed mt-0.5">
                      {stale
                        ? 'The verdict shown is real, and it is from an EARLIER audit — the latest one did not land. Re-running the write adds it.'
                        : claim.failed_step === 'tag'
                          ? 'The verdict is correct and merely not findable by a verdict search yet. The tag is a derived index, never the verdict itself.'
                          : 'This claim is in the catalog with no verdict. Re-running the write completes it.'}{' '}
                      Safe to run more than once: it lands on the same artifact and cannot
                      append a duplicate.
                    </div>
                  </div>
                  <button
                    onClick={() => onRetry(claim.audit_run)}
                    disabled={retrying}
                    className="btn-primary text-xs shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
                  >
                    <RefreshCw size={12} className={retrying ? 'animate-spin' : ''} />
                    {retrying ? 'Repairing…' : 'Repair write'}
                  </button>
                </div>
              )}

              {/* What the claim asserts — the SUBJECT that a bare structured property could
                  never carry, and the whole reason the artifact exists. */}
              <div className="mb-6">
                <div className="text-label-sm mb-3">Asserted</div>
                {Object.keys(claim.asserted).length === 0 ? (
                  <p className="text-xs text-ink-400">This artifact carries no asserted payload.</p>
                ) : (
                  <div className="space-y-1.5">
                    {Object.entries(claim.asserted).map(([key, value]) => (
                      <div key={key} className="flex items-baseline gap-4 text-xs">
                        <span className="font-mono-nums text-ink-500 w-36 shrink-0">{key}</span>
                        <span className="font-mono-nums text-ink-100 break-all">
                          {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {/* THE HISTORY — the lineage peak. Append-only, newest first. */}
              <div className="mb-6">
                <div className="flex items-center justify-between mb-4">
                  <div className="flex items-center gap-1.5 text-label-sm">
                    <History size={11} /> Verdict history
                  </div>
                  <a
                    href={datahubDatasetUrl(claim.target_urn)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-accent-400 hover:text-accent transition-colors font-medium"
                  >
                    View in DataHub <ExternalLink size={11} />
                  </a>
                </div>
                {claim.history.length === 0 ? (
                  <p className="text-xs text-ink-400 leading-relaxed max-w-[72ch]">
                    No verdict has been recorded against this claim yet. That is not a verdict
                    of "unknown" — the claim exists and the catalog holds no answer for it.
                  </p>
                ) : (
                  <div className="relative pl-1">
                    {/* the drawn rail — a flip reads as a sequence, not a list */}
                    <motion.div
                      initial={{ scaleY: 0 }}
                      animate={{ scaleY: 1 }}
                      transition={{ duration: 0.5, ease: [0.4, 0.8, 0.3, 1] }}
                      className="absolute left-[9px] top-2 bottom-2 w-px origin-top bg-gradient-to-b from-ink-500/60 to-ink-600/20"
                    />
                    <div className="flex flex-col gap-2.5">
                      {claim.history.map((event, i) => (
                        <motion.div
                          key={`${event.at}-${i}`}
                          initial={{ opacity: 0, x: 8 }}
                          animate={{ opacity: 1, x: 0 }}
                          transition={{ delay: i * 0.09, duration: 0.4 }}
                          className="relative flex gap-4"
                        >
                          <motion.span
                            initial={{ scale: 0 }}
                            animate={{ scale: 1 }}
                            transition={{ delay: i * 0.09, type: 'spring', stiffness: 400, damping: 20 }}
                            className="w-[18px] h-[18px] shrink-0 rounded-full mt-1 flex items-center justify-center"
                            style={{
                              background: verdictHex(event.verdict),
                              boxShadow: `0 0 14px -2px ${verdictHex(event.verdict)}`,
                            }}
                          >
                            <span className="w-1.5 h-1.5 rounded-full bg-ink-950" />
                          </motion.span>
                          <div className="flex-1 min-w-0 p-3.5 rounded-xl bg-ink-800/40 border border-ink-700/40">
                            <div className="flex items-center gap-2 flex-wrap">
                              <VerdictBadge verdict={event.verdict} size="sm" />
                              {i === 0 && claim.history.length > 1 && (
                                <span className="font-mono-nums text-[9.5px] tracking-[0.12em] text-ink-500 border border-ink-600/50 px-1.5 py-0.5 rounded">
                                  LATEST
                                </span>
                              )}
                              <span className="font-mono-nums text-xs text-ink-400">{when(event.at)}</span>
                            </div>
                            <div className="mt-2 text-xs text-ink-300 space-y-1">
                              {event.reviewer && (
                                <div>
                                  Published by{' '}
                                  <span className="text-ink-100">{corpuserId(event.reviewer)}</span>
                                </div>
                              )}
                              {/* STRUCTURED evidence, absence preserved — never a flat string. */}
                              {event.evidence.length > 0 && (
                                <div className="space-y-0.5">
                                  {event.evidence.map((e, j) => (
                                    <div
                                      key={j}
                                      className="font-mono-nums text-ink-400 break-all text-[0.7rem]"
                                    >
                                      {e.field}
                                      <span className="text-ink-500"> = </span>
                                      {evidenceValue(e.value)}
                                    </div>
                                  ))}
                                </div>
                              )}
                              {/* The snapshot this verdict was decided against — WHICH state of
                                  the world it held true for (Session 21). */}
                              {event.snapshot_id && (
                                <div className="font-mono-nums text-ink-500 text-[0.7rem]">
                                  snapshot {event.snapshot_id}
                                </div>
                              )}
                              {event.audit_run && (
                                <div className="font-mono-nums text-ink-500 text-[0.7rem]">
                                  run {event.audit_run.slice(0, 8)}
                                </div>
                              )}
                            </div>
                          </div>
                        </motion.div>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* THE ARTIFACT identity. */}
              <div className="space-y-1.5">
                <div className="text-label-sm mb-2">Artifact</div>
                <div className="flex items-baseline gap-4 text-xs">
                  <span className="font-mono-nums text-ink-500 w-36 shrink-0">claim urn</span>
                  <span className="font-mono-nums text-ink-100 break-all">{claim.claim_urn}</span>
                </div>
                <div className="flex items-baseline gap-4 text-xs">
                  <span className="font-mono-nums text-ink-500 w-36 shrink-0">dataset</span>
                  <span className="font-mono-nums text-ink-100 break-all">{claim.target_urn}</span>
                </div>
                {claim.tags.length > 0 && (
                  <div className="flex items-baseline gap-4 text-xs">
                    <span className="font-mono-nums text-ink-500 w-36 shrink-0">tags</span>
                    <span className="flex flex-wrap gap-1.5">
                      {claim.tags.map((t) => (
                        <span
                          key={t}
                          className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-ink-700/60 text-ink-200 font-mono-nums text-[0.7rem]"
                        >
                          <Tag size={9} />
                          {t.replace('urn:li:tag:', '')}
                        </span>
                      ))}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
