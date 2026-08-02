import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowLeft, Database, RefreshCw, Search } from 'lucide-react';
import { ApiError, listClaims, retryWriteback } from '../api/client';
import type { ClaimFilters, ClaimsResponse, ClaimType, Verdict } from '../api/types';
import { seededDatasets } from '../data/catalog';
import ClaimArtifactCard from './ClaimArtifactCard';
import { Reveal } from './reveal';

// WHAT THE NEXT AGENT INHERITS — the inheritance half of Challenge 1's thesis.
//
// Everything on this screen is READ FROM DATAHUB. Not from Attest's audit history: an
// explorer backed by SQLite would prove nothing about what a second party can retrieve,
// because a second party cannot open Attest's database. The one thing Attest's store
// contributes is an explanation for an ABSENT verdict — which the catalog genuinely cannot
// supply — and it can never add a claim, remove one, or change a verdict.
//
// The push-down disclosure is not a debug panel. "Retrievable from DataHub" is true; "fully
// queryable in DataHub" is not, and a UI that showed a filtered list without saying who
// filtered it would let a reader believe the catalog answered a question Attest answered.
// That is an unfounded claim about where evidence came from, rendered by the tool built to
// catch unfounded claims about where evidence came from. The disclosure comes from the API's
// own `retrieval` object — it is NEVER computed client-side (the design-pass mockup did that,
// which the browser cannot do truthfully).

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

