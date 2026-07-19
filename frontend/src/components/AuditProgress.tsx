import { useEffect, useState, type ReactNode } from 'react';
import { motion } from 'framer-motion';
import { ArrowRight } from 'lucide-react';
import type { AuditRecord, ClaimRecord, CorrectionOutcome, Verdict } from '../api/types';
import { verdictCounts } from '../api/types';
import { CountUp } from './reveal';

// THE AUDIT-IN-PROGRESS SCREEN, and the honest half of the design pass.
//
// The backend is a SINGLE BLOCKING POST /audit — there is no streaming, no per-step progress
// event. The run's telemetry (tokens, cost, verdicts, lookups) does not EXIST until the call
// returns. So this screen is deliberately in two phases:
//
//   1. while `record === null` (the POST is in flight): an INDETERMINATE stage animation.
//      It shows the pipeline's SHAPE — sanitize, decompose, per-claim loop, sign-off — as an
//      ambient "working" indicator, and asserts ZERO numbers. The only live number is the
//      wall-clock elapsed, which is real. The receipts rail shows "—", never a fabricated
//      count. The design-pass mockup scripted a fake run here (5,939 tokens, $0.0013, a
//      correction to `users_archive` owned by `marco.reyes` that contradicts the real seed);
//      none of that is ported — it would be inventing telemetry the product exists to catch.
//
//   2. when `record` arrives: a brief REPLAY REVEAL of the REAL record — record.receipts,
//      the real verdict tally, the real per-claim verdicts, and the correction outcome the
//      run actually reached. Every figure is measured. `usd` renders "unknown" when null,
//      NEVER $0 (the None-is-not-zero invariant from cost.py holds in the UI too). Then it
//      auto-advances to the results screen (the human checkpoint). A "Continue" button lets a
//      person skip the wait; the auto-advance is what keeps the browser E2E — which never
//      clicks Continue — green.

const STAGES = ['Sanitize', 'Decompose', 'Per-claim loop', 'Sign-off'];
const VERDICT_DOT: Record<Verdict, string> = {
  Supported: 'bg-supported',
  Contradicted: 'bg-contradicted',
  'Insufficient-Coverage': 'bg-insufficient',
};

// The correction outcomes worth surfacing on the reveal, and how loud each is. `stood-firm`
// and `refused` are the damning ones — the agent was shown the catalog and did NOT correct a
// false claim — so they get the prominent callout the mockup reserved for its (scripted)
// subject-swap. Driven entirely by the real record; absent when the run had no such outcome.
const CORRECTION: Record<CorrectionOutcome, { label: string; tone: 'firm' | 'proposal' } | null> = {
  corrected: { label: 'Correction proposed', tone: 'proposal' },
  'stood-firm': { label: 'Stood firm · correction refused', tone: 'firm' },
  refused: { label: 'Declined to revise', tone: 'firm' },
  exhausted: { label: 'Retry limit reached', tone: 'firm' },
  'not-corrected': { label: 'Not corrected', tone: 'firm' },
  'not-attempted': null,
};

