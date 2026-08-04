import { useState, useRef, useEffect, useCallback, useLayoutEffect } from 'react';
import { createPortal } from 'react-dom';
import { Database, ChevronDown, Search, AlertTriangle, Loader2 } from 'lucide-react';
import { searchCatalog, ApiError } from '../api/client';
import type { CatalogHit } from '../api/types';

// LIVE SEARCH over the DataHub MCP Server. Picking a candidate inserts its URN into the
// agent-output textarea at the cursor.
//
// **MCP discovers. A human resolves. GraphQL verifies. Deterministic code decides.** The only
// value that leaves this component is the URN string, and it goes into a textarea a person
// then edits and submits — the backend requires it to appear verbatim in the agent output
// (schemas.py) and the decomposer may quote a URN but never mint one. So a wrong pick yields
// claims about an explicitly wrong URN rather than a silent resolution error, which is the
// distinction CLAUDE.md §4 turns on.
//
// It used to filter a STATIC list generated from seed/ground_truth.json — the seeded catalog,
// baked into the bundle. That is useless against any real DataHub, and worse, it described a
// catalog rather than reading one.
//
// TWO RULES HERE, and neither is cosmetic:
//
//   1. **A FAILURE IS NEVER A FALLBACK.** If discovery is down or not installed, this shows a
//      visible offline state and offers manual URN entry. It never quietly substitutes a
//      hardcoded list: a stale list shown in place of an outage is a UI asserting something
//      nobody checked, which is precisely what this product exists to catch. `just
//      e2e-sabotage` reintroduces exactly that fallback and requires the E2E to go red.
//
//   2. **SEARCH SUCCEEDING IS NOT VERIFICATION SUCCEEDING.** These candidates are labelled
//      advisory wherever they appear. Nothing here has been checked against the catalog by a
//      checker; the audit is what does that, afterwards, over GraphQL.
//
// AND THE PANEL IS A PORTAL, WHICH IS A BUG FIX RATHER THAN A STYLE CHOICE. It used to be an
// `absolute` child of this component, inside Hero's textarea card — and that card is
// `overflow-hidden` (for its rounded corners), while the panel opens UPWARD, out of the card.
// So the browser clipped essentially all of it: measured on the committed pre-change bundle,
// the panel's box was top=100..bottom=395 with the clipping card starting at top=392, and
// `document.elementFromPoint` at a result row returned the hero's three.js CANVAS. **Clicking
// "Insert seeded URN" appeared to do nothing at all, and had for as long as the control had
// existed** — no test had ever opened it, which is §14's lesson arriving a third time. The
// browser E2E added in this session found it on its first run. A portal into `document.body`
// with `position: fixed` escapes the clip without touching the card's design.

const DEBOUNCE_MS = 250;
/** Space the panel needs above the trigger before it opens upward instead of downward. */
const PANEL_MAX_H = 340;

/** The platform, PARSED from the URN — not taken from the search response.
 *
 *  The MCP search fragment returns a Dataset's `urn` and `properties.name` and nothing else,
 *  so there is no platform field to trust. A URN is a canonical identifier and reading it is
 *  lossless, which is the same thing ClaimArtifactCard does with a dataset URN. */
function platformOf(urn: string): string {
  return urn.match(/urn:li:dataPlatform:([^,)]+)/)?.[1] ?? '';
}

/** The dataset name inside the URN, for when the catalog has no display name for it. */
function pathOf(urn: string): string {
  return urn.split(',')[1] ?? urn;
}

type State =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'ready'; hits: CatalogHit[]; total: number; transport: string }
  | { kind: 'offline'; detail: string; permanent: boolean };

/** What a FAILED search becomes. **This function is where the fallback must never go.**
 *
 *  Returning a baked-in list of URNs from here would turn an outage into a plausible-looking
 *  answer — the exact collapse Attest exists to catch, committed by Attest's own UI. It is a
 *  named function precisely so the regression is a one-line swap, which is how
 *  `spikes/e2e_sabotage.py` re-introduces it and demands the browser E2E go red.
 *
 *  501 and 503 are different facts: discovery not being installed on this deployment cannot
 *  be waited out, and telling someone to try again would waste their time. */
function searchFailed(err: unknown): State {
  const api = err instanceof ApiError ? err : null;
  return {
    kind: 'offline',
    detail: api?.detail ?? String(err),
    permanent: api?.status === 501,
  };
}

