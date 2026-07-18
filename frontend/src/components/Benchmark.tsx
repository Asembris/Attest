import type { ReactNode } from 'react';
import { motion } from 'framer-motion';
import { ArrowLeft, ShieldCheck } from 'lucide-react';
import {
  benchmarkMeta,
  perVerdict,
  headline,
  confusion,
  calibrationMeta,
  calibrationRuns,
} from '../data/benchmarkData';
import { Reveal, CountUp } from './reveal';

// The benchmark page, re-typeset to the design pass. EVERY number here is read from
// benchmarkData.ts, which mirrors the committed receipts (benchmark/results/*.json +
// README.md). The design-pass mockup shipped FABRICATED placeholders for these figures
// (hard:14 vs the real 26, cost 0.0518 vs 0.0138309, support 16/12/12 vs 15/17/8, a wrong
// labeler id) — its own author flagged them "PLACEHOLDER … confirm against the data". They
// are deleted; the layout is kept because the determinism-contrast framing was always right.
// The sharpest argument in the project is that a cross-family judge answers its own question
// differently at temperature 0, while the deterministic core does not.

const VERDICT_HEX: Record<string, string> = {
  Supported: '#5FB98D',
  Contradicted: '#D96A64',
  'Insufficient-Coverage': '#C2A265',
};
const COLS: Array<keyof typeof VERDICT_HEX> = [
  'Supported',
  'Contradicted',
  'Insufficient-Coverage',
];

function pct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}
function f3(n: number): string {
  return n.toFixed(3);
}
function short(v: string): string {
  return v === 'Insufficient-Coverage' ? 'Insufficient' : v;
}
function disputedLabel(disputed: string[]): string {
  return disputed.length ? `{ ${disputed.join(', ')} }` : '∅';
}

const totalSupport = perVerdict.reduce((s, v) => s + v.support, 0);
const maxCell = Math.max(...confusion.flatMap((r) => r.predicted));