export default function AuditProgress({
  record,
  onContinue,
}: {
  record: AuditRecord | null;
  onContinue: () => void;
}) {
  const done = record !== null;

  // Wall-clock elapsed while the POST is in flight (a real number). Frozen once the record
  // arrives; from then on the rail shows the record's own measured latency.
  const [wall, setWall] = useState(0);
  useEffect(() => {
    if (done) return;
    const start = performance.now();
    const iv = setInterval(() => setWall(performance.now() - start), 100);
    return () => clearInterval(iv);
  }, [done]);

  // NO AUTO-ADVANCE. On completion the replay HOLDS and stays scrollable; the ONLY way to
  // the results checkpoint is the Continue button in the pinned bar below. A timer a human
  // never sees is not a real path forward — so the browser E2E now clicks Continue too,
  // exercising the same move a person makes rather than waiting on a clock.

  return (
    <>
    <div className="min-h-screen grid grid-cols-1 lg:grid-cols-[1fr_340px]" style={{ background: 'radial-gradient(130% 100% at 30% 0%, #101216 0%, #0A0B0D 55%, #08090B 100%)' }}>
      {/* ===== MAIN ===== */}
      <main className="px-6 lg:px-14 py-12 lg:py-14 overflow-hidden">
        <header className="flex items-baseline justify-between mb-8">
          <div className="flex items-center gap-3.5">
            <span className="font-serif text-[25px] font-semibold tracking-tight">Attest</span>
            <span className="w-px h-4 bg-ink-600/60" />
            <span className="font-mono-nums text-[11px] tracking-[0.22em] uppercase text-ink-400">
              {done ? 'Audit complete' : 'Audit in progress'}
            </span>
          </div>
          <div className="flex items-center gap-6 font-mono-nums text-xs text-ink-400">
            <span>
              run <span className="text-ink-200">{record ? record.run_id.slice(0, 8) : '····'}</span>
            </span>
            <span className="text-ink-200">{elapsed(done ? record!.receipts.latency_ms : wall)}</span>
          </div>
        </header>

        {/* progress bar — indeterminate while working, full when done */}
        <div className="flex items-center gap-3.5 mb-8">
          <span className="font-mono-nums text-[10px] tracking-[0.16em] text-ink-500 whitespace-nowrap">
            {done ? 'DONE' : 'RUNNING'}
          </span>
          <div className="flex-1 h-0.5 bg-ink-600/30 rounded overflow-hidden relative">
            {done ? (
              <motion.div
                initial={{ width: '0%' }}
                animate={{ width: '100%' }}
                transition={{ duration: 0.6, ease: [0.4, 0.8, 0.3, 1] }}
                className="h-full bg-gradient-to-r from-ink-500 to-ink-50 rounded"
              />
            ) : (
              <motion.div
                className="absolute inset-y-0 w-2/5 bg-gradient-to-r from-transparent via-ink-200 to-transparent"
                animate={{ x: ['-100%', '250%'] }}
                transition={{ duration: 1.3, repeat: Infinity, ease: 'linear' }}
              />
            )}
          </div>
        </div>

        {/* phase stepper */}
        <div className="flex items-center gap-0 mb-10">
          {STAGES.map((label, i) => (
            <div key={label} className="flex items-center flex-1">
              <motion.span
                className={`w-2 h-2 rounded-full shrink-0 mr-2.5 ${done ? 'bg-ink-100' : 'bg-ink-500'}`}
                animate={done ? { opacity: 1, scale: [0.4, 1.2, 1] } : { opacity: [0.3, 1, 0.3] }}
                transition={done ? { delay: i * 0.14, duration: 0.4 } : { duration: 1.4, repeat: Infinity, delay: i * 0.18 }}
              />
              <span
                className={`font-mono-nums text-[11px] tracking-[0.1em] uppercase whitespace-nowrap ${
                  done ? 'text-ink-100' : 'text-ink-400'
                }`}
              >
                {label}
              </span>
              {i < STAGES.length - 1 && (
                <span className={`flex-1 h-px mx-3.5 ${done ? 'bg-ink-600/60' : 'bg-ink-700/40'}`} />
              )}
            </div>
          ))}
        </div>

        {done ? <RevealBody record={record!} /> : <WorkingBody />}
        {/* clears the fixed Continue bar so the last claim card is never hidden behind it */}
        {done && <div className="h-24" aria-hidden />}
      </main>

      {/* ===== RECEIPTS RAIL ===== */}
      <aside className="border-t lg:border-t-0 lg:border-l border-ink-700/40 bg-gradient-to-b from-ink-900 to-ink-950 px-7 py-12">
        <div className="font-mono-nums text-[11px] tracking-[0.22em] text-ink-400 mb-7">RECEIPTS</div>
        {done ? <RealReceipts record={record!} /> : <PendingReceipts />}
        <div className="mt-10 font-mono-nums text-[10px] leading-relaxed text-ink-600">
          {done
            ? 'telemetry from this run · verdicts machine-decided · prose model-phrased, guard-checked'
            : 'no numbers are shown until the run returns them — the receipts below are measured, never estimated'}
        </div>
      </aside>
    </div>

    {/* THE ONLY WAY FORWARD, pinned to the viewport so a completed run announces "your move"
        without a scroll. Appears only on completion; the replay above stays scrollable under
        it. Constrained to the main column on large screens so it never covers the rail. */}
    {done && (
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.2, duration: 0.4 }}
        className="fixed bottom-0 left-0 right-0 lg:right-[340px] z-20 border-t border-ink-700/40 bg-ink-950/90 backdrop-blur-md"
      >
        <div className="px-6 lg:px-14 py-4 flex items-center gap-4">
          <button onClick={onContinue} className="btn-primary text-sm">
            Continue to results
            <ArrowRight size={16} />
          </button>
          <span className="font-mono-nums text-[11px] text-ink-500">
            run complete · nothing has reached your catalog — the checkpoint is on the next screen
          </span>
        </div>
      </motion.div>
    )}
    </>
  );
}

