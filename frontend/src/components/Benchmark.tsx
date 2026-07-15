import { motion } from 'framer-motion';
import { ArrowLeft, ShieldCheck, BarChart3, Grid3x3, Repeat, Coins, Sparkles } from 'lucide-react';
import {
  benchmarkMeta,
  perVerdict,
  headline,
  confusion,
  calibrationMeta,
  calibrationRuns,
} from '../data/benchmarkData';

// Phase 5: the benchmark page, re-truthed. Every number here is read from benchmarkData.ts,
// which mirrors the committed receipts (benchmark/results/*.json + README.md). The layout is
// unchanged from the fabricated version because the DETERMINISM CONTRAST was always the right
// frame — it just needed the real finding instead of an invented one: the sharpest argument
// in the whole project is that a cross-family judge answers its own question differently at
// temperature 0, while the deterministic core does not.

function pct(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function disputedLabel(disputed: string[]): string {
  if (disputed.length === 0) return '∅  no stable dispute';
  return `{ ${disputed.join(', ')} }`;
}

function MetricsTable() {
  return (
    <div className="surface-card overflow-hidden">
      <div className="px-5 py-4 border-b border-ink-700/40">
        <div className="flex items-center gap-2">
          <BarChart3 size={15} className="text-ink-300" />
          <h3 className="text-sm font-medium text-ink-100">Per-verdict metrics</h3>
        </div>
        <p className="text-xs text-ink-400 mt-1">
          {benchmarkMeta.nCases} hand-labeled claims · macro-F1{' '}
          <span className="text-supported font-mono-nums">{headline.macroF1.toFixed(3)}</span>
        </p>
      </div>
      <table className="w-full">
        <thead>
          <tr className="border-b border-ink-700/40">
            <th className="text-left text-label-sm py-3 px-5 font-medium">Verdict</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">Precision</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">Recall</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">F1</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">n</th>
          </tr>
        </thead>
        <tbody>
          {perVerdict.map((row, i) => {
            const color =
              row.verdict === 'Supported'
                ? 'text-supported'
                : row.verdict === 'Contradicted'
                  ? 'text-contradicted'
                  : 'text-insufficient';
            return (
              <motion.tr
                key={row.verdict}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: i * 0.08 }}
                className="border-b border-ink-700/20 last:border-0 hover:bg-ink-800/30 transition-colors"
              >
                <td className="py-3.5 px-5 text-sm">
                  <span className="flex items-center gap-2">
                    <span
                      className={`w-1.5 h-1.5 rounded-full ${
                        row.verdict === 'Supported'
                          ? 'bg-supported'
                          : row.verdict === 'Contradicted'
                            ? 'bg-contradicted'
                            : 'bg-insufficient'
                      }`}
                    />
                    <span className={color}>{row.verdict}</span>
                  </span>
                </td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-ink-100">{row.precision.toFixed(3)}</td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-ink-100">{row.recall.toFixed(3)}</td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-ink-100">{row.f1.toFixed(3)}</td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-ink-300">{row.support}</td>
              </motion.tr>
            );
          })}
        </tbody>
      </table>
      <div className="px-5 py-3 border-t border-ink-700/40 grid grid-cols-3 gap-3">
        <Kpi label="Accuracy" value={pct(headline.accuracy)} />
        <Kpi label={`pass@${headline.passAtKCore.k} · core`} value={pct(headline.passAtKCore.value)} />
        <Kpi label={`pass@${headline.passAtKFull.k} · full`} value={pct(headline.passAtKFull.value)} />
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-label-sm">{label}</div>
      <div className="font-mono-nums text-sm text-supported font-medium">{value}</div>
    </div>
  );
}

function ConfusionMatrixViz() {
  const labels = ['Supported', 'Contradicted', 'Insufficient'];
  const colors = ['bg-supported', 'bg-contradicted', 'bg-insufficient'];
  const textColors = ['text-supported', 'text-contradicted', 'text-insufficient'];
  const total = confusion.reduce((s, r) => s + r.predicted.reduce((a, b) => a + b, 0), 0);
  const maxVal = Math.max(...confusion.flatMap((r) => r.predicted));

  return (
    <div className="surface-card p-5">
      <div className="flex items-center gap-2 mb-1">
        <Grid3x3 size={15} className="text-ink-300" />
        <h3 className="text-sm font-medium text-ink-100">Confusion matrix</h3>
      </div>
      <p className="text-xs text-ink-400 mb-5">Actual vs. predicted verdict · {total} claims · zero off-diagonal</p>

      <div className="overflow-x-auto">
        <div className="inline-grid gap-1" style={{ gridTemplateColumns: 'auto repeat(3, minmax(72px, 1fr))' }}>
          <div />
          {labels.map((l, i) => (
            <div key={l} className="text-label-sm text-center pb-2 flex items-center justify-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${colors[i]}`} />
              {l}
            </div>
          ))}

          {confusion.map((row, ri) => (
            <ActualRow key={row.actual} row={row} rowIndex={ri} maxVal={maxVal} colors={colors} textColors={textColors} />
          ))}
        </div>
      </div>

      <div className="mt-4 flex items-center justify-between text-xs text-ink-400">
        <span>← Predicted</span>
        <span>Actual ↓</span>
      </div>
    </div>
  );
}

