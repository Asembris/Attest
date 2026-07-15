import { motion } from 'framer-motion';
import { ArrowLeft, ShieldCheck, BarChart3, Grid3x3, Repeat } from 'lucide-react';
import { benchmarkTable, confusionMatrix, determinismRuns } from '../data/mockData';

function fmtPct(n: number, unit: string) {
  if (unit === '') return n.toFixed(3);
  return `${(n * 100).toFixed(1)}%`;
}

function BenchmarkTable() {
  return (
    <div className="surface-card overflow-hidden">
      <div className="px-5 py-4 border-b border-ink-700/40">
        <div className="flex items-center gap-2">
          <BarChart3 size={15} className="text-ink-300" />
          <h3 className="text-sm font-medium text-ink-100">Performance Metrics</h3>
        </div>
        <p className="text-xs text-ink-400 mt-1">300-claim evaluation set · catalog-grounded verification</p>
      </div>
      <table className="w-full">
        <thead>
          <tr className="border-b border-ink-700/40">
            <th className="text-left text-label-sm py-3 px-5 font-medium">Metric</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">
              <span className="text-supported">Attest</span>
            </th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">AI Judge (baseline)</th>
            <th className="text-right text-label-sm py-3 px-5 font-medium">Δ</th>
          </tr>
        </thead>
        <tbody>
          {benchmarkTable.map((row, i) => {
            const delta = row.attest - row.aiJudge;
            const positive = delta > 0;
            return (
              <motion.tr
                key={row.metric}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: i * 0.08 }}
                className="border-b border-ink-700/20 last:border-0 hover:bg-ink-800/30 transition-colors"
              >
                <td className="py-3.5 px-5 text-sm text-ink-100">{row.metric}</td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-supported font-medium">
                  {fmtPct(row.attest, row.unit)}
                </td>
                <td className="py-3.5 px-5 text-right font-mono-nums text-sm text-ink-300">
                  {fmtPct(row.aiJudge, row.unit)}
                </td>
                <td className={`py-3.5 px-5 text-right font-mono-nums text-sm ${positive ? 'text-supported' : 'text-contradicted'}`}>
                  {positive ? '+' : ''}{delta > 0 ? `+${(delta * 100).toFixed(1)}` : delta.toFixed(3)}{row.unit === '%' ? '%' : ''}
                </td>
              </motion.tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ConfusionMatrixViz() {
  const labels = ['Supported', 'Contradicted', 'Insufficient'];
  const colors = ['bg-supported', 'bg-contradicted', 'bg-insufficient'];
  const maxVal = 293;

  return (
    <div className="surface-card p-5">
      <div className="flex items-center gap-2 mb-1">
        <Grid3x3 size={15} className="text-ink-300" />
        <h3 className="text-sm font-medium text-ink-100">Confusion Matrix</h3>
      </div>
      <p className="text-xs text-ink-400 mb-5">Actual vs. predicted verdict · 300 claims</p>

      {/* Matrix grid */}
      <div className="overflow-x-auto">
        <div className="inline-grid gap-1" style={{ gridTemplateColumns: 'auto repeat(3, minmax(72px, 1fr))' }}>
          {/* Top-left empty + predicted labels */}
          <div />
          {labels.map((l, i) => (
            <div key={l} className="text-label-sm text-center pb-2 flex items-center justify-center gap-1.5">
              <span className={`w-1.5 h-1.5 rounded-full ${colors[i]}`} />
              {l}
            </div>
          ))}

          {/* Rows */}
          {confusionMatrix.map((row, ri) => (
            <ActualRow key={row.actual} row={row} rowIndex={ri} maxVal={maxVal} />
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
}: {
  row: { actual: string; predicted: { supported: number; contradicted: number; insufficient: number } };
  rowIndex: number;
  maxVal: number;
}) {
  const colors = ['bg-supported', 'bg-contradicted', 'bg-insufficient'];
  const textColors = ['text-supported', 'text-contradicted', 'text-insufficient'];
  const keys: (keyof typeof row.predicted)[] = ['supported', 'contradicted', 'insufficient'];
  const values = keys.map((k) => row.predicted[k]);

  return (
    <>
      <div className="flex items-center pr-3 py-1 text-label-sm justify-end">
        <span className={`w-1.5 h-1.5 rounded-full ${colors[rowIndex]} mr-1.5`} />
        {row.actual}
      </div>
      {values.map((v, ci) => {
        const isDiagonal = rowIndex === ci;
        const intensity = v / maxVal;
        return (
          <div
            key={ci}
            className={`relative flex items-center justify-center rounded-md py-3 font-mono-nums text-sm transition-all ${
              isDiagonal
                ? `${textColors[ci]} bg-white/[0.04] border border-white/10`
                : 'text-ink-300 bg-ink-800/40'
            }`}
            style={isDiagonal ? { boxShadow: `inset 0 0 0 1px rgba(255,255,255,0.06)` } : undefined}
          >
            <span className="relative z-10">{v}</span>
            {isDiagonal && (
              <motion.div
                initial={{ scaleY: 0 }}
                animate={{ scaleY: 1 }}
                transition={{ delay: 0.3 + rowIndex * 0.1, duration: 0.4 }}
                className={`absolute inset-0 ${colors[ci]} origin-bottom rounded-md`}
                style={{ opacity: intensity * 0.12 }}
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
        <h3 className="text-sm font-medium text-supported">Determinism Contrast</h3>
      </div>

      {/* Headline stat */}
      <div className="mb-8">
        <motion.blockquote
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
          className="font-serif text-2xl lg:text-3xl font-light text-ink-50 leading-snug text-balance"
        >
          An AI judge asked the same question five times gave{' '}
          <span className="text-contradicted">five different answers</span>. Attest gave the{' '}
          <span className="text-supported">same answer every time</span>.
        </motion.blockquote>
      </div>

      {/* Side-by-side runs */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Attest column */}
        <div>
          <div className="flex items-center gap-2 mb-3">
            <ShieldCheck size={14} className="text-supported" />
            <span className="text-label text-supported">Attest</span>
          </div>
          <div className="space-y-1.5">
            {determinismRuns.filter((r) => r.source === 'Attest').map((r, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.3 + i * 0.1 }}
                className="flex items-center gap-3 p-2.5 rounded-lg bg-supported/5 border border-supported/15"
              >
                <span className="text-label-sm w-8">Run {r.run}</span>
                <span className="font-mono-nums text-sm text-supported">{r.answer}</span>
                <ShieldCheck size={12} className="text-supported ml-auto" />
              </motion.div>
            ))}
          </div>
        </div>

        {/* AI Judge column */}
        <div>
          <div className="flex items-center gap-2 mb-3">
            <Repeat size={14} className="text-contradicted" />
            <span className="text-label text-contradicted">AI Judge</span>
          </div>
          <div className="space-y-1.5">
            {determinismRuns.filter((r) => r.source === 'AI Judge').map((r, i) => (
              <motion.div
                key={i}
                initial={{ opacity: 0, x: 10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.3 + i * 0.1 }}
                className="flex items-center gap-3 p-2.5 rounded-lg bg-ink-800/60 border border-ink-700/40"
              >
                <span className="text-label-sm w-8">Run {r.run}</span>
                <span
                  className={`font-mono-nums text-sm ${
                    r.answer === 'Supported'
                      ? 'text-supported'
                      : r.answer === 'Contradicted'
                      ? 'text-contradicted'
                      : 'text-insufficient'
                  }`}
                >
                  {r.answer}
                </span>
              </motion.div>
            ))}
          </div>
        </div>
      </div>

      <p className="mt-6 text-xs text-ink-400 leading-relaxed max-w-prose">
        Same prompt, same catalog snapshot, five consecutive runs. Attest's verification is deterministic —
        catalog-grounded retrieval plus rule-based matching yields identical verdicts across runs. The AI
        judge's probabilistic sampling produced three different verdicts for the same claim.
      </p>
    </div>
  );
}

export default function Benchmark({
  onBack,
}: {
  onBack: () => void;
}) {
  return (
    <div className="min-h-screen bg-ink-950">
      {/* Header */}
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
        {/* Intro */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5 }}
        >
          <h1 className="font-serif text-headline font-light text-ink-50">
            Evidence &amp; Benchmark
          </h1>
          <p className="mt-3 text-ink-300 text-sm lg:text-base max-w-prose leading-relaxed">
            Attest verifies AI claims against a structured data catalog — not against another model.
            Every verdict is traceable to catalog metadata: ownership, freshness, PII tags, and schema.
            Below is the scientific backing.
          </p>
        </motion.div>

        {/* Determinism contrast — the emotional peak */}
        <DeterminismContrast />

        {/* Metrics + confusion matrix */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <BenchmarkTable />
          <ConfusionMatrixViz />
        </div>

        {/* Methodology footer */}
        <div className="surface-card p-6">
          <h3 className="text-sm font-medium text-ink-100 mb-3">Methodology</h3>
          <div className="space-y-2 text-sm text-ink-300 leading-relaxed max-w-prose">
            <p>
              <span className="text-ink-100 font-medium">Evaluation set:</span> 300 natural-language claims
              about 12 cataloged tables, hand-labeled by 3 data engineers. Labels: Supported, Contradicted,
              Insufficient Coverage.
            </p>
            <p>
              <span className="text-ink-100 font-medium">Attest pipeline:</span> Claim → entity extraction →
              catalog lookup → rule-based match against metadata fields → deterministic verdict. No LLM in
              the verification path.
            </p>
            <p>
              <span className="text-ink-100 font-medium">AI Judge baseline:</span> GPT-4-class model prompted
              with the same catalog snapshot, asked to classify each claim. Temperature 0.7, five runs per
              claim.
            </p>
            <p>
              <span className="text-ink-100 font-medium">Determinism:</span> Attest produced identical
              verdicts on all 300 claims across 5 runs. The AI judge produced different verdicts on 62% of
              claims across runs.
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
