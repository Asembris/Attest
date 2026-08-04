import { useState, useRef, useEffect, useCallback, useId } from 'react';
import { Database, ChevronDown } from 'lucide-react';
import { searchCatalog, ApiError } from '../api/client';
import type { CatalogHit } from '../api/types';
import CatalogDrawer from './CatalogDrawer';

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
// THIS FILE IS THE POLICY HALF: the trigger, the search, the race guard and `searchFailed`.
// Drawing the result is `CatalogDrawer`, which fetches nothing and decides nothing — the split
// keeps the rules above in one small module, and it is the module the sabotage edits.

const DEBOUNCE_MS = 250;

/** What the drawer is showing. `ready` is the ONLY state that can carry candidates, which is
 *  what makes `searchFailed` returning anything else a one-line, visible regression. */
export type State =
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
  const drawerId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Monotonic request id: a slow response for an older query must never overwrite a newer
  // one's results. Debouncing reduces the races; it does not remove them.
  const seq = useRef(0);

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
  // catalog) so the drawer is never a blank box waiting to be typed into.
  useEffect(() => {
    if (!open) return;
    const timer = setTimeout(() => void run(q), q ? DEBOUNCE_MS : 0);
    return () => clearTimeout(timer);
  }, [open, q, run]);

  /** Closing resets the query, so re-opening never flashes the previous search's results
   *  before the new one lands.
   *
   *  `restoreFocus` is the whole reason this takes an argument. On DISMISSAL (Escape, the
   *  scrim, the close button) focus belongs back on the trigger — the drawer is gone and
   *  focus would otherwise fall to `<body>`. On a PICK it emphatically does not: `insertUrn`
   *  puts the caret back in the textarea just after the inserted URN, and stealing it back
   *  here would undo the one thing the pick was for. */
  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    setQ('');
    setManual('');
    setState({ kind: 'idle' });
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  function pick(urn: string) {
    onPick(urn);
    close(false);
  }

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => (open ? close(true) : setOpen(true))}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={drawerId}
        className="inline-flex items-center gap-1.5 text-xs text-ink-300 hover:text-ink-100 transition-colors"
      >
        <Database size={12} />
        Insert dataset URN
        <ChevronDown size={12} className={`transition-transform ${open ? '-rotate-90' : ''}`} />
      </button>

      <CatalogDrawer
        open={open}
        id={drawerId}
        q={q}
        onQ={setQ}
        state={state}
        manual={manual}
        onManual={setManual}
        onPick={pick}
        onClose={() => close(true)}
      />
    </>
  );
}