// ---- indeterminate (POST in flight) ----------------------------------------

function WorkingBody() {
  return (
    <div className="space-y-4 max-w-[720px]">
      <div className="font-mono-nums text-[12px] tracking-[0.16em] text-ink-200 mb-1">
        CHECKING CLAIMS AGAINST THE CATALOG
      </div>
      <p className="text-sm text-ink-400 leading-relaxed max-w-[62ch]">
        The pipeline sanitizes the agent's text, decomposes it into typed claims, resolves each
        entity in DataHub once, and decides every verdict with deterministic code. It is a single
        call — nothing here is streamed, so no count is shown until the run returns its own.
      </p>
      {/* a few skeleton claim shells with a travelling scan — shape, not content */}
      <div className="space-y-3 pt-4">
        {[0, 1, 2].map((i) => (
          <div
            key={i}
            className="rounded-2xl border border-ink-700/40 bg-ink-800/20 px-5 py-5 relative overflow-hidden"
          >
            <div className="flex items-center gap-3">
              <div className="w-7 h-7 rounded-lg bg-ink-700/50" />
              <div className="flex-1 space-y-2">
                <div className="h-3 rounded bg-ink-700/50" style={{ width: `${70 - i * 12}%` }} />
                <div className="h-2 rounded bg-ink-700/30 w-24" />
              </div>
            </div>
            <motion.div
              className="absolute inset-y-0 w-1/3 bg-gradient-to-r from-transparent via-ink-500/20 to-transparent"
              animate={{ x: ['-120%', '320%'] }}
              transition={{ duration: 1.6, repeat: Infinity, ease: 'linear', delay: i * 0.25 }}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

function PendingReceipts() {
  const rows = ['Claims', 'Verdicts', 'Tokens', 'Cost', 'Catalog lookups', 'Explanations'];
  return (
    <div className="flex flex-col gap-5">
      {rows.map((r) => (
        <div key={r} className="flex items-baseline justify-between">
          <span className="font-mono-nums text-[10px] tracking-[0.16em] text-ink-500">{r.toUpperCase()}</span>
          <span className="font-mono-nums text-base text-ink-600">—</span>
        </div>
      ))}
    </div>
  );
}

// ---- reveal (record present) -----------------------------------------------

function RevealBody({ record }: { record: AuditRecord }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
      className="max-w-[760px]"
    >
      <div className="font-mono-nums text-[11px] tracking-[0.16em] text-ink-400 mb-5">
        HOW EACH VERDICT WAS REACHED
      </div>
      <div className="flex flex-col gap-3">
        {record.claims.map((c, i) => (
          <ClaimWalk key={c.index} claim={c} index={i} />
        ))}
      </div>
    </motion.div>
  );
}

// One claim, replayed as the pipeline WALK that reached its verdict: resolve → check →
// explain → faithfulness → (self-correct). Every node's colour and sublabel is a MEASURED
// fact from the record — the verdict, the explanation source, whether the guard let the prose
// ship, the correction outcome. Nothing here is a scripted beat; a run with no correction
// renders no self-correct node and no callout.
type WalkNode = { label: string; sub: string; kind: string; dot: string };

function verdictHex(v: Verdict): string {
  return v === 'Supported' ? '#5FB98D' : v === 'Contradicted' ? '#D96A64' : '#C2A265';
}

function walkNodes(c: ClaimRecord): WalkNode[] {
  const nodes: WalkNode[] = [
    { label: 'Resolve', kind: 'io', sub: 'catalog', dot: '#7B8390' },
    {
      label: 'Check',
      kind: 'deterministic',
      sub: c.verdict === 'Insufficient-Coverage' ? 'insufficient' : c.verdict.toLowerCase(),
      dot: verdictHex(c.verdict),
    },
    {
      label: 'Explain',
      kind: 'model',
      sub: c.explanation_source === 'model' ? 'model-authored' : 'safe template',
      dot: c.explanation_source === 'model' ? '#6E8FBF' : '#C2A265',
    },
    {
      label: 'Faithfulness',
      kind: 'deterministic',
      sub: c.faithful ? 'evidence-checked' : 'template fallback',
      dot: c.faithful ? '#5FB98D' : '#C2A265',
    },
  ];
  if (c.correction.outcome !== 'not-attempted') {
    const firm = CORRECTION[c.correction.outcome]?.tone === 'firm';
    nodes.push({
      label: 'Self-correct',
      kind: 'model + check',
      sub: c.correction.outcome,
      dot: firm ? '#D96A64' : c.correction.outcome === 'corrected' ? '#5FB98D' : '#C2A265',
    });
  }
  return nodes;
}

function ClaimWalk({ claim, index }: { claim: ClaimRecord; index: number }) {
  const nodes = walkNodes(claim);
  const base = 0.15 + index * 0.5; // claims replay top-to-bottom
  const span = Math.max(0.5, nodes.length * 0.16); // rail fills as nodes light up
  const meta = CORRECTION[claim.correction.outcome];

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: base, duration: 0.4 }}
      className="rounded-2xl border border-ink-700/40 bg-ink-800/25 px-5 py-5"
    >
      <div className="flex items-start justify-between gap-4 mb-5">
        <div className="flex items-start gap-3.5 min-w-0">
          <span className="font-mono-nums text-xs text-ink-500 w-6 shrink-0 pt-0.5">
            {String(index + 1).padStart(2, '0')}
          </span>
          <div className="min-w-0">
            <p className="text-sm text-ink-100 leading-snug break-words">{claim.raw_text}</p>
            <span className="font-mono-nums text-[10px] tracking-[0.1em] uppercase text-ink-500">
              {claim.claim_type}
            </span>
          </div>
        </div>
        <span
          className={`shrink-0 inline-flex items-center gap-2 font-mono-nums text-[11px] tracking-[0.04em] ${verdictText(claim.verdict)}`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${VERDICT_DOT[claim.verdict]}`} />
          {claim.verdict === 'Insufficient-Coverage' ? 'Insufficient coverage' : claim.verdict}
        </span>
      </div>

      {/* the walker rail — a fill line grows as the nodes light up left-to-right */}
      <div className="relative pt-0.5">
        <div className="absolute left-[7px] right-[7px] top-[7px] h-px bg-ink-700/50" />
        <motion.div
          initial={{ scaleX: 0 }}
          animate={{ scaleX: 1 }}
          transition={{ delay: base, duration: span, ease: 'linear' }}
          className="absolute left-[7px] top-[7px] h-px origin-left bg-ink-300"
          style={{ width: 'calc(100% - 14px)' }}
        />
        <div className="relative flex items-start justify-between gap-2">
          {nodes.map((n, ni) => (
            <motion.div
              key={n.label}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: base + ni * 0.16, duration: 0.32 }}
              className="flex flex-col items-start gap-1.5 flex-1 min-w-0"
            >
              <motion.span
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                transition={{ delay: base + ni * 0.16, type: 'spring', stiffness: 380, damping: 18 }}
                className="w-3.5 h-3.5 rounded-full shrink-0"
                style={{ background: n.dot, boxShadow: `0 0 10px -1px ${n.dot}` }}
              />
              <span className="text-[12.5px] text-ink-100 leading-none">{n.label}</span>
              <span className="font-mono-nums text-[10px] tracking-[0.04em] text-ink-400 truncate max-w-full">
                {n.sub}
              </span>
              <span className="font-mono-nums text-[9px] tracking-[0.08em] uppercase text-ink-600">
                {n.kind}
              </span>
            </motion.div>
          ))}
        </div>
      </div>

      {/* the prominent correction callout, from the REAL outcome — loud for stood-firm/refused */}
      {meta && <CorrectionReveal claim={claim} delay={base + span + 0.1} />}
    </motion.div>
  );
}

function CorrectionReveal({ claim, delay = 0.3 }: { claim: ClaimRecord; delay?: number }) {
  const meta = CORRECTION[claim.correction.outcome];
  if (!meta) return null;
  const firm = meta.tone === 'firm';
  const attempts = claim.correction.attempts;
  const rc = attempts.length ? attempts[attempts.length - 1].revised_claim : null;
  const revised = rc && typeof rc.raw_text === 'string' ? rc.raw_text : null;
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ delay, duration: 0.4 }}
      className={`mt-5 rounded-xl border p-4 ${
        firm ? 'border-contradicted/40 bg-contradicted/[0.06]' : 'border-insufficient/30 bg-insufficient/[0.05]'
      }`}
    >
      <div className="flex items-center gap-2.5 mb-1.5">
        {firm && (
          <motion.span
            className="w-2 h-2 rounded-full bg-contradicted"
            animate={{ boxShadow: ['0 0 0 0 rgba(217,106,100,0.5)', '0 0 0 8px rgba(217,106,100,0)'] }}
            transition={{ duration: 1.8, repeat: Infinity }}
          />
        )}
        <span className={`font-mono-nums text-[11px] tracking-[0.1em] uppercase ${firm ? 'text-contradicted' : 'text-insufficient'}`}>
          {meta.label}
        </span>
        <span className="font-mono-nums text-[11px] text-ink-500 ml-auto">
          {claim.correction.outcome}
        </span>
      </div>
      <p className="text-xs text-ink-300 leading-relaxed max-w-[62ch]">
        {firm ? (
          <>
            The agent was shown the catalog and did not produce an honest revision. The verdict
            stands at <span className="text-contradicted">Contradicted</span> — a subject-swap is
            treated as a deceptive correction, and the loop fails closed.
          </>
        ) : (
          <>The agent revised the claim and it re-verified against the same snapshot.</>
        )}
      </p>
      {revised && (
        <div className="mt-2 font-mono-nums text-[11px] text-ink-400 break-words">
          revised → <span className="text-ink-200">{revised}</span>
        </div>
      )}
    </motion.div>
  );
}

function RealReceipts({ record }: { record: AuditRecord }) {
  const r = record.receipts;
  const counts = verdictCounts(record);
  const tokens = r.input_tokens + r.output_tokens;
  const modelAuthored = record.claims.filter((c) => c.explanation_source === 'model').length;
  const template = record.claims.length - modelAuthored;
  const cached = Math.max(0, r.catalog_lookups - r.catalog_fetches);

  return (
    <div className="flex flex-col gap-6">
      <div>
        <RailLabel>Claims done</RailLabel>
        <div className="font-mono-nums text-[26px] text-ink-50">
          <CountUp value={record.claims.length} />
          <span className="text-ink-600 text-lg"> / {record.claims.length}</span>
        </div>
      </div>

      <div>
        <RailLabel>Verdicts</RailLabel>
        <div className="flex gap-4 font-mono-nums text-sm">
          <VerdictTally color="bg-supported" n={counts.Supported} />
          <VerdictTally color="bg-contradicted" n={counts.Contradicted} />
          <VerdictTally color="bg-insufficient" n={counts['Insufficient-Coverage']} />
        </div>
      </div>

      <div className="h-px bg-ink-700/40" />

      <RailStat label="Tokens" value={tokens.toLocaleString()} />
      {/* usd: null renders "unknown", NEVER $0 — the None-is-not-zero invariant (cost.py). */}
      <RailStat label="Cost" value={r.usd === null ? 'unknown' : `$${r.usd.toFixed(4)}`} />
      <RailStat label="Elapsed" value={elapsed(r.latency_ms)} />

      <div className="h-px bg-ink-700/40" />

      <div>
        <div className="flex items-baseline justify-between mb-1.5">
          <RailLabel>Catalog lookups</RailLabel>
          <span className="font-mono-nums text-base text-ink-50">
            <CountUp value={r.catalog_lookups} />
          </span>
        </div>
        <div className="font-mono-nums text-[11px] text-ink-400">
          {r.catalog_fetches} fetch · {cached} cached
        </div>
      </div>

      <div>
        <div className="flex items-baseline justify-between mb-1.5">
          <RailLabel>Explanations</RailLabel>
          <span className="font-mono-nums text-sm text-ink-50">
            {record.claims.length ? Math.round((modelAuthored / record.claims.length) * 100) : 0}% model
          </span>
        </div>
        <div className="font-mono-nums text-[11px] text-ink-400">
          {modelAuthored} model · {template} template
        </div>
      </div>
    </div>
  );
}

function VerdictTally({ color, n }: { color: string; n: number }) {
  return (
    <span className="flex items-center gap-1.5 text-ink-200">
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      <CountUp value={n} />
    </span>
  );
}

function RailStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <RailLabel>{label}</RailLabel>
      <span className="font-mono-nums text-base text-ink-50">{value}</span>
    </div>
  );
}

function RailLabel({ children }: { children: ReactNode }) {
  return (
    <span className="font-mono-nums text-[10px] tracking-[0.16em] text-ink-500 uppercase">
      {children}
    </span>
  );
}

function verdictText(v: Verdict): string {
  return v === 'Supported'
    ? 'text-supported'
    : v === 'Contradicted'
      ? 'text-contradicted'
      : 'text-insufficient';
}

function elapsed(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}
