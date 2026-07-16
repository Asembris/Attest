import { useCallback, useEffect, useRef, useState } from 'react';
import { motion } from 'framer-motion';
import { AlertTriangle, ArrowLeft, Database, RefreshCw, Search, ShieldCheck } from 'lucide-react';
import { ApiError, listClaims, retryWriteback } from '../api/client';
import type { ClaimFilters, ClaimsResponse, ClaimType, Verdict } from '../api/types';
import { seededDatasets } from '../data/catalog';
import ClaimArtifactCard from './ClaimArtifactCard';

// WHAT THE NEXT AGENT INHERITS — the inheritance half of Challenge 1's thesis.
//
// Everything on this screen is READ FROM DATAHUB. Not from Attest's audit history: an
// explorer backed by SQLite would prove nothing about what a second party can retrieve,
// because a second party cannot open Attest's database. The one thing Attest's store
// contributes is an explanation for an ABSENT verdict — which the catalog genuinely cannot
// supply — and it can never add a claim, remove one, or change a verdict.
//
// The push-down disclosure at the top is not a debug panel. "Retrievable from DataHub" is
// true; "fully queryable in DataHub" is not, and a UI that showed a filtered list without
// saying who filtered it would let a reader believe the catalog answered a question Attest
// answered. That is an unfounded claim about where evidence came from, rendered by the tool
// built to catch unfounded claims about where evidence came from.

const VERDICTS: Verdict[] = ['Supported', 'Contradicted', 'Insufficient-Coverage'];
const CLAIM_TYPES: ClaimType[] = ['freshness', 'ownership', 'classification', 'schema'];

// THE AUTO-POLL, BOUNDED. Measured on the pinned server: a verdict becomes readable a median
// 2.1s after it is accepted, max 3.2s over five trials. So a claim that reads `pending-lag`
// resolves itself within seconds and a human should watch it happen rather than click
// anything — but the poll STOPS, and stops early.
//
// Past the cap you can no longer tell an index that is slow from a problem that is real, and
// a spinner that never gives up is a UI asserting "this is fine" about a state it has no
// evidence for. So it hands back to a person: five attempts over ten seconds — comfortably
// past the 7s worst case anything here has been measured at — and then a manual control.
const POLL_INTERVAL_MS = 2000;
const POLL_ATTEMPTS = 5;

