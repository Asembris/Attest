import { useEffect, useId, useRef, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { AnimatePresence, motion, useReducedMotion, type Transition } from 'framer-motion';
import { Search, AlertTriangle, Loader2, X, Database } from 'lucide-react';
import type { State } from './UrnPicker';

// THE PRESENTATION HALF OF THE URN PICKER. It fetches nothing, debounces nothing and decides
// nothing: `UrnPicker` owns the search, the race guard and `searchFailed` — the policy that
// `spikes/e2e_sabotage.py` targets — and hands the result here to be drawn.
//
// A RIGHT-EDGE DRAWER, NOT AN ANCHORED POPOVER, and that is what deleted ~45 lines rather
// than tuning them. The panel used to measure its trigger, flip upward when there was less
// than 340px below it, and re-measure on every scroll and resize — all to answer "where is
// the button". At the hero's own viewport the answer was "flip up and land on top of the
// agent-output card", half-covering the textarea the user was about to paste into. An edge
// anchors itself, so the measurement, the flip threshold and both window listeners go away.
//
// The width is `42rem` deliberately: that is `max-w-2xl`, the agent-output card's exact
// width, so the two surfaces read as the same system rather than as a widget over a page.
//
// STILL A PORTAL, for the reason Session 28 found the hard way — Hero's card is
// `overflow-hidden` for its rounded corners, and a panel rendered inside it is CLIPPED OUT OF
// EXISTENCE. Measured on the committed pre-change bundle at the time: `elementFromPoint` at a
// result row returned the hero's three.js CANVAS, and the control had been dead for as long
// as it had existed. `position: fixed` in `document.body` escapes the clip.

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

/** Everything a `Tab` can land on inside the drawer. A disabled control is excluded by the
 *  selector rather than filtered afterwards, so the manual-entry Insert button drops out of
 *  the cycle exactly while it is unusable. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** What the drawer is saying right now, in one sentence, for a screen reader. The visual
 *  states are a spinner, a list and a coloured alert; none of them announce themselves. */
function announce(state: State): string {
  switch (state.kind) {
    case 'loading':
      return 'Searching the catalog';
    case 'ready':
      return state.hits.length === 0
        ? 'No matching datasets'
        : `${state.hits.length} matching datasets`;
    case 'offline':
      return `Catalog discovery is unavailable: ${state.detail}`;
    default:
      return '';
  }
}

export default function CatalogDrawer({
  open,
  id,
  q,
  onQ,
  state,
  manual,
  onManual,
  onPick,
  onClose,
}: {
  open: boolean;
  /** Shared with the trigger's `aria-controls`, so the button points at this dialog. */
  id: string;
  q: string;
  onQ: (q: string) => void;
  state: State;
  manual: string;
  onManual: (v: string) => void;
  onPick: (urn: string) => void;
  /** Dismissal — Escape, the scrim, or the close button. Picking a URN does NOT come through
   *  here; `UrnPicker` restores focus to the trigger on dismissal only, and a pick has to
   *  leave the caret in the textarea instead. */
  onClose: () => void;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const reduce = useReducedMotion();

  // The same curve every entrance in Hero uses. Reduced motion collapses it to a cut rather
  // than to a slower slide — the preference asks for no movement, not for gentler movement.
  const slide: Transition = { duration: reduce ? 0 : 0.32, ease: [0.2, 0.7, 0.2, 1] };
  const fade: Transition = { duration: reduce ? 0 : 0.2, ease: 'linear' };

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Lock the page behind the drawer, and pay back the scrollbar's width so the hero does not
  // jump sideways as it disappears. Fixed elements are positioned to the viewport, so the
  // replay banner is untouched by the padding.
  useEffect(() => {
    if (!open) return;
    const { body } = document;
    const gap = window.innerWidth - document.documentElement.clientWidth;
    const overflow = body.style.overflow;
    const padding = body.style.paddingRight;
    body.style.overflow = 'hidden';
    if (gap > 0) body.style.paddingRight = `${gap}px`;
    return () => {
      body.style.overflow = overflow;
      body.style.paddingRight = padding;
    };
  }, [open]);

  /** Escape closes; Tab cycles WITHIN the drawer. `aria-modal` is a promise to a screen
   *  reader that the rest of the page is inert, and the DOM has to keep it. */
  function onKeyDown(e: KeyboardEvent<HTMLElement>) {
    if (e.key === 'Escape') {
      onClose();
      return;
    }
    if (e.key !== 'Tab') return;
    const nodes = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (nodes.length === 0) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  return createPortal(
    <>
      <AnimatePresence>
        {open && (
          <motion.div
            key="scrim"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={fade}
            onClick={onClose}
            aria-hidden
            className="fixed inset-0 z-[60] bg-ink-950/70 backdrop-blur-[2px]"
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {open && (
          <motion.aside
            key="drawer"
            ref={panelRef}
            id={id}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            onKeyDown={onKeyDown}
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={slide}
            // `top` follows the replay banner when there is one. `replay.css` shifts `body`,
            // `min-h-screen` and the two `sticky top-0` headers; a `fixed` drawer is a fourth
            // case it does not cover, and the variable is undefined in the production bundle
            // so the fallback makes this line inert there.
            style={{ top: 'var(--replay-banner-h, 0px)' }}
            className="fixed right-0 bottom-0 z-[61] flex w-[min(42rem,100vw)] flex-col border-l border-ink-700/60 bg-ink-900/95 backdrop-blur-xl shadow-[0_0_80px_-10px_rgba(0,0,0,0.9)]"
          >
            <header className="flex items-center justify-between gap-4 px-5 py-4 border-b border-ink-700/50">
              <div className="flex items-center gap-2">
                <Database size={13} className="text-ink-400" />
                <h2 id={titleId} className="text-label-sm">
                  Search your catalog
                </h2>
              </div>
              <button
                type="button"
                onClick={onClose}
                aria-label="Close catalog search"
                className="p-1.5 -mr-1.5 rounded-lg text-ink-400 hover:text-ink-100 hover:bg-ink-800 transition-colors"
              >
                <X size={16} />
              </button>
            </header>

            <div className="flex items-center gap-2.5 px-5 py-3.5 border-b border-ink-700/50">
              {state.kind === 'loading' ? (
                <Loader2 size={15} className="text-ink-400 animate-spin shrink-0" />
              ) : (
                <Search size={15} className="text-ink-400 shrink-0" />
              )}
              <input
                ref={inputRef}
                value={q}
                onChange={(e) => onQ(e.target.value)}
                placeholder="Search by dataset name…"
                aria-label="Search the catalog"
                className="w-full bg-transparent text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none"
              />
            </div>

            <p role="status" aria-live="polite" className="sr-only">
              {announce(state)}
            </p>

            <div className="flex-1 overflow-y-auto overscroll-contain">
              {state.kind === 'offline' ? (
                // THE OUTAGE, VISIBLE. Never a silent swap for a baked-in list: "we could not
                // ask" and "your catalog has nothing like that" are different facts, and only
                // one of them is true here.
                <div className="px-5 py-5 space-y-4">
                  <div className="flex items-start gap-2.5 text-sm text-contradicted">
                    <AlertTriangle size={15} className="mt-0.5 shrink-0" />
                    <div>
                      <div className="font-medium">
                        Catalog discovery is {state.permanent ? 'not available' : 'offline'}
                      </div>
                      <div className="text-xs text-ink-400 mt-1.5 leading-relaxed">
                        {state.detail}
                      </div>
                    </div>
                  </div>
                  <div className="text-xs text-ink-400 leading-relaxed">
                    Nothing about an audit depends on discovery — paste or type a dataset URN and
                    Attest will verify claims about it against the catalog exactly as usual.
                  </div>
                  <div className="flex items-center gap-2 pt-1">
                    <input
                      value={manual}
                      onChange={(e) => onManual(e.target.value)}
                      placeholder="urn:li:dataset:(urn:li:dataPlatform:…,…,PROD)"
                      aria-label="Dataset URN"
                      className="flex-1 min-w-0 bg-ink-900/70 border border-ink-700/60 rounded-lg px-2.5 py-2 text-xs font-mono-nums text-ink-100 placeholder:text-ink-600 focus:outline-none focus:border-ink-500"
                    />
                    <button
                      type="button"
                      disabled={!manual.trim()}
                      onClick={() => onPick(manual.trim())}
                      className="btn-ghost text-xs px-3 py-2 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      Insert
                    </button>
                  </div>
                </div>
              ) : (
                <ul className="py-1.5">
                  {state.kind === 'ready' &&
                    state.hits.map((d) => (
                      <li key={d.urn}>
                        <button
                          type="button"
                          onClick={() => onPick(d.urn)}
                          className="w-full text-left px-5 py-2.5 hover:bg-ink-800/70 focus:bg-ink-800/70 focus:outline-none transition-colors"
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
                    <li className="px-5 py-4 text-xs text-ink-400">
                      Your catalog has no dataset matching “{q}”.
                    </li>
                  )}
                  {state.kind === 'loading' && (
                    <li className="px-5 py-4 text-xs text-ink-500">Searching the catalog…</li>
                  )}
                </ul>
              )}
            </div>

            {/* ADVISORY, said where the results are — not in a tooltip. */}
            <div className="px-5 py-3 border-t border-ink-700/50 text-[11px] leading-relaxed text-ink-500">
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
          </motion.aside>
        )}
      </AnimatePresence>
    </>,
    document.body,
  );
}
