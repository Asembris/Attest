import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import Hero from './components/Hero';
import AuditProgress from './components/AuditProgress';
import AuditResults from './components/AuditResults';
import Benchmark from './components/Benchmark';
import ClaimsExplorer from './components/ClaimsExplorer';
import { health as fetchHealth, submitAudit, approve, ApiError } from './api/client';
import { setDatahubUiUrl } from './api/types';
import type { AuditRecord, DecisionRequest, HealthResponse, WriteBackView } from './api/types';

// `claims` is the OTHER half of the thesis, and it is reachable from the results screen on
// purpose: a reviewer publishes a verdict and can then go and read it back out of the
// CATALOG, which is the whole argument — not "Attest saved it" but "the next agent inherits
// it". No router here (see the house convention); a view is a member of this union.
type View = 'hero' | 'auditing' | 'results' | 'benchmark' | 'claims';

export default function App() {
  const [view, setView] = useState<View>('hero');
  const [record, setRecord] = useState<AuditRecord | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [writebacks, setWritebacks] = useState<WriteBackView[] | null>(null);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);

  useEffect(() => {
    fetchHealth()
      .then((h) => {
        // Adopt the backend's configured DataHub UI origin before any card deep-links to it.
        setDatahubUiUrl(h.datahub_ui_url);
        setHealth(h);
      })
      .catch(() => setHealth(null));
  }, []);

  async function runAudit(text: string) {
    setError(null);
    setWritebacks(null);
    setApproveError(null);
    // Clear any prior record so the progress screen starts in its indeterminate phase; when
    // the POST resolves, AuditProgress reveals the REAL record, replays the run, and HOLDS on
    // completion until the human clicks "Continue to results" — which fires onContinue (below)
    // -> setView('results'). The browser E2E clicks that button. So the transition to results
    // is on the click, not here.
    setRecord(null);
    setView('auditing');
    try {
      const result = await submitAudit(text, 'attest-ui');
      setRecord(result);
    } catch (e) {
      const detail =
        e instanceof ApiError ? e.detail : 'Could not reach the audit service. Is it running on :8003?';
      setError(detail);
      setView('hero');
    }
  }

  async function settle(decisions: DecisionRequest[]) {
    if (!record) return;
    setApproving(true);
    setApproveError(null);
    try {
      const response = await approve(record.run_id, decisions);
      // The settled record replaces the parked one: its proposals now carry accepted/rejected
      // reviews and its status is complete, so the review bar and buttons retire on their own.
      setRecord(response.audit);
      setWritebacks(response.writebacks);
    } catch (e) {
      const detail =
        e instanceof ApiError ? e.detail : 'Could not reach the audit service to submit the decision.';
      setApproveError(detail);
    } finally {
      setApproving(false);
    }
  }

  return (
    <AnimatePresence mode="wait">
      {view === 'hero' && (
        <motion.div key="hero" exit={{ opacity: 0 }} transition={{ duration: 0.3 }}>
          <Hero
            onRunAudit={runAudit}
            onShowBenchmark={() => setView('benchmark')}
            onShowClaims={() => setView('claims')}
            error={error}
          />
        </motion.div>
      )}

      {view === 'auditing' && (
        <motion.div
          key="auditing"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.3 }}
        >
          <AuditProgress record={record} onContinue={() => setView('results')} />
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
            writebacks={writebacks}
            approving={approving}
            approveError={approveError}
            onApprove={settle}
            onBack={() => setView('hero')}
            onShowBenchmark={() => setView('benchmark')}
            onShowClaims={() => setView('claims')}
          />
        </motion.div>
      )}

      {view === 'claims' && (
        <motion.div
          key="claims"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
        >
          <ClaimsExplorer onBack={() => setView(record ? 'results' : 'hero')} />
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
