import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { ArrowRight } from 'lucide-react';
import type { AuditRecord, ClaimRecord, CorrectionOutcome, StepView, Verdict } from '../api/types';
import AttestMark from './AttestMark';

// THE AUDIT-IN-PROGRESS SCREEN. The backend is a SINGLE BLOCKING POST /audit — no streaming,
// no per-step event — so the screen is in two honest phases:
//
//   1. while `record === null` (POST in flight): an INDETERMINATE animation that shows the
//      pipeline's SHAPE and asserts ZERO numbers. The only live number is the wall clock.
//
//   2. when `record` arrives: a PROGRESSIVE REPLAY of the finished run, animated FORWARD on an
//      honest clock. This is the smoothness lever: the replay's beats are sized PROPORTIONAL
//      to the run's real per-node latency (record.steps), scaled into a watchable ~6-8s
//      window. A model call (decompose, explain, revise) dwells; a millisecond deterministic
//      step flashes past. So the smooth progression is not a designer's guess at pacing — it
//      is a time-compressed but proportionally-faithful replay of how the run actually ran.
//      If a step has no latency (e.g. a resumed run), that beat falls back to an even slice;
//      no latency is ever invented, and no invented number is ever shown. The stepper
//      advances, the receipts count up to their REAL finals, and each claim resolves in turn
//      with its walker lighting node-by-node. `usd` renders "unknown" when null, NEVER $0. The
//      replay HOLDS on completion until the human clicks Continue (no auto-advance).

const STAGES = ['Sanitize', 'Decompose', 'Per-claim loop', 'Sign-off'];

const VERDICT_DOT: Record<Verdict, string> = {
  Supported: 'bg-supported',
  Contradicted: 'bg-contradicted',
  'Insufficient-Coverage': 'bg-insufficient',
};

const CORRECTION: Record<CorrectionOutcome, { label: string; tone: 'firm' | 'proposal' } | null> = {
  corrected: { label: 'Correction proposed', tone: 'proposal' },
  'stood-firm': { label: 'Stood firm · correction refused', tone: 'firm' },
  refused: { label: 'Declined to revise', tone: 'firm' },
  exhausted: { label: 'Retry limit reached', tone: 'firm' },
  'not-corrected': { label: 'Not corrected', tone: 'firm' },
  'not-attempted': null,
};

// ---- the honest clock: real per-node latency -> proportional replay beats ----

type NodeKey = 'resolve' | 'check' | 'explain' | 'guard' | 'correct';

// Which walker node a real pipeline step belongs to. Names are from graph.py (resolve_entity,
// check_*, explain, faithfulness_guard, revise, recheck). recheck is the re-verify inside the
// correction loop, so it folds into Self-correct.
function nodeKeyForStep(name: string): NodeKey | null {
  if (name.startsWith('resolve')) return 'resolve';
  if (name === 'revise' || name === 'recheck') return 'correct';
  if (name.startsWith('check')) return 'check';
  if (name === 'explain') return 'explain';
  if (name.startsWith('faithfulness')) return 'guard';
  return null;
}

type Beat = { seg: 'sanitize' | 'decompose' | 'node'; claim: number; node: NodeKey | null; startF: number; endF: number };

