"""THE VACUITY CHECK FOR THE BROWSER E2E. Re-introduce five REAL bugs; demand it goes red.

`just e2e-sabotage`. Exits NON-ZERO if the E2E stays green under sabotage — same discipline
as `just bench-sabotage` and `just spike-mcp`. An assertion that only ever passes is a green
light wired to nothing, and that goes double for a test whose whole justification is catching
a class of bug every other test missed.

**THE SABOTAGES ARE NOT INVENTED.** The first three are UI bugs that shipped or were
caught before submission. The fourth is the reproduced same-dataset receipt alias. The fifth
is the one regression the discovery feature is arranged to be unable to make — a failed
catalog search quietly answered from a list baked into the bundle — and it is here rather
than merely forbidden in a comment, because a rule with no falsification is a preference:

    1. THE 422. `DecisionRequest` carried `accept: boolean` for a session after the backend
       split it into `publish` / `accept_correction`. The backend forbids extras
       (`schemas.py`, `extra="forbid"`), so EVERY approve the UI sent came back 422 and the
       approve flow was simply dead.

    2. THE RE-PARK. The review bar counted `proposals` (claims with a corrected revision)
       while Option A parks on every claim's PUBLICATION. A three-claim run with one proposal
       submitted one decision, re-parked on the other two, and could never reach `complete` —
       while the UI reported it settled. (The gate blind to the PUBLICATION axis.)

    3. THE STRAND. The gate counted only publications and left `accept_correction` out of it,
       so publishing every verdict enabled Submit at "3/3" while a proposed correction was
       still unruled — the backend re-parked on it and the run stuck in `awaiting-review` with
       no live control. (The gate blind to the CORRECTION axis — the mirror of the re-park.)

    4. THE RECEIPT ALIAS. Two claims on one dataset received divergent publication results,
       but both cards matched the first receipt by target URN. The second card therefore
       reported a catalog failure even though its own write succeeded.

    5. THE STATIC FALLBACK. The URN picker answers a FAILED catalog search with the seeded
       list that used to be baked into the bundle, instead of a visible offline state. Never
       shipped — this one is prevented rather than fixed — but it is the single most likely
       "helpful" edit anyone will make to that component, and it would have the UI show a
       human a catalog that is a FILE. An outage rendered as a plausible answer is the exact
       collapse Attest exists to catch, committed in Attest's own interface.

**The first three were found by a human clicking the app. The fourth was reproduced through
that same browser boundary after an external audit named the source-level inference.**
That is the whole argument for the E2E, so these are the exact bugs it has to catch. If it
does not catch them, it is not wired to anything and should not be trusted — which is what
this script exists to find out, rather than to assume.

Every edit is reverted in a `finally`, and the bundle is rebuilt from the restored source
before exit, so a crash cannot leave a sabotaged tree or a sabotaged `dist` behind.

**The restore writes back the EXACT BYTES that were read**, and the anchors are matched
against a newline-normalized COPY. Both halves are needed and for different reasons. `git
config core.autocrlf` is `true` here, so the working tree is CRLF while the blob is LF: match
the anchors against the raw file and they never hit (they are written with `\\n`), which makes
the script abort claiming the UI changed shape when nothing has. And a restore that
round-trips through anything but the original bytes is a tool that reports success while
rewriting the file it exists to put back — the loud-then-silent shape this repo names for the
TLS repair. Byte-exact in, byte-exact out, normalize only in between.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRONTEND = REPO / "frontend"
RESULTS = FRONTEND / "src" / "components" / "AuditResults.tsx"
PICKER = FRONTEND / "src" / "components" / "UrnPicker.tsx"


@dataclass(frozen=True)
class Sabotage:
    """One real historical bug, and the file edit that re-introduces it."""

    name: str
    history: str
    path: Path
    old: str
    new: str
    expect: str
    # Further edits the same sabotage needs — an import the swapped-in symbol requires, most
    # often. Applied to the same file, in order, and abort if any anchor is missing: a
    # sabotage that half-applies would compile into some third thing and test nothing.
    also: tuple[tuple[str, str], ...] = ()


SABOTAGES = (
    Sabotage(
        name="the 422",
        history=(
            "6903d6c: DecisionRequest carried `accept: boolean` a full session after the "
            "backend split it. extra='forbid' -> 422 on EVERY approve."
        ),
        path=RESULTS,
        old="        claim_index: c.index,\n        publish: d.publish,",
        new="        claim_index: c.index,\n        accept: d.publish,",
        expect="the browser's approve call must come back 422 and the E2E must say so",
    ),
    Sabotage(
        name="the re-park",
        history=(
            "6903d6c: the review bar counted `proposals` while Option A parks on every "
            "claim's publication. A 3-claim run with 1 proposal could never complete."
        ),
        path=RESULTS,
        old="const reviewSet = awaitingDecision(record);",
        new="const reviewSet = proposals(record);",
        expect="the bar must park on 1 of 3 claims, and the E2E must refuse to call that settled",
    ),
    Sabotage(
        name="the strand",
        history=(
            "pre-submission audit: the gate left `accept_correction` out, so publishing "
            "every verdict enabled Submit at 3/3 while a proposed correction was unruled — "
            "the run re-parked into a dead awaiting-review with no live control."
        ),
        path=RESULTS,
        old="    return pubDecided && correctionDecided;",
        new="    return pubDecided;",
        expect="publishing all verdicts must NOT open Submit while a correction is unruled",
    ),
    Sabotage(
        name="the receipt alias",
        history=(
            "external audit, reproduced live: same-dataset cards matched writebacks by "
            "target URN, so claim 1 displayed claim 0's failed receipt."
        ),
        path=RESULTS,
        old="writebacks?.find((w) => w.claim_index === claim.index)",
        new="writebacks?.find((w) => w.target_urn === claim.target_urn)",
        expect="the successful second card must not inherit the first claim's failed receipt",
    ),
    Sabotage(
        name="the static fallback",
        history=(
            "never shipped, and prevented rather than fixed: answering a FAILED catalog "
            "search with the seeded list that used to be in the bundle. An outage rendered "
            "as a plausible answer, in the UI of the tool built to catch exactly that."
        ),
        path=PICKER,
        old=(
            "  return {\n"
            "    kind: 'offline',\n"
            "    detail: api?.detail ?? String(err),\n"
            "    permanent: api?.status === 501,\n"
            "  };"
        ),
        new=(
            "  void api;\n"
            "  return {\n"
            "    kind: 'ready',\n"
            "    hits: seededDatasets.map((d) => ({ urn: d.urn, name: d.name })),\n"
            "    total: seededDatasets.length,\n"
            "    transport: 'DataHub MCP Server',\n"
            "  };"
        ),
        also=(
            (
                "import type { CatalogHit } from '../api/types';",
                "import type { CatalogHit } from '../api/types';\n"
                "import { seededDatasets } from '../data/catalog';",
            ),
        ),
        expect="a dead discovery service must NOT render a list of datasets from the bundle",
    ),
)

# `proposals` is exported but imported by no component, so the re-park sabotage needs the
# import too (it swaps `awaitingDecision` -> `proposals`). The strand sabotage touches only
# the gate's return and needs no import change.
IMPORT_OLD = "import { verdictCounts, awaitingDecision } from '../api/types';"
IMPORT_NEW = "import { verdictCounts, proposals } from '../api/types';"


def build() -> None:
    r = subprocess.run(
        ["npx", "vite", "build"],
        cwd=str(FRONTEND),
        env={**__import__("os").environ, "NODE_USE_SYSTEM_CA": "1"},
        capture_output=True,
        text=True,
        shell=True,
    )
    if r.returncode != 0:
        raise SystemExit(f"vite build failed, refusing to test a stale bundle:\n{r.stderr}")


def run_e2e() -> tuple[bool, str]:
    """The E2E, as a subprocess. True == it passed."""
    r = subprocess.run(
        [
            sys.executable, "-m", "pytest",
            "tests/test_e2e_browser.py", "-m", "live", "-q", "--no-header", "-x",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    return r.returncode == 0, r.stdout + r.stderr


def main() -> int:
    # Byte-exact originals for EVERY file any sabotage touches, kept for the restore. A
    # restore that only put back the file the last sabotage happened to edit would leave a
    # sabotaged tree behind on any crash — the loud-then-silent shape this repo names for the
    # TLS repair, one directory over.
    touched = sorted({s.path for s in SABOTAGES}, key=str)
    original: dict[Path, bytes] = {p: p.read_bytes() for p in touched}
    survived: list[str] = []

    print("=" * 78)
    print(f"  E2E VACUITY CHECK — {len(SABOTAGES)} real browser-boundary bugs, re-introduced")
    print("=" * 78)

    try:
        for s in SABOTAGES:
            print(f"\n--- SABOTAGE: {s.name}")
            print(f"    history : {s.history}")
            print(f"    expect  : {s.expect}")

            # `normalized` is the editing surface: the working tree is CRLF
            # (core.autocrlf=true) and every anchor is written with \n.
            raw = original[s.path].decode("utf-8")
            crlf = "\r\n" in raw
            text = raw.replace("\r\n", "\n")
            edits = ((s.old, s.new), *s.also)
            if s.name == "the re-park":
                edits = (*edits, (IMPORT_OLD, IMPORT_NEW))
            for old, new in edits:
                if old not in text:
                    raise SystemExit(
                        f"cannot apply '{s.name}': the anchor is gone from {s.path.name}.\n"
                        f"  looked for: {old!r}\n"
                        "The UI changed shape and this sabotage no longer reproduces the bug "
                        "it names. FIX THE SABOTAGE — do not delete it."
                    )
                text = text.replace(old, new)
            out = text.replace("\n", "\r\n") if crlf else text
            s.path.write_bytes(out.encode("utf-8"))

            build()
            passed, out = run_e2e()
            s.path.write_bytes(original[s.path])  # one sabotage at a time, byte-exact back

            if passed:
                survived.append(s.name)
                print(f"    RESULT  : *** THE E2E STAYED GREEN. '{s.name}' SURVIVED. ***")
            else:
                why = [ln for ln in out.splitlines() if ln.strip().startswith(("E ", "FAILED"))]
                print("    RESULT  : caught — the E2E went RED")
                for ln in why[:4]:
                    print(f"              {ln.strip()[:96]}")
    finally:
        for path, data in original.items():
            path.write_bytes(data)  # byte-exact, whatever happened above
        print(
            "\n--- restored "
            + ", ".join(p.name for p in touched)
            + " and rebuilding the honest bundle"
        )
        build()

    print("\n" + "=" * 78)
    if survived:
        print(f"  FAILED: {len(survived)}/{len(SABOTAGES)} sabotage(s) survived: {survived}")
        print("  The E2E did not catch a bug that really shipped. It is not wired to what it")
        print("  claims to test, and a green run of it means nothing until this is fixed.")
        print("=" * 78)
        return 1

    print(f"  PASSED: all {len(SABOTAGES)} sabotages caught. The E2E can fail for a real")
    print("  reason — every one of the reasons it was written for.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