export default function UrnPicker({ onPick }: { onPick: (urn: string) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [state, setState] = useState<State>({ kind: 'idle' });
  const [manual, setManual] = useState('');
  const boxRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // Where to put the portalled panel: measured from the trigger, in viewport coordinates.
  const [at, setAt] = useState<{ right: number; top?: number; bottom?: number } | null>(null);
  // Monotonic request id: a slow response for an older query must never overwrite a newer
  // one's results. Debouncing reduces the races; it does not remove them.
  const seq = useRef(0);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      const target = e.target as Node;
      // BOTH refs: the panel is portalled into document.body, so it is no longer a DOM
      // descendant of `boxRef`. Checking only the trigger would close the panel on the very
      // click that was trying to use it.
      if (boxRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  // Anchor the panel to the trigger, in VIEWPORT coordinates, and keep it there. Measured in
  // a layout effect so the first paint is already in the right place rather than jumping, and
  // re-measured on scroll/resize because `fixed` does not follow the trigger on its own.
  useLayoutEffect(() => {
    if (!open) return;
    function place() {
      const r = boxRef.current?.getBoundingClientRect();
      if (!r) return;
      const above = r.top;
      setAt(
        above >= PANEL_MAX_H
          ? { right: window.innerWidth - r.right, bottom: window.innerHeight - r.top + 8 }
          : { right: window.innerWidth - r.right, top: r.bottom + 8 },
      );
    }
    place();
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const run = useCallback(async (text: string) => {
    const mine = ++seq.current;
    setState({ kind: 'loading' });
    try {
      const found = await searchCatalog(text, 12);
      if (seq.current !== mine) return;
      setState({
        kind: 'ready',
        hits: found.hits,
        total: found.total,
        transport: found.transport,
      });
    } catch (err) {
      if (seq.current !== mine) return;
      setState(searchFailed(err));
    }
  }, []);

  // Debounced live search, and it runs on OPEN too (with an empty query, which lists the
  // catalog) so the panel is never a blank box waiting to be typed into.
  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => void run(q), q ? DEBOUNCE_MS : 0);
    return () => clearTimeout(timer);
  }, [open, q, run]);

  function pick(urn: string) {
    onPick(urn);
    setOpen(false);
    setQ('');
  }

  return (
    <div ref={boxRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 text-xs text-ink-300 hover:text-ink-100 transition-colors"
      >
        <Database size={12} />
        Insert dataset URN
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && at && createPortal(
        // PORTALLED INTO document.body. Rendered here as an `absolute` child it was clipped
        // out of existence by Hero's `overflow-hidden` card (see the note at the top of this
        // file) — the control looked dead. `position: fixed`, anchored to the trigger.
        <div
          ref={panelRef}
          style={{ position: 'fixed', right: at.right, top: at.top, bottom: at.bottom }}
          className="z-50 w-[min(32rem,84vw)] surface-card bg-ink-850 shadow-2xl overflow-hidden"
        >
          <div className="flex items-center gap-2 px-3 py-2 border-b border-ink-700/50">
            {state.kind === 'loading' ? (
              <Loader2 size={13} className="text-ink-400 animate-spin" />
            ) : (
              <Search size={13} className="text-ink-400" />
            )}
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search your catalog by dataset name…"
              aria-label="Search the catalog"
              className="w-full bg-transparent text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none"
            />
          </div>

          {state.kind === 'offline' ? (
            // THE OUTAGE, VISIBLE. Never a silent swap for a baked-in list: "we could not
            // ask" and "your catalog has nothing like that" are different facts, and only
            // one of them is true here.
            <div className="px-3 py-3 space-y-2.5">
              <div className="flex items-start gap-2 text-xs text-contradicted">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <div>
                  <div className="font-medium">
                    Catalog discovery is {state.permanent ? 'not available' : 'offline'}
                  </div>
                  <div className="text-ink-400 mt-1 leading-relaxed">{state.detail}</div>
                </div>
              </div>
              <div className="text-xs text-ink-400 leading-relaxed">
                Nothing about an audit depends on discovery — paste or type a dataset URN and
                Attest will verify claims about it against the catalog exactly as usual.
              </div>
              <div className="flex items-center gap-2 pt-1">
                <input
                  value={manual}
                  onChange={(e) => setManual(e.target.value)}
                  placeholder="urn:li:dataset:(urn:li:dataPlatform:…,…,PROD)"
                  aria-label="Dataset URN"
                  className="flex-1 min-w-0 bg-ink-900/70 border border-ink-700/60 rounded px-2 py-1.5 text-xs font-mono-nums text-ink-100 placeholder:text-ink-600 focus:outline-none focus:border-ink-500"
                />
                <button
                  type="button"
                  disabled={!manual.trim()}
                  onClick={() => {
                    pick(manual.trim());
                    setManual('');
                  }}
                  className="btn-ghost text-xs disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Insert
                </button>
              </div>
            </div>
          ) : (
            <>
              <ul className="max-h-64 overflow-y-auto py-1">
                {state.kind === 'ready' &&
                  state.hits.map((d) => (
                    <li key={d.urn}>
                      <button
                        type="button"
                        onClick={() => pick(d.urn)}
                        className="w-full text-left px-3 py-2 hover:bg-ink-800/70 transition-colors"
                      >
                        <div className="text-sm text-ink-100 font-mono-nums truncate">
                          {d.name || pathOf(d.urn)}
                        </div>
                        <div className="text-xs text-ink-400 truncate">
                          {platformOf(d.urn) || 'dataset'} · {pathOf(d.urn)}
                        </div>
                      </button>
                    </li>
                  ))}
                {state.kind === 'ready' && state.hits.length === 0 && (
                  // A REAL empty answer: the catalog was asked and matched nothing. It says
                  // so in those words, because the failure case above says something else.
                  <li className="px-3 py-3 text-xs text-ink-400">
                    Your catalog has no dataset matching “{q}”.
                  </li>
                )}
                {state.kind === 'loading' && (
                  <li className="px-3 py-3 text-xs text-ink-500">Searching the catalog…</li>
                )}
              </ul>
              {/* ADVISORY, said where the results are — not in a tooltip. */}
              <div className="px-3 py-2 border-t border-ink-700/50 text-[11px] leading-relaxed text-ink-500">
                {state.kind === 'ready' && state.transport ? (
                  <>
                    via <span className="text-ink-400">{state.transport}</span> — advisory.
                    {state.total > state.hits.length && (
                      <> Showing {state.hits.length} of {state.total}.</>
                    )}{' '}
                  </>
                ) : null}
                Discovery only: nothing here is verified. Attest checks the claim you write
                against the catalog itself.
              </div>
            </>
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}