function buildBeats(record: AuditRecord): Beat[] {
  const steps = record.steps;
  const latOf = (pred: (s: StepView) => boolean): number | null => {
    const hit = steps.filter(pred);
    return hit.length ? hit.reduce((sum, s) => sum + s.latency_ms, 0) : null;
  };
  type Seg = { seg: Beat['seg']; claim: number; node: NodeKey | null; lat: number | null };
  const segs: Seg[] = [
    { seg: 'sanitize', claim: -1, node: null, lat: latOf((s) => s.claim_index === null && s.name === 'sanitize') },
    { seg: 'decompose', claim: -1, node: null, lat: latOf((s) => s.claim_index === null && s.name === 'decompose') },
  ];
  for (const c of record.claims) {
    for (const n of walkNodes(c)) {
      segs.push({
        seg: 'node',
        claim: c.index,
        node: n.key,
        lat: latOf((s) => s.claim_index === c.index && nodeKeyForStep(s.name) === n.key),
      });
    }
  }
  // FALLBACK for a missing/null latency (e.g. a resumed run): give that beat the MEAN of the
  // known latencies — an even, typical slice — or equal weights if none are known at all.
  // This paces the animation only; it is never a displayed number, so no latency is invented.
  const known = segs.map((s) => s.lat).filter((v): v is number => v !== null && v > 0);
  const mean = known.length ? known.reduce((a, b) => a + b, 0) / known.length : 1;
  const weights = segs.map((s) => (s.lat !== null && s.lat > 0 ? s.lat : mean));
  const total = weights.reduce((a, b) => a + b, 0) || segs.length;
  let acc = 0;
  return segs.map((s, i) => {
    const startF = acc / total;
    acc += weights[i];
    return { seg: s.seg, claim: s.claim, node: s.node, startF, endF: acc / total };
  });
}

type ClaimReplay = { resolved: boolean; active: boolean; fill: number; lit: Set<NodeKey> };
type Replay = {
  stageIndex: number;
  claims: ClaimReplay[];
  receipts: {
    claimsDone: number;
    total: number;
    supported: number;
    contradicted: number;
    insufficient: number;
    tokens: number;
    lookups: number;
    fetches: number;
    cached: number;
    cost: string;
    elapsed: string;
    model: number;
    template: number;
    modelRate: number;
  };
};

function deriveReplay(record: AuditRecord, beats: Beat[], t: number): Replay {
  const e = 1 - Math.pow(1 - t, 3); // easeOutCubic — the counting-up aggregates settle smoothly

  let stageIndex: number;
  if (t >= 1 || !beats.length) stageIndex = 3;
  else {
    const b = beats.find((bt) => t < bt.endF) ?? beats[beats.length - 1];
    stageIndex = b.seg === 'sanitize' ? 0 : b.seg === 'decompose' ? 1 : 2;
  }

  const claims: ClaimReplay[] = record.claims.map((c) => {
    const nb = beats.filter((b) => b.claim === c.index);
    const keys = walkNodes(c).map((n) => n.key);
    if (!nb.length) {
      const isDone = t >= 1;
      return { resolved: isDone, active: false, fill: isDone ? 1 : 0, lit: new Set(isDone ? keys : []) };
    }
    const firstStart = nb[0].startF;
    const lastEnd = nb[nb.length - 1].endF;
    const resolved = t >= lastEnd;
    const active = t >= firstStart && t < lastEnd;
    const fill = resolved ? 1 : active ? (t - firstStart) / Math.max(1e-6, lastEnd - firstStart) : 0;
    const lit = new Set<NodeKey>(
      resolved ? keys : (nb.filter((b) => t >= b.startF).map((b) => b.node) as NodeKey[]),
    );
    return { resolved, active, fill, lit };
  });

  const resolved = record.claims.filter((_, i) => claims[i].resolved);
  const by = (v: Verdict) => resolved.filter((c) => c.verdict === v).length;
  const model = resolved.filter((c) => c.explanation_source === 'model').length;
  const claimsDone = resolved.length;
  const r = record.receipts;
  const lookups = Math.round(r.catalog_lookups * e);
  const fetches = Math.round(r.catalog_fetches * e);

  return {
    stageIndex,
    claims,
    receipts: {
      claimsDone,
      total: record.claims.length,
      supported: by('Supported'),
      contradicted: by('Contradicted'),
      insufficient: by('Insufficient-Coverage'),
      tokens: Math.round((r.input_tokens + r.output_tokens) * e),
      lookups,
      fetches,
      cached: Math.max(0, lookups - fetches),
      // usd null -> "unknown", NEVER $0 (None-is-not-zero, cost.py). Read from record only.
      cost: r.usd === null ? 'unknown' : `$${(r.usd * e).toFixed(4)}`,
      // the REAL measured duration, shown whole — the replay window is NOT claimed as the run
      // time. It counts nothing up; it is the fact of how long the run took.
      elapsed: `${(r.latency_ms / 1000).toFixed(1)}s`,
      model,
      template: claimsDone - model,
      modelRate: claimsDone ? Math.round((model / claimsDone) * 100) : 0,
    },
  };
}

