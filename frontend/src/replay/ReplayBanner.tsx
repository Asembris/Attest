import { useEffect, useRef } from 'react';
import { AlertTriangle, Radio } from 'lucide-react';
import { replayManifest } from '../api/replayClient';

// THE BANNER, AND IT IS NOT A DISCLAIMER — it is the product's own thesis applied to itself.
//
// Attest exists to say that an unverified claim is not a verified one. A replay that let a
// reader believe they were driving a live system would be making exactly the kind of
// unfounded claim it was built to catch, on its own front page. So this is fixed, on every
// view, on every scroll position, with no dismiss control anywhere in the tree.
//
// IT LEADS WITH WHAT THE PAGE IS AND DEMOTES THE PROVENANCE. A judge needs "recorded, real,
// not live" in the first clause; the run id and the Core version answer a question they have
// not asked yet, so they sit in the dim second row.
//
// AND IT NEVER SAYS "seeded", "sample" or "demo data". This is a real audit against a real
// catalog — that is the entire reason a replay of it is worth anything, and copy implying
// otherwise would give away the only thing that distinguishes this from a mockup. It says
// "a recorded audit", never "the demo run's": the video records a DIFFERENT run against the
// same catalog, and eliding the two would be a small lie of exactly the shape this page
// exists to refuse.

const MONTHS = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
];

/** `2026-08-04T12:34:16+00:00` -> `4 August 2026`. ALWAYS English, always this shape.
 *
 *  NOT `toLocaleDateString`, which renders in the VIEWER's locale — a French browser showed
 *  "4 août 2026" in an otherwise English banner. And not `new Date(...)` either: that shifts
 *  the calendar day across timezones, so a reader far enough east or west would be told this
 *  was captured on a date it was not. The recorded timestamp is a fact about the capture, so
 *  it is read off the string rather than reinterpreted per reader. */
function captureDate(iso: string): string {
  const parts = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!parts) return iso;
  const [, year, month, day] = parts;
  return `${Number(day)} ${MONTHS[Number(month) - 1] ?? month} ${year}`;
}

// The repository this recording was produced in. Hardcoded because there is nothing to derive
// it from — `git remote` is not a fact a static bundle carries — and because a wrong value is
// visible the moment anyone follows the link. Shape verified against the live host: a 40-char
// SHA under `/commit/` resolves, and the short form in the label is the same commit.
const REPO_URL = 'https://github.com/Asembris/Attest';

export default function ReplayBanner() {
  const ref = useRef<HTMLDivElement>(null);
  // THE PROVENANCE THE HASHES CANNOT COVER. `manifest.files` proves the recorded responses
  // have not been edited since capture; it says nothing about which code produced them. That
  // is this commit — so if the capture ran against a tree that did not match it, the SHA
  // beside the hashes is approximate, and a reader has no way to tell. `capture_replay.py`
  // refuses such a capture and a test refuses to ship one, but if either is ever bypassed the
  // page has to say so ITSELF rather than let the link imply a provenance nobody verified.
  const dirty = replayManifest.capturing_commit_dirty;

  // THE BANNER PUBLISHES ITS OWN HEIGHT, and that is not polish. `replay.css` uses
  // `--replay-banner-h` to push the page down and to drop the two `sticky top-0` headers
  // below the banner. Hard-coding that height means guessing how this copy wraps at every
  // viewport width, and guessing low hides a sticky header behind the banner at exactly the
  // widths nobody checked. Measuring it removes the guess, and keeps it correct if the copy
  // is ever edited again. The CSS keeps a static fallback for the first paint.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const publish = () =>
      document.documentElement.style.setProperty(
        '--replay-banner-h',
        `${Math.ceil(el.getBoundingClientRect().height)}px`,
      );
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={ref}
      role="note"
      className="replay-banner fixed top-0 left-0 right-0 z-[100] border-b border-accent/35 backdrop-blur-md"
      style={{ background: 'linear-gradient(180deg, rgba(110,143,191,0.20), rgba(10,11,13,0.94))' }}
    >
      <div className="px-4 sm:px-6 py-2">
        <p className="text-[12.5px] leading-snug text-ink-100 flex flex-wrap items-center gap-x-2">
          <span className="inline-flex items-center gap-1.5 font-mono-nums text-[10.5px] tracking-[0.22em] uppercase text-accent-400 shrink-0">
            <Radio size={11} />
            Replay
          </span>
          <span>
            <span className="text-ink-50 font-medium">A recorded audit.</span> Every response is
            a real one, captured off the wire.{' '}
            <span className="text-ink-50 font-medium">Nothing here is live.</span>
          </span>
        </p>
        <p className="mt-0.5 font-mono-nums text-[10.5px] leading-snug text-ink-400">
          run {replayManifest.run_id.slice(0, 8)} · built by{' '}
          <a
            href={`${REPO_URL}/commit/${replayManifest.capturing_commit}`}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-ink-600 underline-offset-2 hover:text-ink-200"
          >
            {replayManifest.capturing_commit.slice(0, 7)}
          </a>{' '}
          · DataHub Core {replayManifest.datahub_core_version} ·{' '}
          {captureDate(replayManifest.captured_at)} · &ldquo;View in DataHub&rdquo; links point at
          the local catalog that produced this record
        </p>
        {dirty && (
          <p className="mt-1 flex items-start gap-1.5 text-[11px] leading-snug text-insufficient-200">
            <AlertTriangle size={12} className="mt-0.5 shrink-0 text-insufficient" />
            <span>
              <span className="font-medium">Provenance incomplete.</span> This was captured from a
              working tree with uncommitted changes, so the commit above is not exactly the code
              that produced these responses. The recorded responses are still real and still
              unedited — their hashes are pinned — but the link names approximate provenance.
            </span>
          </p>
        )}
      </div>
    </div>
  );
}
