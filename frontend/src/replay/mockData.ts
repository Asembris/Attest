// The replay's stand-in for `src/data/mockData.ts`, swapped in by alias (vite.config.ts).
//
// The production sample randomises a freshness window on every mount, so each visitor's
// published claim gets a fresh content-addressed artifact URN. A replay has no catalog to
// write to and exactly ONE recorded audit, so the textarea must show the prose that audit was
// actually of. Showing one sentence and replaying the verdict of another is the single lie
// this whole build is arranged not to be able to tell — and `submitAudit` refuses edited text
// for the same reason, rather than returning a recorded verdict about something unaudited.

import { replaySourceText } from '../api/replayClient';

export function sampleAgentOutput(): string {
  return replaySourceText;
}
