import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import Hero from './components/Hero';
import AuditResults from './components/AuditResults';
import Benchmark from './components/Benchmark';
import { health as fetchHealth, submitAudit, ApiError } from './api/client';
import type { AuditRecord, HealthResponse } from './api/types';

type View = 'hero' | 'auditing' | 'results' | 'benchmark';

export default function App() {
  const [view, setView] = useState<View>('hero');
  const [record, setRecord] = useState<AuditRecord | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  async function runAudit(text: string) {
    setError(null);
    setView('auditing');
    try {
      const result = await submitAudit(text, 'attest-ui');
      setRecord(result);
      setView('results');
    } catch (e) {
      const detail =
        e instanceof ApiError ? e.detail : 'Could not reach the audit service. Is it running on :8003?';
      setError(detail);
      setView('hero');
    }
  }

  return (
    <AnimatePresence mode="wait">
      {view === 'hero' && (
        <motion.div key="hero" exit={{ opacity: 0 }} transition={{ duration: 0.3 }}>
          <Hero onRunAudit={runAudit} onShowBenchmark={() => setView('benchmark')} error={error} />
        </motion.div>
      )}

      {view === 'auditing' && (
        <motion.div
          key="auditing"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3 }}
          className="min-h-screen bg-ink-950 flex flex-col items-center justify-center"
        >
          <AuditingState />
        </motion.div>
      )}

      {view === 'results' && record && (
        <motion.div
          key="results"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
        >
          <AuditResults
            record={record}
            health={health}
            onBack={() => setView('hero')}
            onShowBenchmark={() => setView('benchmark')}
          />
        </motion.div>
      )}

      {view === 'benchmark' && (
        <motion.div
          key="benchmark"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
        >
          <Benchmark onBack={() => setView('hero')} />
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function AuditingState() {
  const steps = [
    'Sanitizing agent output...',
    'Decomposing claims...',
    'Resolving entities in the catalog...',
    'Running deterministic checks...',
    'Generating guarded explanations...',
  ];

  return (
    <div className="flex flex-col items-center gap-8">
      <div className="relative">
        <motion.div
          animate={{ rotate: 360 }}
          transition={{ duration: 1.2, repeat: Infinity, ease: 'linear' }}
          className="w-12 h-12 rounded-full border-2 border-ink-700 border-t-supported"
        />
      </div>
      <div className="space-y-2 w-80">
        {steps.map((s, i) => (
          <motion.div
            key={s}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: i * 0.25 }}
            className="flex items-center gap-2 text-sm text-ink-300"
          >
            <motion.span
              animate={{ opacity: [0.3, 1, 0.3] }}
              transition={{ duration: 1.5, repeat: Infinity, delay: i * 0.1 }}
              className="w-1.5 h-1.5 rounded-full bg-supported"
            />
            {s}
          </motion.div>
        ))}
      </div>
    </div>
  );
}