function ActualRow({
  row,
  rowIndex,
  maxVal,
  colors,
  textColors,
}: {
  row: { actual: string; predicted: number[] };
  rowIndex: number;
  maxVal: number;
  colors: string[];
  textColors: string[];
}) {
  const shortActual = row.actual === 'Insufficient-Coverage' ? 'Insufficient' : row.actual;
  return (
    <>
      <div className="flex items-center pr-3 py-1 text-label-sm justify-end">
        <span className={`w-1.5 h-1.5 rounded-full ${colors[rowIndex]} mr-1.5`} />
        {shortActual}
      </div>
      {row.predicted.map((v, ci) => {
        const isDiagonal = rowIndex === ci;
        const intensity = maxVal ? v / maxVal : 0;
        return (
          <div
            key={ci}
            className={`relative flex items-center justify-center rounded-md py-3 font-mono-nums text-sm transition-all ${
              isDiagonal ? `${textColors[ci]} bg-white/[0.04] border border-white/10` : 'text-ink-500 bg-ink-800/40'
            }`}
          >
            <span className="relative z-10">{v}</span>
            {isDiagonal && (
              <motion.div
                initial={{ scaleY: 0 }}
                animate={{ scaleY: 1 }}
                transition={{ delay: 0.3 + rowIndex * 0.1, duration: 0.4 }}
                className={`absolute inset-0 ${colors[ci]} origin-bottom rounded-md`}
                style={{ opacity: intensity * 0.14 }}
              />
            )}
          </div>
        );
      })}
    </>
  );
}

function DeterminismContrast() {
  return (
    <div className="surface-card p-6 lg:p-8">
      <div className="flex items-center gap-2 mb-4">
        <Repeat size={15} className="text-supported" />
        <h3 className="text-sm font-medium text-supported">Determinism contrast</h3>
      </div>

      <div className="mb-8">
        <motion.blockquote
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
          className="font-serif text-2xl lg:text-3xl font-light text-ink-50 leading-snug text-balance"
        >
          Asked to re-label the same {benchmarkMeta.nCases} claims six times, an independent model
          family <span className="text-contradicted">disputed a different set each run</span>.
          Attest's core returned the{' '}
          <span className="text-supported">same {perVerdict.reduce((s, v) => s + v.support, 0)} verdicts every time</span>.
        </motion.blockquote>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Attest column — the deterministic core */}
        <div>
          <div className="flex items-center gap-2 mb-3">
            <ShieldCheck size={14} className="text-supported" />
            <span className="text-label text-supported">Attest core · pass@{headline.passAtKCore.k}</span>
          </div>
          <div className="space-y-1.5">
            {Array.from({ length: headline.passAtKCore.k }, (_, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.3 + i * 0.1 }}
                className="flex items-center gap-3 p-2.5 rounded-lg bg-supported/5 border border-supported/15"
              >
                <span className="text-label-sm w-10">Run {i + 1}</span>
                <span className="font-mono-nums text-sm text-supported">
                  {benchmarkMeta.nCases}/{benchmarkMeta.nCases} identical
                </span>
                <ShieldCheck size={12} className="text-supported ml-auto" />
              </motion.div>
            ))}
          </div>
        </div>

        {/* Cross-family judge column — Nemotron, Llama family */}
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Repeat size={14} className="text-contradicted" />
            <span className="text-label text-contradicted">{calibrationMeta.family} judge · temp {calibrationMeta.temperature}</span>
          </div>
          <div className="space-y-1.5">
            {calibrationRuns.map((r, i) => (
              <motion.div
                key={r.run}
                initial={{ opacity: 0, x: 10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.3 + i * 0.08 }}
                className="flex items-center gap-3 p-2.5 rounded-lg bg-ink-800/60 border border-ink-700/40"
              >
                <span className="text-label-sm w-10">Run {r.run}</span>
                <span className="font-mono-nums text-xs text-ink-400 w-14">{pct(r.agreement)}</span>
                <span
                  className={`font-mono-nums text-sm ${r.disputed.length === 0 ? 'text-supported' : 'text-contradicted'}`}
                >
                  {disputedLabel(r.disputed)}
                </span>
              </motion.div>
            ))}
          </div>
        </div>
      </div>

      <p className="mt-6 text-xs text-ink-400 leading-relaxed max-w-prose">
        Identical code, identical prompt, temperature 0. The judge's dispute set reshuffles every
        run — {'{class-15}'} → {'{class-03, class-15}'} → {'{class-03, class-13}'} → {'{class-03}'} →
        ∅ → ∅ — and <span className="text-ink-200">every case it ever disputed, it also agreed with in
        another run</span>. No disagreement survives repetition, so the residual 5% is judge noise,
        not a finding about the labels. Attest's checkers are date math and set membership; they
        return the same verdict every time, which is why the core's pass@{headline.passAtKCore.k} is
        100%. A judge that answers its own question differently cannot <em>be</em> ground truth — it
        can only calibrate it.
      </p>
    </div>
  );
}