export default function AuditProgress({
  record,
  onContinue,
}: {
  record: AuditRecord | null;
  onContinue: () => void;
}) {
  const reduced = useReducedMotion();
  const done = record !== null;

  // In-flight wall clock (a real number). Frozen once the record arrives.
  const [wall, setWall] = useState(0);
  useEffect(() => {
    if (done) return;
    const start = performance.now();
    const iv = setInterval(() => setWall(performance.now() - start), 100);
    return () => clearInterval(iv);
  }, [done]);

  // The replay clock: 0..1 over a watchable window, advancing in REAL time. Because the beats
  // it moves through are sized proportional to real latency, dwell time in each beat is
  // proportional to how long that step actually took. Reduced-motion jumps straight to 1.
  const [t, setT] = useState(0);
  useEffect(() => {
    if (!record) {
      setT(0);
      return;
    }
    if (reduced) {
      setT(1);
      return;
    }
    const windowMs = Math.min(8000, 5500 + record.claims.length * 800);
    let raf = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const nt = Math.min(1, (now - start) / windowMs);
      setT(nt);
      if (nt < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [record, reduced]);

  const beats = useMemo(() => (record ? buildBeats(record) : []), [record]);
  const replay = useMemo(() => (record ? deriveReplay(record, beats, t) : null), [record, beats, t]);

  return (
    <>
      <div
        className="min-h-screen grid grid-cols-1 lg:grid-cols-[1fr_340px]"
        style={{ background: 'radial-gradient(130% 100% at 30% 0%, #101216 0%, #0A0B0D 55%, #08090B 100%)' }}
      >
        {/* ===== MAIN ===== */}
        <main className="px-6 lg:px-14 py-12 lg:py-14 overflow-hidden">
          <header className="flex items-baseline justify-between mb-8">
            <div className="flex items-center gap-3.5">
              {/* mark + wordmark are ONE lockup — tighter gap than the rule beside it. */}
              <div className="flex items-center gap-2">
                <AttestMark size={20} className="text-ink-100" />
                <span className="font-serif text-[25px] font-semibold tracking-tight">Attest</span>
              </div>
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

          {/* progress bar — indeterminate while working, driven by the replay clock when done */}
          <div className="flex items-center gap-3.5 mb-8">
            <span className="font-mono-nums text-[10px] tracking-[0.16em] text-ink-500 whitespace-nowrap">
              {done ? 'REPLAY' : 'RUNNING'}
            </span>
            <div className="flex-1 h-0.5 bg-ink-600/30 rounded overflow-hidden relative">
              {done ? (
                <div
                  className="h-full bg-gradient-to-r from-ink-500 to-ink-50 rounded"
                  style={{ width: `${(replay ? Math.max(0.02, replayT(replay)) : 1) * 100}%` }}
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

          {/* phase stepper — advances Sanitize -> Decompose -> Per-claim -> Sign-off */}
          <Stepper done={done} stageIndex={replay?.stageIndex ?? 0} />

          <AnimatePresence mode="wait">
            {done && replay ? (
              <motion.div
                key="reveal"
                initial={reduced ? false : { opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.35 }}
              >
                <RevealBody record={record!} replay={replay} />
                {/* clears the fixed Continue bar so the last claim card is never hidden */}
                <div className="h-24" aria-hidden />
              </motion.div>
            ) : (
              <motion.div key="working" initial={false} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.35 }}>
                <WorkingBody />
              </motion.div>
            )}
          </AnimatePresence>
        </main>

        {/* ===== RECEIPTS RAIL ===== */}
        <aside className="border-t lg:border-t-0 lg:border-l border-ink-700/40 bg-gradient-to-b from-ink-900 to-ink-950 px-7 py-12">
          <div className="font-mono-nums text-[11px] tracking-[0.22em] text-ink-400 mb-7">RECEIPTS</div>
          <AnimatePresence mode="wait">
            {done && replay ? (
              <motion.div key="real" initial={reduced ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.35 }}>
                <RealReceipts r={replay.receipts} />
              </motion.div>
            ) : (
              <motion.div key="pending" initial={false} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.35 }}>
                <PendingReceipts />
              </motion.div>
            )}
          </AnimatePresence>
          <div className="mt-10 font-mono-nums text-[10px] leading-relaxed text-ink-600">
            {done
              ? 'telemetry from this run · paced by real per-step latency · verdicts machine-decided'
              : 'no numbers are shown until the run returns them — the receipts below are measured, never estimated'}
          </div>
        </aside>
      </div>

      {/* THE ONLY WAY FORWARD, pinned to the viewport so a completed run announces "your move"
          without a scroll. Appears only on completion; the replay above stays scrollable. */}
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

// The replay clock's progress, recovered from the last claim's fill for the top bar. When all
// claims are resolved the bar is full; otherwise it tracks how far the replay has advanced.
function replayT(replay: Replay): number {
  const done = replay.claims.filter((c) => c.resolved).length;
  if (done === replay.claims.length) return 1;
  const active = replay.claims.find((c) => c.active);
  const per = 1 / Math.max(1, replay.claims.length);
  return done * per + (active ? active.fill * per : 0);
}

// ---- the phase stepper -----------------------------------------------------

function Stepper({ done, stageIndex }: { done: boolean; stageIndex: number }) {
  return (
    <div className="flex items-center gap-0 mb-10">
      {STAGES.map((label, i) => {
        const state = !done ? 'flight' : i < stageIndex ? 'done' : i === stageIndex ? 'active' : 'pending';
        const dotColor =
          state === 'flight' ? '#3A4049' : state === 'active' ? '#EDEFF2' : state === 'done' ? '#D2D6DC' : '#565D67';
        const textColor =
          state === 'pending' ? 'text-ink-500' : state === 'flight' ? 'text-ink-400' : 'text-ink-100';
        return (
          <div key={label} className="flex items-center flex-1">
            <motion.span
              className="w-2 h-2 rounded-full shrink-0 mr-2.5"
              initial={false}
              animate={
                state === 'flight'
                  ? { opacity: [0.3, 1, 0.3], scale: 1, backgroundColor: dotColor }
                  : state === 'active'
                    ? { opacity: 1, scale: [1, 1.25, 1], backgroundColor: dotColor }
                    : { opacity: 1, scale: 1, backgroundColor: dotColor }
              }
              transition={
                state === 'flight'
                  ? { duration: 1.4, repeat: Infinity, delay: i * 0.18 }
                  : state === 'active'
                    ? { scale: { duration: 1.2, repeat: Infinity, ease: 'easeInOut' }, backgroundColor: { duration: 0.4 } }
                    : { duration: 0.4, ease: [0.22, 1, 0.36, 1] }
              }
            />
            <span className={`font-mono-nums text-[11px] tracking-[0.1em] uppercase whitespace-nowrap transition-colors duration-300 ${textColor}`}>
              {label}
            </span>
            {i < STAGES.length - 1 && (
              <div className="flex-1 h-px mx-3.5 bg-ink-700/40 relative overflow-hidden">
                <motion.div
                  className="absolute inset-0 origin-left bg-ink-500"
                  initial={false}
                  animate={{ scaleX: done && i < stageIndex ? 1 : 0 }}
                  transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
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
      <div className="space-y-3 pt-4">
        {[0, 1, 2].map((i) => (
          <div key={i} className="rounded-2xl border border-ink-700/40 bg-ink-800/20 px-5 py-5 relative overflow-hidden">
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

function RevealBody({ record, replay }: { record: AuditRecord; replay: Replay }) {
  return (
    <div className="max-w-[760px]">
      <div className="font-mono-nums text-[11px] tracking-[0.16em] text-ink-400 mb-5">
        HOW EACH VERDICT WAS REACHED
      </div>
      <div className="flex flex-col gap-3">
        {record.claims.map((c, i) => (
          <ClaimWalk key={c.index} claim={c} state={replay.claims[i]} index={i} />
        ))}
      </div>
    </div>
  );
}

type WalkNode = { key: NodeKey; label: string; sub: string; kind: string; dot: string };

function verdictHex(v: Verdict): string {
  return v === 'Supported' ? '#5FB98D' : v === 'Contradicted' ? '#D96A64' : '#C2A265';
}

function walkNodes(c: ClaimRecord): WalkNode[] {
  const nodes: WalkNode[] = [
    { key: 'resolve', label: 'Resolve', kind: 'io', sub: 'catalog', dot: '#7B8390' },
    {
      key: 'check',
      label: 'Check',
      kind: 'deterministic',
      sub: c.verdict === 'Insufficient-Coverage' ? 'insufficient' : c.verdict.toLowerCase(),
      dot: verdictHex(c.verdict),
    },
    {
      key: 'explain',
      label: 'Explain',
      kind: 'model',
      sub: c.explanation_source === 'model' ? 'model-authored' : 'safe template',
      dot: c.explanation_source === 'model' ? '#6E8FBF' : '#C2A265',
    },
    {
      key: 'guard',
      label: 'Faithfulness',
      kind: 'deterministic',
      sub: c.faithful ? 'evidence-checked' : 'template fallback',
      dot: c.faithful ? '#5FB98D' : '#C2A265',
    },
  ];
  if (c.correction.outcome !== 'not-attempted') {
    const firm = CORRECTION[c.correction.outcome]?.tone === 'firm';
    nodes.push({
      key: 'correct',
      label: 'Self-correct',
      kind: 'model + check',
      sub: c.correction.outcome,
      dot: firm ? '#D96A64' : c.correction.outcome === 'corrected' ? '#5FB98D' : '#C2A265',
    });
  }
  return nodes;
}

function ClaimWalk({ claim, state, index }: { claim: ClaimRecord; state: ClaimReplay; index: number }) {
  const nodes = walkNodes(claim);
  const shown = state.active || state.resolved;
  const checkLit = state.resolved || state.lit.has('check');
  const correctLit = state.resolved || state.lit.has('correct');
  const meta = CORRECTION[claim.correction.outcome];

  return (
    <motion.div
      initial={false}
      animate={{ opacity: shown ? 1 : 0.4, borderColor: state.active ? 'rgba(110,143,191,0.35)' : 'rgba(150,158,168,0.14)' }}
      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
      className="rounded-2xl border bg-ink-800/25 px-5 py-5"
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
        {/* the verdict is revealed when the Check node lights — that is when the deterministic
            checker decided it in the replay. Before that, "reviewing". */}
        {checkLit ? (
          <motion.span
            initial={false}
            className={`shrink-0 inline-flex items-center gap-2 font-mono-nums text-[11px] tracking-[0.04em] ${verdictText(claim.verdict)}`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${VERDICT_DOT[claim.verdict]}`} />
            {claim.verdict === 'Insufficient-Coverage' ? 'Insufficient coverage' : claim.verdict}
          </motion.span>
        ) : (
          <span className="shrink-0 font-mono-nums text-[11px] text-ink-500">reviewing…</span>
        )}
      </div>

      {/* the walker rail — the fill grows with the replay, nodes light node-by-node */}
      <div className="relative pt-0.5">
        <div className="absolute left-[7px] right-[7px] top-[7px] h-px bg-ink-700/50" />
        <div
          className="absolute left-[7px] top-[7px] h-px origin-left bg-ink-300"
          style={{ width: 'calc(100% - 14px)', transform: `scaleX(${state.fill})` }}
        />
        <div className="relative flex items-start justify-between gap-2">
          {nodes.map((n) => {
            const lit = state.resolved || state.lit.has(n.key);
            return (
              <motion.div
                key={n.key}
                initial={false}
                animate={{ opacity: lit ? 1 : 0.32 }}
                transition={{ duration: 0.3 }}
                className="flex flex-col items-start gap-1.5 flex-1 min-w-0"
              >
                <motion.span
                  initial={false}
                  animate={{ scale: lit ? 1 : 0.001, opacity: lit ? 1 : 0 }}
                  transition={{ type: 'spring', stiffness: 380, damping: 20 }}
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
            );
          })}
        </div>
      </div>

      {/* the correction callout appears when the Self-correct node lights — real outcome only */}
      <AnimatePresence>{meta && correctLit && <CorrectionReveal claim={claim} />}</AnimatePresence>
    </motion.div>
  );
}

function CorrectionReveal({ claim }: { claim: ClaimRecord }) {
  const meta = CORRECTION[claim.correction.outcome];
  if (!meta) return null;
  const firm = meta.tone === 'firm';
  const attempts = claim.correction.attempts;
  const rc = attempts.length ? attempts[attempts.length - 1].revised_claim : null;
  const revised = rc && typeof rc.raw_text === 'string' ? rc.raw_text : null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.4 }}
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
        <span className="font-mono-nums text-[11px] text-ink-500 ml-auto">{claim.correction.outcome}</span>
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

function RealReceipts({ r }: { r: Replay['receipts'] }) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <RailLabel>Claims done</RailLabel>
        <div className="font-mono-nums text-[26px] text-ink-50">
          {r.claimsDone}
          <span className="text-ink-600 text-lg"> / {r.total}</span>
        </div>
      </div>

      <div>
        <RailLabel>Verdicts</RailLabel>
        <div className="flex gap-4 font-mono-nums text-sm">
          <VerdictTally color="bg-supported" n={r.supported} />
          <VerdictTally color="bg-contradicted" n={r.contradicted} />
          <VerdictTally color="bg-insufficient" n={r.insufficient} />
        </div>
      </div>

      <div className="h-px bg-ink-700/40" />

      <RailStat label="Tokens" value={r.tokens.toLocaleString()} />
      <RailStat label="Cost" value={r.cost} />
      <RailStat label="Elapsed" value={r.elapsed} />

      <div className="h-px bg-ink-700/40" />

      <div>
        <div className="flex items-baseline justify-between mb-1.5">
          <RailLabel>Catalog lookups</RailLabel>
          <span className="font-mono-nums text-base text-ink-50">{r.lookups}</span>
        </div>
        <div className="font-mono-nums text-[11px] text-ink-400">
          {r.fetches} fetch · {r.cached} cached
        </div>
      </div>

      <div>
        <div className="flex items-baseline justify-between mb-1.5">
          <RailLabel>Explanations</RailLabel>
          <span className="font-mono-nums text-sm text-ink-50">{r.modelRate}% model</span>
        </div>
        <div className="font-mono-nums text-[11px] text-ink-400">
          {r.model} model · {r.template} template
        </div>
      </div>
    </div>
  );
}

function VerdictTally({ color, n }: { color: string; n: number }) {
  return (
    <span className="flex items-center gap-1.5 text-ink-200">
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      {n}
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
  return <span className="font-mono-nums text-[10px] tracking-[0.16em] text-ink-500 uppercase">{children}</span>;
}

function verdictText(v: Verdict): string {
  return v === 'Supported' ? 'text-supported' : v === 'Contradicted' ? 'text-contradicted' : 'text-insufficient';
}

function elapsed(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}