export default function ClaimsExplorer({ onBack }: { onBack: () => void }) {
  const [filters, setFilters] = useState<ClaimFilters>({});
  const [data, setData] = useState<ClaimsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState<string | null>(null);
  const [pollsLeft, setPollsLeft] = useState(POLL_ATTEMPTS);

  // The filters as they were when the request went out. Rendering the disclosure against the
  // LIVE filter state would let it describe a query that has not run yet.
  const applied = useRef<ClaimFilters>({});

  const load = useCallback(async (next: ClaimFilters, resetPolls = true) => {
    setLoading(true);
    setError(null);
    applied.current = next;
    try {
      const body = await listClaims(next);
      setData(body);
      if (resetPolls) setPollsLeft(POLL_ATTEMPTS);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : 'Could not reach Attest.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load({});
  }, [load]);

  // Poll ONLY while something is genuinely resolving itself, and only while budget remains.
  //
  // The condition is `pending-lag` and nothing else, which is the load-bearing part: a claim
  // whose write FAILED will never grow a verdict no matter how long anyone waits, so polling
  // for one would be a spinner over a permanent state — an incomplete claim wearing a
  // transient's clothes. The backend has already consulted its own record to tell those
  // apart (retrieval.read_state); this only has to respect the answer.
  const lagging = (data?.claims ?? []).some((c) => c.state === 'pending-lag');

  useEffect(() => {
    if (!lagging || pollsLeft <= 0) return;
    const timer = setTimeout(() => {
      setPollsLeft((n) => n - 1);
      void load(applied.current, false);
    }, POLL_INTERVAL_MS);
    return () => clearTimeout(timer);
  }, [lagging, pollsLeft, data, load]);

  async function repair(runId: string) {
    setRetrying(runId);
    setError(null);
    try {
      await retryWriteback(runId);
      await load(applied.current);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : 'The repair could not be run.');
    } finally {
      setRetrying(null);
    }
  }

  function update(patch: Partial<ClaimFilters>) {
    const next = { ...filters, ...patch };
    setFilters(next);
    void load(next);
  }

  const claims = data?.claims ?? [];
  const retrieval = data?.retrieval;
  const gaveUp = lagging && pollsLeft <= 0;

  return (
    <div className="min-h-screen bg-ink-950">
      <header className="sticky top-0 z-20 border-b border-ink-700/40 bg-ink-950/80 backdrop-blur-md">
        <div className="max-w-4xl mx-auto px-6 lg:px-8 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button onClick={onBack} className="btn-ghost text-sm px-2">
              <ArrowLeft size={16} />
            </button>
            <div className="flex items-center gap-2">
              <ShieldCheck size={18} className="text-supported" strokeWidth={2.5} />
              <span className="font-serif text-base font-medium tracking-tight">Attest</span>
            </div>
            <span className="text-ink-500 mx-1">/</span>
            <span className="text-sm text-ink-300">Published claims</span>
          </div>
          <button
            onClick={() => load(applied.current)}
            disabled={loading}
            className="btn-ghost text-sm disabled:opacity-40"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </header>

      <div className="max-w-4xl mx-auto px-6 lg:px-8 py-8 space-y-6">
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <h1 className="font-serif text-headline font-light text-ink-50">
            What the next agent inherits
          </h1>
          <p className="mt-2 text-ink-300 text-sm max-w-2xl leading-relaxed">
            Every claim below is read out of <span className="text-ink-100">DataHub</span>, not
            out of Attest's audit history — one artifact per claim, carrying what was asserted,
            at what grain, and every verdict it has ever had. This is what a second agent gets
            from the catalog with no Attest process running.
          </p>
        </motion.div>

        {/* Filters */}
        <div className="surface-card p-5 space-y-4">
          <div className="flex items-center gap-1.5 text-label-sm">
            <Search size={11} /> Filter
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <label className="block">
              <span className="text-label-sm">Dataset</span>
              <select
                value={filters.target_urn ?? ''}
                onChange={(e) => update({ target_urn: e.target.value })}
                className="mt-1.5 w-full bg-ink-800 border border-ink-600/50 rounded-lg px-3 py-2 text-sm text-ink-100 focus:border-accent/50 focus:outline-none"
              >
                <option value="">Every dataset</option>
                {seededDatasets.map((d) => (
                  <option key={d.urn} value={d.urn}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-label-sm">Verdict</span>
              <select
                value={filters.verdict ?? ''}
                onChange={(e) => update({ verdict: e.target.value as Verdict | '' })}
                className="mt-1.5 w-full bg-ink-800 border border-ink-600/50 rounded-lg px-3 py-2 text-sm text-ink-100 focus:border-accent/50 focus:outline-none"
              >
                <option value="">Any verdict</option>
                {VERDICTS.map((v) => (
                  <option key={v} value={v}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-label-sm">Claim type</span>
              <select
                value={filters.claim_type ?? ''}
                onChange={(e) => update({ claim_type: e.target.value as ClaimType | '' })}
                className="mt-1.5 w-full bg-ink-800 border border-ink-600/50 rounded-lg px-3 py-2 text-sm text-ink-100 focus:border-accent/50 focus:outline-none"
              >
                <option value="">Any type</option>
                {CLAIM_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-label-sm">Reviewer</span>
              <input
                value={filters.reviewer ?? ''}
                onChange={(e) => setFilters({ ...filters, reviewer: e.target.value })}
                onBlur={() => update({})}
                onKeyDown={(e) => e.key === 'Enter' && update({})}
                placeholder="who signed a verdict off"
                className="mt-1.5 w-full bg-ink-800 border border-ink-600/50 rounded-lg px-3 py-2 text-sm text-ink-100 placeholder:text-ink-500 focus:border-accent/50 focus:outline-none"
              />
            </label>
          </div>
        </div>

        {/* WHERE EACH PREDICATE WAS APPLIED. Part of the answer, not diagnostics. */}
        {retrieval && (
          <div className="surface-card p-5 space-y-3 border-ink-700/60">
            <div className="flex items-center gap-1.5 text-label-sm">
              <Database size={11} /> Where this was filtered
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-mono-nums text-ink-400">{retrieval.entry_point}</span>
              <span className="text-ink-600">·</span>
              <span className="text-ink-300">
                DataHub returned{' '}
                <span className="font-mono-nums text-ink-100">{retrieval.considered}</span>,
                Attest kept <span className="font-mono-nums text-ink-100">{claims.length}</span>
              </span>
            </div>
            <div className="flex flex-wrap gap-4 text-xs">
              <div className="flex items-center gap-2">
                <span className="text-label-sm">DataHub applied</span>
                {retrieval.pushed_down.length === 0 ? (
                  <span className="text-ink-500">nothing</span>
                ) : (
                  retrieval.pushed_down.map((p) => (
                    <span
                      key={p}
                      className="px-2 py-0.5 rounded-full bg-supported/10 text-supported border border-supported/25 font-mono-nums"
                    >
                      {p}
                    </span>
                  ))
                )}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-label-sm">Attest applied</span>
                {retrieval.filtered_locally.length === 0 ? (
                  <span className="text-ink-500">nothing</span>
                ) : (
                  retrieval.filtered_locally.map((p) => (
                    <span
                      key={p}
                      className="px-2 py-0.5 rounded-full bg-insufficient/10 text-insufficient border border-insufficient/25 font-mono-nums"
                    >
                      {p}
                    </span>
                  ))
                )}
              </div>
            </div>
            <p className="text-xs text-ink-400 leading-relaxed">{retrieval.note}</p>
          </div>
        )}

        {/* The poll gave up. It says so rather than spinning forever. */}
        {gaveUp && (
          <div className="surface-card p-4 border-insufficient/25 bg-insufficient/5">
            <div className="flex items-center gap-2 text-sm text-insufficient">
              <AlertTriangle size={15} />
              <span className="font-medium">Still catching up after {POLL_ATTEMPTS} checks</span>
            </div>
            <p className="mt-1 text-xs text-ink-300">
              Attest's record says these writes landed, and the catalog has not shown them in{' '}
              {(POLL_INTERVAL_MS * POLL_ATTEMPTS) / 1000} seconds — longer than any lag measured
              on this server. Past here nobody can tell a slow index from a real problem, so
              this stops guessing and hands it to you.
            </p>
            <button onClick={() => load(applied.current)} className="btn-ghost text-xs mt-3">
              <RefreshCw size={12} /> Check again
            </button>
          </div>
        )}

        {error && (
          <div className="surface-card p-4 border-contradicted/25 bg-contradicted/5">
            <div className="flex items-center gap-2 text-sm text-contradicted">
              <AlertTriangle size={15} /> {error}
            </div>
          </div>
        )}

        {/* The claims */}
        <div className="space-y-3">
          {loading && claims.length === 0 ? (
            <div className="surface-card p-8 text-center text-sm text-ink-400">
              Reading the catalog…
            </div>
          ) : claims.length === 0 ? (
            <div className="surface-card p-8 text-center space-y-2">
              <p className="text-sm text-ink-200">No claim artifacts match this filter.</p>
              <p className="text-xs text-ink-400 max-w-md mx-auto leading-relaxed">
                That is the catalog being empty of matching claims — not a claim with no
                verdict, and not an error. A verdict reaches DataHub only when a human
                publishes it at the checkpoint.
              </p>
            </div>
          ) : (
            claims.map((claim, i) => (
              <ClaimArtifactCard
                key={claim.claim_urn}
                claim={claim}
                index={i}
                onRetry={repair}
                retrying={retrying === claim.audit_run}
              />
            ))
          )}
        </div>
      </div>
    </div>
  );
}
