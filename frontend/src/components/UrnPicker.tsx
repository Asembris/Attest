import { useState, useRef, useEffect } from 'react';
import { Database, ChevronDown, Search } from 'lucide-react';
import { seededDatasets } from '../data/catalog';

// Phase 6: autocomplete over the REAL seeded URNs (catalog.ts, generated from
// ground_truth.json), so a demo-driver never has to hand-type a 90-character dataset URN.
// Picking one inserts it into the agent-output textarea at the cursor; the URNs offered are
// exactly the ones the live backend can resolve.

export default function UrnPicker({ onPick }: { onPick: (urn: string) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const boxRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    function onDoc(e: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const s = q.trim().toLowerCase();
  const matches = seededDatasets.filter(
    (d) =>
      !s ||
      d.name.toLowerCase().includes(s) ||
      d.urn.toLowerCase().includes(s) ||
      (d.owner ?? '').toLowerCase().includes(s),
  );

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
        Insert seeded URN
        <ChevronDown size={12} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute z-30 bottom-full mb-2 right-0 w-[min(28rem,80vw)] surface-card bg-ink-850 shadow-2xl overflow-hidden">
          <div className="flex items-center gap-2 px-3 py-2 border-b border-ink-700/50">
            <Search size={13} className="text-ink-400" />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Filter by name, owner, or URN…"
              className="w-full bg-transparent text-sm text-ink-100 placeholder:text-ink-500 focus:outline-none"
            />
          </div>
          <ul className="max-h-64 overflow-y-auto py-1">
            {matches.map((d) => (
              <li key={d.urn}>
                <button
                  type="button"
                  onClick={() => pick(d.urn)}
                  className="w-full text-left px-3 py-2 hover:bg-ink-800/70 transition-colors"
                >
                  <div className="text-sm text-ink-100 font-mono-nums truncate">{d.name}</div>
                  <div className="text-xs text-ink-400">
                    {d.platform}
                    {d.owner ? ` · owner ${d.owner}` : ' · unowned'}
                  </div>
                </button>
              </li>
            ))}
            {matches.length === 0 && (
              <li className="px-3 py-3 text-xs text-ink-400">No seeded dataset matches “{q}”.</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
