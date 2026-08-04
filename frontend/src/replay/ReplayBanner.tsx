import { Radio } from 'lucide-react';
import { replayManifest } from '../api/replayClient';

// THE BANNER, AND IT IS NOT A DISCLAIMER — it is the product's own thesis applied to itself.
//
// Attest exists to say that an unverified claim is not a verified one. A replay that let a
// reader believe they were driving a live system would be making exactly the kind of
// unfounded claim it was built to catch, on its own front page. So this is fixed, on every
// view, on every scroll position, with no dismiss control anywhere in the tree.
//
// It says "A REAL RUN'S committed record", never "the demo run's": the video records a
// DIFFERENT run against the same catalog, and claiming they are one would be a small lie of
// exactly the shape this whole page exists to refuse.

function captureDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' });
}

export default function ReplayBanner() {
  return (
    <div
      role="note"
      className="replay-banner fixed top-0 left-0 right-0 z-[100] border-b border-accent/35 backdrop-blur-md"
      style={{ background: 'linear-gradient(180deg, rgba(110,143,191,0.20), rgba(10,11,13,0.94))' }}
    >
      <div className="px-4 sm:px-6 py-2 flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1.5 font-mono-nums text-[10.5px] tracking-[0.22em] uppercase text-accent-400 shrink-0">
          <Radio size={11} />
          Replay
        </span>
        <span className="text-[12.5px] text-ink-100 leading-snug">
          A real run's committed record, captured {captureDate(replayManifest.captured_at)} against
          DataHub Core {replayManifest.datahub_core_version}.{' '}
          <span className="text-ink-50 font-medium">Nothing here is live.</span>
        </span>
        <span className="font-mono-nums text-[10.5px] text-ink-400 leading-snug basis-full sm:basis-auto sm:ml-auto">
          run {replayManifest.run_id.slice(0, 8)} · every response is verbatim off the wire ·
          "View in DataHub" points at the local catalog that produced this record
        </span>
      </div>
    </div>
  );
}
