import { Check, Clock3, HelpCircle, Loader2, XCircle } from 'lucide-react';
import type { ReadState } from '../api/types';

// HOW A CLAIM'S VERDICT READS IN THE CATALOG RIGHT NOW — and the whole reason there are
// four of these rather than two.
//
// An absent verdict is not one fact. The catalog can be silent about a claim because the
// write landed and DataHub's index has not caught up (transient, ~2s measured), because the
// write FAILED at a step (no verdict is ever coming), or because Attest's own record cannot
// say whether one was attempted. Those are three different facts with three different
// responses, and only ONE of them is a bug.
//
// Rendering all of them as "broken" would be reading absence as an answer — the exact
// mistake this product exists to catch — in Attest's own read path. And it would be wrong
// on the HAPPY PATH: pending-lag is the normal state for the first couple of seconds after
// every single approval.
//
// The colours say it: `complete` is a verdict (neutral — the VerdictBadge beside it carries
// the colour), `pending-lag` is a working state and not an alarm, `incomplete` is the only
// red one, and `unknown` is the honest shrug.

const STATE: Record<
  ReadState,
  { label: string; blurb: string; color: string; bg: string; border: string; icon: typeof Check }
> = {
  complete: {
    label: 'In the catalog',
    blurb: 'DataHub holds this verdict. A reader with no access to Attest sees it.',
    color: 'text-supported',
    bg: 'bg-supported/10',
    border: 'border-supported/30',
    icon: Check,
  },
  'pending-lag': {
    label: 'Catching up',
    blurb:
      "The verdict is written — Attest's record says the write landed. DataHub's index has " +
      'not shown it yet, which takes about two seconds. There is nothing to repair.',
    color: 'text-accent-400',
    bg: 'bg-accent/10',
    border: 'border-accent/30',
    icon: Loader2,
  },
  incomplete: {
    label: 'Incomplete',
    blurb:
      'The write failed partway, so no verdict is coming on its own. The claim is in the ' +
      'catalog without one. This is repairable, and the repair is safe to run twice.',
    color: 'text-contradicted',
    bg: 'bg-contradicted/10',
    border: 'border-contradicted/30',
    icon: XCircle,
  },
  unknown: {
    label: 'Unknown',
    blurb:
      'There is no verdict here, and Attest has no record of writing one. That is not ' +
      'evidence the write failed — it is the absence of evidence, which is a different ' +
      'thing and is reported as one.',
    color: 'text-insufficient',
    bg: 'bg-insufficient/10',
    border: 'border-insufficient/30',
    icon: HelpCircle,
  },
};

export function readStateBlurb(state: ReadState): string {
  return STATE[state].blurb;
}

export default function ReadStateBadge({
  state,
  size = 'sm',
}: {
  state: ReadState;
  size?: 'sm' | 'md';
}) {
  const c = STATE[state];
  const Icon = c.icon;
  const sz = size === 'sm' ? 'text-xs px-2.5 py-1' : 'text-sm px-3 py-1.5';

  return (
    <span
      title={c.blurb}
      className={`inline-flex items-center gap-1.5 rounded-full border font-medium ${sz} ${c.color} ${c.bg} ${c.border}`}
    >
      <Icon
        size={size === 'sm' ? 12 : 14}
        // The spinner spins ONLY while something is genuinely resolving itself. A static
        // spinner on a state nobody is waiting on reads as a hang.
        className={state === 'pending-lag' ? 'animate-spin' : ''}
      />
      {c.label}
    </span>
  );
}

export { Clock3 };