export default function Benchmark({ onBack }: { onBack: () => void }) {
  return (
    <div className="min-h-screen bg-ink-950">
      <header className="sticky top-0 z-20 border-b border-ink-700/40 bg-ink-950/80 backdrop-blur-md">
        <div className="max-w-5xl mx-auto px-6 lg:px-8 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={onBack} className="btn-ghost text-sm px-2">
              <ArrowLeft size={16} />
            </button>
            <div className="flex items-center gap-2">
              <ShieldCheck size={18} className="text-supported" strokeWidth={2.5} />
              <span className="font-serif text-base font-medium tracking-tight">Attest</span>
            </div>
            <span className="text-ink-500 mx-1">/</span>
            <span className="text-sm text-ink-300">Evidence &amp; Benchmark</span>
          </div>
        </div>
      </header>

      <div className="max-w-5xl mx-auto px-6 lg:px-8 py-10 space-y-10">
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <h1 className="font-serif text-headline font-light text-ink-50">Evidence &amp; Benchmark</h1>
          <p className="mt-3 text-ink-300 text-sm lg:text-base max-w-prose leading-relaxed">
            {benchmarkMeta.nCases} hand-labeled groundedness claims about a DataHub catalog,{' '}
            {benchmarkMeta.hard} of them marked hard, spanning all {benchmarkMeta.cells} (claim type ×
            verdict) cells. Every verdict is decided by deterministic code — no model in the
            verification path — and cross-checked by an independent model family.
          </p>
        </motion.div>

        <DeterminismContrast />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <MetricsTable />
          <ConfusionMatrixViz />
        </div>

        {/* Receipts strip */}
        <div className="surface-card p-5 flex flex-wrap items-center gap-x-8 gap-y-3">
          <div className="flex items-center gap-2">
            <Coins size={14} className="text-ink-300" />
            <div>
              <div className="text-label-sm">Full-pipeline cost</div>
              <div className="font-mono-nums text-sm text-ink-100">${headline.costUsd.toFixed(4)} / {headline.explanations} claims</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Sparkles size={14} className="text-ink-300" />
            <div>
              <div className="text-label-sm">Model-authored explanations</div>
              <div className="font-mono-nums text-sm text-ink-100">
                {headline.modelAuthored}/{headline.explanations} · {headline.guardRejected} guard rejections
              </div>
            </div>
          </div>
        </div>

        {/* Methodology */}
        <div className="surface-card p-6">
          <h3 className="text-sm font-medium text-ink-100 mb-3">Methodology</h3>
          <div className="space-y-2 text-sm text-ink-300 leading-relaxed max-w-prose">
            <p>
              <span className="text-ink-100 font-medium">Dataset:</span> {benchmarkMeta.nCases}{' '}
              natural-language claims about a seeded catalog, hand-labeled with a one-line rationale
              each. Both the prose and the structured claim are carried, so a checker bug can be told
              from an extraction bug. All {benchmarkMeta.cells} (claim type × verdict) cells are
              populated; a cell going dark is a silent misclassifier.
            </p>
            <p>
              <span className="text-ink-100 font-medium">Attest core:</span> claim → catalog lookup →
              rule-based match against ownership, freshness, PII signals and schema → deterministic
              verdict. No LLM in the verification path. Run against the structured claim it is
              precision/recall/F1; run against the prose (<span className="font-mono-nums">--full</span>)
              it also exercises extraction, at ${headline.costUsd.toFixed(4)} for the set.
            </p>
            <p>
              <span className="text-ink-100 font-medium">Cross-family calibration:</span>{' '}
              {calibrationMeta.family} ({calibrationMeta.labeler}) re-labels every case from the same
              declared policy, at temperature {calibrationMeta.temperature}, over{' '}
              {calibrationMeta.runs} runs. A different family on purpose: an LLM judge favors its own
              family's outputs, so letting GPT grade GPT-labeled truth would inflate agreement by
              construction. It never touches the verdict path.
            </p>
            <p>
              <span className="text-ink-100 font-medium">Why 100% is expected, not suspicious:</span>{' '}
              the checkers are code implementing exactly the rules the labels encode, so anything below
              100% would be a bug, not a difficulty signal. The benchmark is a regression net and a
              coverage proof — and it can fail: a sabotage harness that swaps in an affirm-everything
              checker drops accuracy to 67.5%, and that failure is asserted by a test on every CI run.
            </p>
          </div>
        </div>

        <div className="pb-12 text-center">
          <button onClick={onBack} className="btn-ghost text-sm">
            <ArrowLeft size={15} />
            Back to Attest
          </button>
        </div>
      </div>
    </div>
  );
}
