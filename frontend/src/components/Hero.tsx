import { useState, useRef, Suspense, lazy } from 'react';
import { motion } from 'framer-motion';
import { ArrowRight, BarChart3, ShieldCheck, AlertTriangle } from 'lucide-react';
import { sampleAgentOutput } from '../data/mockData';
import UrnPicker from './UrnPicker';

const HeroLattice = lazy(() => import('./HeroLattice'));

export default function Hero({
  onRunAudit,
  onShowBenchmark,
  error,
}: {
  onRunAudit: (text: string) => void;
  onShowBenchmark: () => void;
  error?: string | null;
}) {
  const [text, setText] = useState(sampleAgentOutput);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const [reducedMotion] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  );

  // Insert a picked seeded URN at the textarea cursor (append if it is not focused), then
  // restore the caret just after the inserted URN so the user can keep composing.
  function insertUrn(urn: string) {
    const ta = taRef.current;
    if (!ta) {
      setText((t) => (t ? `${t} ${urn}` : urn));
      return;
    }
    const start = ta.selectionStart ?? text.length;
    const end = ta.selectionEnd ?? text.length;
    setText(text.slice(0, start) + urn + text.slice(end));
    requestAnimationFrame(() => {
      ta.focus();
      const pos = start + urn.length;
      ta.setSelectionRange(pos, pos);
    });
  }

  return (
    <div className="relative min-h-screen flex flex-col overflow-hidden">
      {/* Ambient lattice background */}
      <div className="absolute inset-0 opacity-60">
        {!reducedMotion && (
          <Suspense fallback={null}>
            <HeroLattice />
          </Suspense>
        )}
      </div>
      <div className="absolute inset-0 bg-grid opacity-40" />
      <div className="absolute inset-0 bg-gradient-to-b from-ink-950/40 via-transparent to-ink-950" />

      {/* Nav */}
      <nav className="relative z-10 flex items-center justify-between px-6 lg:px-12 py-6">
        <div className="flex items-center gap-2">
          <ShieldCheck size={20} className="text-supported" strokeWidth={2.5} />
          <span className="font-serif text-lg font-medium tracking-tight">Attest</span>
        </div>
        <button onClick={onShowBenchmark} className="btn-ghost text-sm">
          <BarChart3 size={15} />
          Evidence &amp; Benchmark
        </button>
      </nav>

      {/* Main content */}
      <div className="relative z-10 flex-1 flex flex-col items-center justify-center px-6 lg:px-12 pb-16">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
          className="w-full max-w-2xl text-center"
        >
          <motion.h1
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.1, ease: [0.22, 1, 0.36, 1] }}
            className="font-serif text-display font-light text-ink-50"
          >
            Attest
          </motion.h1>
          <motion.p
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.7, delay: 0.25, ease: [0.22, 1, 0.36, 1] }}
            className="mt-5 text-lg lg:text-xl text-ink-200 font-light text-balance"
          >
            Verify what your AI claims about your data.
          </motion.p>
        </motion.div>

        {/* Textarea card */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.4, ease: [0.22, 1, 0.36, 1] }}
          className="w-full max-w-2xl mt-10"
        >
          <div className="surface-card p-1.5 backdrop-blur-sm bg-ink-850/80">
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-ink-700/40">
              <span className="text-label-sm">Paste AI Agent Output</span>
              <div className="flex items-center gap-4">
                <UrnPicker onPick={insertUrn} />
                <span className="text-xs text-ink-400 font-mono-nums">{text.length} chars</span>
              </div>
            </div>
            <textarea
              ref={taRef}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={7}
              className="w-full bg-transparent px-4 py-4 text-sm text-ink-100 leading-relaxed resize-none focus:outline-none placeholder:text-ink-400"
              placeholder="Paste the AI agent's claims about your data tables here..."
            />
            <div className="flex items-center justify-between px-3 pb-3">
              <span className="text-xs text-ink-400 px-2">
                Claims are checked against your live DataHub catalog.
              </span>
              <button
                onClick={() => onRunAudit(text)}
                disabled={!text.trim()}
                className="btn-primary group disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Run Audit
                <ArrowRight size={16} className="transition-transform group-hover:translate-x-0.5" />
              </button>
            </div>
          </div>
        </motion.div>

        {error && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="mt-6 flex items-center gap-2 text-sm text-contradicted max-w-2xl"
          >
            <AlertTriangle size={15} />
            {error}
          </motion.div>
        )}
      </div>
    </div>
  );
}
