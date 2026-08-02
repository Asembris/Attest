import { useState, useRef, Suspense, lazy } from 'react';
import { motion } from 'framer-motion';
import { ArrowRight, BarChart3, Library, AlertTriangle } from 'lucide-react';
import { sampleAgentOutput } from '../data/mockData';
import AttestMark from './AttestMark';
import UrnPicker from './UrnPicker';

const HeroLattice = lazy(() => import('./HeroLattice'));

export default function Hero({
  onRunAudit,
  onShowBenchmark,
  onShowClaims,
  error,
}: {
  onRunAudit: (text: string) => void;
  onShowBenchmark: () => void;
  // Reachable WITHOUT running an audit first, deliberately: the claims view reads the
  // catalog, so it is exactly what a second party would open — and a second party has no
  // audit of their own. Requiring a run first would quietly make the point unprovable.
  onShowClaims: () => void;
  error?: string | null;
}) {
  // Computed once per mount so each page load gets a fresh freshness window — a distinct
  // content-addressed artifact URN, so every visitor's read-back starts clean. See mockData.
  const [text, setText] = useState(() => sampleAgentOutput());
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
    <div
      className="relative min-h-screen flex flex-col overflow-hidden"
      style={{
        background:
          'radial-gradient(120% 120% at 50% 38%, #0b0e13 0%, #070809 55%, #040405 100%)',
      }}
    >
      {/* Ambient particle field */}
      <div className="absolute inset-0">
        {!reducedMotion && (
          <Suspense fallback={null}>
            <HeroLattice />
          </Suspense>
        )}
      </div>
      {/* Vignette — focuses the eye on the centre without hiding the field */}
      <div
        className="absolute inset-0 pointer-events-none"
        style={{
          background:
            'radial-gradient(70% 55% at 50% 46%, rgba(5,6,7,0) 40%, rgba(5,6,7,0.55) 100%)',
        }}
      />

      {/* Nav — BOTH destinations, deliberately: claims is reachable without an audit. */}
      <nav className="relative z-10 flex items-center justify-between px-6 lg:px-12 py-6">
        <div className="flex items-center gap-2">
          <AttestMark size={20} className="text-ink-100" />
          <span className="font-serif text-xl font-medium tracking-tight">Attest</span>
        </div>
        <div className="flex items-center gap-1">
          <button onClick={onShowClaims} className="btn-ghost text-sm">
            <Library size={15} />
            Published claims
          </button>
          <button onClick={onShowBenchmark} className="btn-ghost text-sm">
            <BarChart3 size={15} />
            Evidence &amp; Benchmark
          </button>
        </div>
      </nav>

      {/* Main content */}
      <div className="relative z-10 flex-1 flex flex-col items-center justify-center px-6 lg:px-12 pb-16 pointer-events-none">
        <motion.div
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.9, ease: [0.2, 0.7, 0.2, 1] }}
          className="w-full max-w-2xl text-center"
        >
          <motion.h1
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1.0, delay: 0.05, ease: [0.2, 0.7, 0.2, 1] }}
            className="font-serif font-medium text-ink-50 leading-[0.9] tracking-[0.005em] text-[clamp(72px,11vw,158px)]"
            style={{ textShadow: '0 6px 60px rgba(120,150,200,0.28)' }}
          >
            Attest
          </motion.h1>
          <motion.p
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.9, delay: 0.2, ease: [0.2, 0.7, 0.2, 1] }}
            className="mt-5 text-lg lg:text-2xl font-light text-ink-200 tracking-[0.005em] text-balance"
          >
            Verify what your AI claims about your data.
          </motion.p>
        </motion.div>

        {/* Textarea card — glass, and it is a real editable input (the mockup showed static
            prose; the demo needs the textarea, the URN picker and the char count). */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.9, delay: 0.35, ease: [0.2, 0.7, 0.2, 1] }}
          className="w-full max-w-2xl mt-12 pointer-events-auto"
        >
          <div
            className="rounded-2xl border border-[rgba(150,168,196,0.14)] overflow-hidden backdrop-blur-md"
            style={{
              background: 'linear-gradient(180deg, rgba(20,24,31,0.72) 0%, rgba(12,15,20,0.80) 100%)',
              boxShadow: '0 30px 80px -30px rgba(0,0,0,0.8), inset 0 1px 0 rgba(255,255,255,0.04)',
            }}
          >
            <div className="flex items-center justify-between px-4 py-2.5 border-b border-[rgba(150,168,196,0.1)]">
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
            <div className="flex items-center justify-between px-3 pb-3 border-t border-[rgba(150,168,196,0.1)] pt-3">
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
            className="mt-6 flex items-center gap-2 text-sm text-contradicted max-w-2xl pointer-events-auto"
          >
            <AlertTriangle size={15} />
            {error}
          </motion.div>
        )}

        {/* The interaction affordance for the field behind everything. */}
        {!reducedMotion && (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 1.6, delay: 1 }}
            className="mt-9 font-mono-nums text-[11px] tracking-[0.14em] uppercase text-[rgba(140,155,180,0.4)]"
          >
            move · hover · click the field
          </motion.p>
        )}
      </div>
    </div>
  );
}