const SELECT_CLS =
  'mt-2 w-full bg-ink-800 border border-ink-600/50 rounded-lg px-3 py-2.5 text-sm text-ink-100 focus:border-accent/50 focus:outline-none';

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

  // WHICH REQUEST IS THE LATEST. Reads land in whatever order the server finishes them, and
  // this screen issues two in quick succession on the most ordinary path there is: it loads
  // UNFILTERED on mount (`searchAcrossEntities`, catalog-wide) and a human picks a dataset a
  // moment later (`dataset.assertions`, scoped). The scoped read is much the faster of the
  // two, so the slower catalog-wide response routinely lands SECOND — and, unguarded,
  // overwrote the answer to the question that was actually asked.
  //
  // That is not a cosmetic race here. `applied.current` is set when a request goes OUT, so
  // the "Where this was filtered" panel would describe the scoped query while the rows on
  // screen came from the catalog-wide one: the disclosure asserting DataHub scoped a result
  // it did not scope. That panel exists precisely so a reader is never misled about which
  // predicates the catalog applied, and this is the one way it could lie.
  //
  // MEASURED through the browser E2E: 2 of 3 runs rendered `searchAcrossEntities` results
  // while the dropdown named a dataset. So a stale response is DISCARDED — the newest
  // request always wins, whatever order the network answers in.
  const latest = useRef(0);

  const load = useCallback(async (next: ClaimFilters, resetPolls = true) => {
    const seq = ++latest.current;
    setLoading(true);
    setError(null);
    applied.current = next;
    try {
      const body = await listClaims(next);
      if (seq !== latest.current) return; // superseded — never render an older answer
      setData(body);
      if (resetPolls) setPollsLeft(POLL_ATTEMPTS);
    } catch (e) {
      if (seq !== latest.current) return; // a superseded FAILURE is not this query's error
      setError(e instanceof ApiError ? e.detail : 'Could not reach Attest.');
    } finally {
      // Only the newest request owns the spinner; a stale one finishing must not clear it
      // while the current read is still in flight.
      if (seq === latest.current) setLoading(false);
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
  // The catalog's own total for this entry point's scope. When it exceeds what this page
  // considered, the listing was TRUNCATED at the limit — surfaced from retrieval.total, so a
  // reader never mistakes "not on this page" for "not in the catalog".
  const truncated = retrieval ? retrieval.total > retrieval.considered : false;

  return (
    <div
      className="min-h-screen"
      style={{ background: 'radial-gradient(140% 90% at 50% -10%, #101216 0%, #0A0B0D 46%, #08090B 100%)' }}
    >
      <header className="sticky top-0 z-30 border-b border-ink-700/40 bg-ink-950/75 backdrop-blur-md">
        <div className="max-w-[1000px] mx-auto px-6 lg:px-10 py-4 flex items-center gap-3.5">
          <button onClick={onBack} className="btn-ghost text-sm px-2">
            <ArrowLeft size={16} />
          </button>
          <span className="font-serif text-xl font-semibold tracking-tight">Attest</span>
          <span className="text-ink-500">/</span>
          <span className="text-sm text-ink-300">Published claims</span>
          <button
            onClick={() => load(applied.current)}
            disabled={loading}
            className="ml-auto btn-ghost text-sm disabled:opacity-40"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
            Refresh
          </button>
        </div>
      </header>

      <main className="max-w-[1000px] mx-auto px-6 lg:px-10 pb-32">
        {/* ===== OPENING ===== */}
        <section className="pt-20 pb-12">
          <Reveal className="flex items-center gap-2.5 font-mono-nums text-[11px] tracking-[0.24em] uppercase text-accent mb-6">
            <span className="w-1.5 h-1.5 rounded-full bg-accent shadow-[0_0_10px_#6E8FBF]" />
            Read live from the DataHub catalog
          </Reveal>
          <Reveal delay={0.08}>
            <h1 className="font-serif font-normal leading-[0.96] text-[clamp(44px,7vw,88px)] max-w-[15ch]">
              What the next agent inherits
            </h1>
          </Reveal>
          <Reveal delay={0.16}>
            <p className="mt-6 max-w-[64ch] text-base lg:text-[17px] font-light leading-relaxed text-ink-300">
              Every claim below is read out of <span className="text-ink-50">DataHub</span> — not out
              of Attest's audit history. One durable artifact per claim, carrying what was asserted,
              at what grain, and every verdict it has ever had. This is exactly what a second agent
              gets from the catalog <span className="text-ink-50">with no Attest process running</span>.
            </p>
          </Reveal>
        </section>

        {/* ===== FILTERS ===== */}
        <Reveal className="rounded-2xl border border-ink-700/40 p-6 bg-ink-800/[0.15]">
          <div className="flex items-center gap-2 text-label-sm mb-5">
            <Search size={11} /> Filter the catalog read
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <label className="block">
              <span className="text-label-sm">Dataset</span>
              <select
                value={filters.target_urn ?? ''}
                onChange={(e) => update({ target_urn: e.target.value })}
                className={SELECT_CLS}
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
                className={SELECT_CLS}
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
                className={SELECT_CLS}
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
                className={`${SELECT_CLS} placeholder:text-ink-500`}
              />
            </label>
          </div>
        </Reveal>

        {/* WHERE EACH PREDICATE WAS APPLIED. First-class, from the API's own retrieval object. */}
        {retrieval && (
          <Reveal className="mt-4" y={16}>
            <div
              className="rounded-2xl p-6 border border-accent/[0.28]"
              style={{ background: 'linear-gradient(180deg, rgba(110,143,191,0.055), rgba(110,143,191,0.015))' }}
            >
              <div className="flex items-center gap-2 text-label-sm text-accent mb-4">
                <Database size={11} /> Where this was filtered
              </div>
              <div className="flex flex-wrap items-center gap-2.5 font-mono-nums text-xs mb-4">
                <span className="text-ink-300">{retrieval.entry_point}</span>
                <span className="text-ink-600">·</span>
                <span className="text-ink-300">
                  DataHub returned <span className="text-ink-50">{retrieval.considered}</span>, Attest
                  kept <span className="text-ink-50">{claims.length}</span>
                </span>
                {truncated && (
                  <>
                    <span className="text-ink-600">·</span>
                    <span className="px-2 py-0.5 rounded-full bg-insufficient/12 text-insufficient border border-insufficient/30">
                      TRUNCATED · catalog holds {retrieval.total}
                    </span>
                  </>
                )}
              </div>
              <div className="flex flex-wrap gap-x-7 gap-y-3 mb-4">
                <div className="flex items-center gap-2.5 flex-wrap">
                  <span className="text-label-sm">DataHub applied</span>
                  {retrieval.pushed_down.length === 0 ? (
                    <span className="text-ink-500 text-xs">nothing</span>
                  ) : (
                    retrieval.pushed_down.map((p) => (
                      <span
                        key={p}
                        className="px-2.5 py-0.5 rounded-full bg-accent/15 text-accent-400 border border-accent/35 font-mono-nums text-[11.5px]"
                      >
                        {p}
                      </span>
                    ))
                  )}
                </div>
                <div className="flex items-center gap-2.5 flex-wrap">
                  <span className="text-label-sm">Attest applied</span>
                  {retrieval.filtered_locally.length === 0 ? (
                    <span className="text-ink-500 text-xs">nothing</span>
                  ) : (
                    retrieval.filtered_locally.map((p) => (
                      <span
                        key={p}
                        className="px-2.5 py-0.5 rounded-full bg-insufficient/13 text-insufficient border border-insufficient/[0.32] font-mono-nums text-[11.5px]"
                      >
                        {p}
                      </span>
                    ))
                  )}
                </div>
              </div>
              <p className="text-xs text-ink-400 leading-relaxed max-w-[82ch]">{retrieval.note}</p>
            </div>
          </Reveal>
        )}

        {/* The poll gave up. It says so rather than spinning forever. */}
        {gaveUp && (
          <div className="mt-4 rounded-2xl p-5 border border-insufficient/25 bg-insufficient/5">
            <div className="flex items-center gap-2 text-sm text-insufficient">
              <AlertTriangle size={15} />
              <span className="font-medium">Still catching up after {POLL_ATTEMPTS} checks</span>
            </div>
            <p className="mt-1 text-xs text-ink-300 leading-relaxed max-w-[80ch]">
              Attest's record says these writes landed, and the catalog has not shown them in{' '}
              {(POLL_INTERVAL_MS * POLL_ATTEMPTS) / 1000} seconds — longer than any lag measured on
              this server. Past here nobody can tell a slow index from a real problem, so this stops
              guessing and hands it to you.
            </p>
            <button onClick={() => load(applied.current)} className="btn-ghost text-xs mt-3">
              <RefreshCw size={12} /> Check again
            </button>
          </div>
        )}

        {error && (
          <div className="mt-4 rounded-2xl p-5 border border-contradicted/25 bg-contradicted/5">
            <div className="flex items-center gap-2 text-sm text-contradicted">
              <AlertTriangle size={15} /> {error}
            </div>
          </div>
        )}

        {/* The claims */}
        <div className="mt-7 flex flex-col gap-3">
          {loading && claims.length === 0 ? (
            <div className="rounded-2xl border border-ink-700/40 p-10 text-center text-sm text-ink-400">
              Reading the catalog…
            </div>
          ) : claims.length === 0 ? (
            <div className="rounded-2xl border border-ink-700/40 p-11 text-center space-y-2">
              <p className="text-sm text-ink-200">No claim artifacts match this filter.</p>
              <p className="text-xs text-ink-400 max-w-[52ch] mx-auto leading-relaxed">
                That is the catalog being empty of matching claims — not a claim with no verdict, and
                not an error. A verdict reaches DataHub only when a human publishes it at the
                checkpoint.
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

        <div className="text-center mt-14">
          <button onClick={onBack} className="btn-ghost text-sm">
            <ArrowLeft size={15} />
            Back to Attest
          </button>
        </div>
      </main>
    </div>
  );
}