export default function Benchmark({ onBack }: { onBack: () => void }) {
  return (
    <div className="min-h-screen bg-ink-950">
      <header className="sticky top-0 z-30 border-b border-ink-700/40 bg-ink-950/80 backdrop-blur-md">
        <div className="max-w-[1120px] mx-auto px-6 lg:px-10 py-4 flex items-center gap-3.5">
          <button onClick={onBack} className="btn-ghost text-sm px-2">
            <ArrowLeft size={16} />
          </button>
          <span className="font-serif text-xl font-semibold tracking-tight">Attest</span>
          <span className="text-ink-500">/</span>
          <span className="text-sm text-ink-300">Evidence &amp; Benchmark</span>
          <span className="ml-auto flex items-center gap-2 font-mono-nums text-[11px] tracking-[0.14em] text-supported">
            <span className="w-1.5 h-1.5 rounded-full bg-supported shadow-[0_0_10px_#5FB98D]" />
            DETERMINISTIC CORE
          </span>
        </div>
      </header>

      <main className="max-w-[1120px] mx-auto px-6 lg:px-10 pb-32">
        {/* ===== OPENING ===== */}
        <section className="pt-24 pb-16">
          <Reveal>
            <div className="font-mono-nums text-[11px] tracking-[0.24em] uppercase text-ink-400 mb-6">
              Groundedness benchmark · committed receipts
            </div>
          </Reveal>
          <Reveal delay={0.08}>
            <h1 className="font-serif font-normal leading-[0.94] tracking-[0.005em] text-[clamp(52px,8vw,104px)] max-w-[14ch]">
              Evidence &amp; Benchmark
            </h1>
          </Reveal>
          <Reveal delay={0.16}>
            <p className="mt-7 max-w-[60ch] text-lg font-light leading-relaxed text-ink-300">
              {benchmarkMeta.nCases} hand-labeled groundedness claims about a DataHub catalog,{' '}
              {benchmarkMeta.hard} of them marked hard, spanning all {benchmarkMeta.cells} (claim
              type × verdict) cells. Every verdict is decided by{' '}
              <span className="text-ink-50">deterministic code — no model in the verification path</span>{' '}
              — and cross-checked by an independent model family.
            </p>
          </Reveal>
        </section>

        {/* ===== CENTERPIECE: DETERMINISM CONTRAST ===== */}
        <section className="py-16 border-t border-ink-700/40">
          <Reveal className="flex items-center gap-3 mb-8">
            <span className="w-2 h-2 rounded-full bg-supported shadow-[0_0_12px_#5FB98D]" />
            <span className="font-mono-nums text-[11px] tracking-[0.2em] uppercase text-supported">
              The determinism contrast
            </span>
          </Reveal>

          <blockquote className="max-w-[22ch] font-serif font-light leading-[1.06] tracking-[0.004em] text-[clamp(34px,5.4vw,68px)]">
            <Reveal>
              <span className="block">
                Across two runs re-labeling the same{' '}
                <span className="font-mono-nums text-[0.62em] align-[0.12em] text-ink-300">
                  {benchmarkMeta.nCases}
                </span>{' '}
                claims, an independent model family
              </span>
            </Reveal>
            <Reveal delay={0.2}>
              <span className="block text-contradicted">
                disputed a different case each time — and flipped a verdict at temperature 0.
              </span>
            </Reveal>
            <Reveal delay={0.4}>
              <span className="block">
                Attest&rsquo;s core returned the{' '}
                <span className="text-supported">
                  same{' '}
                  <span className="font-mono-nums text-[0.62em] align-[0.12em]">{totalSupport}</span>{' '}
                  verdicts every time.
                </span>
              </span>
            </Reveal>
          </blockquote>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-7 mt-16 items-stretch">
            {/* Attest core */}
            <Reveal x={-22} className="h-full">
              <div className="h-full rounded-[18px] border border-supported/25 p-7 bg-gradient-to-b from-supported/[0.06] to-supported/[0.015]">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-mono-nums text-[11px] tracking-[0.12em] text-supported">
                    ATTEST CORE
                  </span>
                  <span className="font-mono-nums text-[11px] text-ink-400">
                    pass@{headline.passAtKCore.k}
                  </span>
                </div>
                <div className="font-serif text-[74px] leading-none text-supported my-2">
                  <CountUp value={headline.passAtKCore.value * 100} suffix="%" />
                </div>
                <div className="text-sm text-ink-300 mb-6">
                  identical verdicts, every run · deterministic
                </div>
                <div className="flex flex-col gap-2">
                  {Array.from({ length: headline.passAtKCore.k }, (_, i) => (
                    <motion.div
                      key={i}
                      initial={{ opacity: 0, x: -14 }}
                      whileInView={{ opacity: 1, x: 0 }}
                      viewport={{ once: true }}
                      transition={{ delay: 0.2 + i * 0.08, duration: 0.4 }}
                      className="flex items-center gap-3 px-3.5 py-2.5 rounded-xl bg-supported/[0.06] border border-supported/15"
                    >
                      <span className="font-mono-nums text-[11px] tracking-[0.1em] text-ink-400 w-12">
                        Run {i + 1}
                      </span>
                      <span className="font-mono-nums text-sm text-supported">
                        {totalSupport}/{totalSupport} identical
                      </span>
                      <ShieldCheck size={13} className="text-supported ml-auto" />
                    </motion.div>
                  ))}
                </div>
              </div>
            </Reveal>

            {/* Cross-family judge */}
            <Reveal x={22} delay={0.12} className="h-full">
              <div className="h-full rounded-[18px] border border-contradicted/25 p-7 bg-gradient-to-b from-contradicted/[0.06] to-contradicted/[0.015]">
                <div className="flex items-center justify-between mb-1.5">
                  <span className="font-mono-nums text-[11px] tracking-[0.12em] text-contradicted">
                    {calibrationMeta.family.toUpperCase()} JUDGE
                  </span>
                  <span className="font-mono-nums text-[11px] text-ink-400">
                    temp {calibrationMeta.temperature}
                  </span>
                </div>
                <div className="font-serif text-[74px] leading-none text-contradicted my-2">
                  disagrees
                </div>
                <div className="text-sm text-ink-300 mb-6">
                  with itself — a different dispute each run
                </div>
                <div className="flex flex-col gap-2">
                  {calibrationRuns.map((r, i) => (
                    <motion.div
                      key={r.condition}
                      initial={{ opacity: 0, x: 14 }}
                      whileInView={{ opacity: 1, x: 0 }}
                      viewport={{ once: true }}
                      transition={{ delay: 0.2 + i * 0.08, duration: 0.4 }}
                      className="flex items-center gap-3 px-3.5 py-2.5 rounded-xl bg-ink-800/60 border border-ink-700/40"
                    >
                      <span className="font-mono-nums text-[11px] tracking-[0.1em] text-ink-400 w-12">
                        {i === 0 ? 'pre' : 'post'}
                      </span>
                      <span className="font-mono-nums text-xs text-ink-300 w-14">
                        {pct(r.agreement)}
                      </span>
                      <span className="font-mono-nums text-sm text-contradicted">
                        {disputedLabel(r.disputed)}
                      </span>
                      <span
                        className={`ml-auto font-mono-nums text-[11px] tracking-[0.06em] ${
                          r.nature === 'signal' ? 'text-supported' : 'text-ink-400'
                        }`}
                      >
                        {r.nature}
                      </span>
                    </motion.div>
                  ))}
                </div>
              </div>
            </Reveal>
          </div>

          <Reveal>
            <p className="mt-8 max-w-[76ch] text-sm leading-relaxed text-ink-300">
              Two committed runs, temperature 0. The disputed case is different each run —{' '}
              <span className="font-mono-nums text-ink-200">{'{class-15}'}</span> before a missing
              governance rule was declared,{' '}
              <span className="font-mono-nums text-ink-200">{'{class-03}'}</span> after — and{' '}
              <span className="text-ink-200">
                class-03 was a verdict the judge had agreed with on the earlier run before flipping
                it
              </span>
              . One dispute was signal (a real policy gap, now declared), one was noise (an unstable
              flip); neither was a stable disagreement about a label. Attest&rsquo;s checkers are date
              math and set membership; they return the same verdict every time, which is why the
              core&rsquo;s pass@{headline.passAtKCore.k} is 100%.{' '}
              <span className="text-ink-50">
                A judge that answers its own question differently cannot <em>be</em> ground truth —
                it can only calibrate it.
              </span>
            </p>
          </Reveal>
        </section>

        {/* ===== CONFUSION MATRIX ===== */}
        <section className="py-16 border-t border-ink-700/40">
          <div className="flex items-end justify-between flex-wrap gap-5 mb-11">
            <div>
              <Reveal className="font-mono-nums text-[11px] tracking-[0.2em] uppercase text-ink-400 mb-3.5">
                Confusion matrix
              </Reveal>
              <Reveal delay={0.08}>
                <h2 className="font-serif font-normal leading-none text-[clamp(32px,4.2vw,52px)]">
                  Zero off the diagonal.
                </h2>
              </Reveal>
            </div>
            <Reveal delay={0.16} className="max-w-[34ch]">
              <p className="text-sm leading-relaxed text-ink-300">
                Actual vs. predicted verdict across {benchmarkMeta.nCases} claims. Every claim lands
                on the diagonal — the checker never confuses one verdict for another.
              </p>
            </Reveal>
          </div>

          <Reveal className="overflow-x-auto">
            <div
              className="grid gap-3 min-w-[560px] max-w-[760px]"
              style={{ gridTemplateColumns: '130px repeat(3, 1fr)' }}
            >
              <div />
              {COLS.map((c) => (
                <div key={c} className="flex items-center justify-center gap-2 pb-2">
                  <span className="w-2 h-2 rounded-full" style={{ background: VERDICT_HEX[c] }} />
                  <span className="font-mono-nums text-[11px] tracking-[0.06em] text-ink-300">
                    {short(c)}
                  </span>
                </div>
              ))}

              {confusion.map((row, ri) => (
                <MatrixRow key={row.actual} actual={row.actual} predicted={row.predicted} rowIndex={ri} />
              ))}
            </div>
          </Reveal>

          <Reveal className="flex justify-between max-w-[760px] mt-4 font-mono-nums text-[11px] tracking-[0.08em] text-ink-400">
            <span>← Predicted</span>
            <span>Actual ↓</span>
          </Reveal>
        </section>

        {/* ===== METRICS TABLE ===== */}
        <section className="py-16 border-t border-ink-700/40">
          <div className="flex items-baseline justify-between flex-wrap gap-4 mb-9">
            <Reveal>
              <h2 className="font-serif font-normal text-[clamp(30px,4vw,46px)]">Per-verdict metrics</h2>
            </Reveal>
            <Reveal delay={0.08} className="font-mono-nums text-sm text-ink-300">
              macro-F1 <span className="text-supported text-base">{f3(headline.macroF1)}</span>
            </Reveal>
          </div>

          <Reveal className="rounded-2xl border border-ink-700/40 overflow-hidden">
            <div
              className="grid px-6 py-3.5 border-b border-ink-700/40 font-mono-nums text-[10.5px] tracking-[0.12em] uppercase text-ink-400"
              style={{ gridTemplateColumns: '1.6fr 1fr 1fr 1fr 0.7fr' }}
            >
              <span>Verdict</span>
              <span className="text-right">Precision</span>
              <span className="text-right">Recall</span>
              <span className="text-right">F1</span>
              <span className="text-right">n</span>
            </div>
            {perVerdict.map((r, i) => (
              <motion.div
                key={r.verdict}
                initial={{ opacity: 0, x: -10 }}
                whileInView={{ opacity: 1, x: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.09, duration: 0.5 }}
                className="grid items-center px-6 py-4 border-b border-ink-700/20 last:border-0"
                style={{ gridTemplateColumns: '1.6fr 1fr 1fr 1fr 0.7fr' }}
              >
                <span className="flex items-center gap-2.5">
                  <span className="w-1.5 h-1.5 rounded-full" style={{ background: VERDICT_HEX[r.verdict] }} />
                  <span className="text-[15px]" style={{ color: VERDICT_HEX[r.verdict] }}>
                    {r.verdict}
                  </span>
                </span>
                <span className="text-right font-mono-nums text-sm text-ink-100">{f3(r.precision)}</span>
                <span className="text-right font-mono-nums text-sm text-ink-100">{f3(r.recall)}</span>
                <span className="text-right font-mono-nums text-sm text-ink-100">{f3(r.f1)}</span>
                <span className="text-right font-mono-nums text-sm text-ink-300">{r.support}</span>
              </motion.div>
            ))}
            <div className="grid grid-cols-3 gap-4 px-6 py-5 bg-ink-800/30">
              <Kpi label="Accuracy" value={pct(headline.accuracy)} />
              <Kpi label={`pass@${headline.passAtKCore.k} · core`} value={pct(headline.passAtKCore.value)} />
              <Kpi label={`pass@${headline.passAtKFull.k} · full`} value={pct(headline.passAtKFull.value)} />
            </div>
          </Reveal>
        </section>

        {/* ===== RECEIPTS STRIP ===== */}
        <Reveal className="py-9 border-t border-ink-700/40">
          <div className="flex flex-wrap gap-11 items-center">
            <div>
              <div className="font-mono-nums text-[10px] tracking-[0.14em] uppercase text-ink-400 mb-1.5">
                Full-pipeline cost
              </div>
              <div className="font-mono-nums text-lg text-ink-50">
                ${headline.costUsd.toFixed(4)}{' '}
                <span className="text-ink-400 text-[13px]">/ {headline.explanations} claims</span>
              </div>
            </div>
            <div className="w-px h-8 bg-ink-700/40" />
            <div>
              <div className="font-mono-nums text-[10px] tracking-[0.14em] uppercase text-ink-400 mb-1.5">
                Model-authored explanations
              </div>
              <div className="font-mono-nums text-lg text-ink-50">
                {headline.modelAuthored}/{headline.explanations}{' '}
                <span className="text-ink-400 text-[13px]">· {headline.guardRejected} guard rejections</span>
              </div>
            </div>
            <div className="ml-auto max-w-[24ch] text-right font-mono-nums text-[10.5px] tracking-[0.06em] text-ink-500 leading-relaxed">
              verdicts machine-decided · prose model-phrased, guard-checked
            </div>
          </div>
        </Reveal>

        {/* ===== METHODOLOGY ===== */}
        <section className="pt-14 pb-10 border-t border-ink-700/40">
          <Reveal>
            <h2 className="font-serif font-normal text-[clamp(30px,4vw,46px)] mb-9">Methodology</h2>
          </Reveal>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-x-14 gap-y-8">
            <Method title="Dataset">
              {benchmarkMeta.nCases} natural-language claims about a seeded catalog, hand-labeled with
              a one-line rationale each. Both the prose and the structured claim are carried, so a
              checker bug can be told from an extraction bug. All {benchmarkMeta.cells} cells are
              populated; a cell going dark is a silent misclassifier.
            </Method>
            <Method title="Attest core" delay={0.08}>
              claim → catalog lookup → rule-based match against ownership, freshness, PII signals and
              schema → deterministic verdict. No LLM in the verification path. Run against the prose (
              <span className="font-mono-nums text-ink-200">--full</span>) it also exercises
              extraction, at ${headline.costUsd.toFixed(4)} for the set.
            </Method>
            <Method title="Cross-family calibration">
              {calibrationMeta.family} ({calibrationMeta.labeler}) re-labels every case from the same
              declared policy, at temperature {calibrationMeta.temperature}, over {calibrationMeta.runs}{' '}
              runs. A different family on purpose: an LLM judge favors its own family&rsquo;s outputs,
              so letting GPT grade GPT-labeled truth would inflate agreement by construction. It never
              touches the verdict path.
            </Method>
            <Reveal delay={0.08}>
              <div className="rounded-[14px] border border-insufficient/30 p-6 bg-insufficient/[0.04]">
                <div className="font-mono-nums text-[11px] tracking-[0.12em] uppercase text-insufficient mb-2.5">
                  Why 100% is expected, not suspicious
                </div>
                <p className="text-[14.5px] leading-relaxed text-ink-200">
                  The checkers are code implementing exactly the rules the labels encode, so anything
                  below 100% would be a bug, not a difficulty signal. The benchmark is a regression net
                  and a coverage proof — and it can fail: a sabotage harness that swaps in an
                  affirm-everything checker <span className="text-insufficient">drops accuracy to 67.5%</span>,
                  and that failure is asserted by a test on every CI run.
                </p>
              </div>
            </Reveal>
          </div>

          <div className="text-center mt-16">
            <button onClick={onBack} className="btn-ghost text-sm">
              <ArrowLeft size={15} />
              Back to Attest
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}

function MatrixRow({
  actual,
  predicted,
  rowIndex,
}: {
  actual: string;
  predicted: number[];
  rowIndex: number;
}) {
  return (
    <>
      <div className="flex items-center justify-end gap-2.5 pr-1.5">
        <span className="w-2 h-2 rounded-full" style={{ background: VERDICT_HEX[COLS[rowIndex]] }} />
        <span className="font-mono-nums text-[11px] text-ink-300">{short(actual)}</span>
      </div>
      {predicted.map((val, ci) => {
        const isDiag = rowIndex === ci;
        const vc = VERDICT_HEX[COLS[ci]];
        const intensity = maxCell ? val / maxCell : 0;
        return (
          <div
            key={ci}
            className="relative flex items-center justify-center h-24 rounded-[14px] overflow-hidden font-mono-nums"
            style={
              isDiag
                ? {
                    fontSize: 34,
                    color: vc,
                    background: 'rgba(255,255,255,0.02)',
                    border: `1px solid ${vc}55`,
                    boxShadow: `0 0 0 1px ${vc}, 0 0 34px -8px ${vc}`,
                  }
                : {
                    fontSize: 18,
                    color: '#3A3E44',
                    background: 'rgba(150,158,168,0.035)',
                    border: '1px solid rgba(150,158,168,0.08)',
                  }
            }
          >
            {isDiag ? (
              <span className="relative z-10">
                <CountUp value={val} />
              </span>
            ) : (
              <span className="relative z-10">{val}</span>
            )}
            {isDiag && (
              <div className="absolute inset-0 z-0" style={{ background: vc, opacity: intensity * 0.16 }} />
            )}
          </div>
        );
      })}
    </>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="font-mono-nums text-[10px] tracking-[0.12em] uppercase text-ink-400 mb-1.5">
        {label}
      </div>
      <div className="font-mono-nums text-base text-supported">{value}</div>
    </div>
  );
}

function Method({
  title,
  delay = 0,
  children,
}: {
  title: string;
  delay?: number;
  children: ReactNode;
}) {
  return (
    <Reveal delay={delay}>
      <div className="font-mono-nums text-[11px] tracking-[0.12em] uppercase text-ink-200 mb-2.5">
        {title}
      </div>
      <p className="text-[14.5px] leading-relaxed text-ink-300">{children}</p>
    </Reveal>
  );
}
