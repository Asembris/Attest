"""THE VACUITY CHECK FOR THE BROWSER E2E. Re-introduce two REAL bugs; demand it goes red.

`just e2e-sabotage`. Exits NON-ZERO if the E2E stays green under sabotage — same discipline
as `just bench-sabotage` and `just spike-mcp`. An assertion that only ever passes is a green
light wired to nothing, and that goes double for a test whose whole justification is catching
a class of bug every other test missed.

**THE TWO SABOTAGES ARE NOT INVENTED. They are lifted from commit 6903d6c**, where they had
both shipped and lived a full session in `main`:

    1. THE 422. `DecisionRequest` carried `accept: boolean` for a session after the backend
       split it into `publish` / `accept_correction`. The backend forbids extras
       (`schemas.py`, `extra="forbid"`), so EVERY approve the UI sent came back 422 and the
       approve flow was simply dead.

    2. THE RE-PARK. The review bar counted `proposals` (claims with a corrected revision)
       while Option A parks on every claim's PUBLICATION. A three-claim run with one proposal
       submitted one decision, re-parked on the other two, and could never reach `complete` —
       while the UI reported it settled.

**Both were found by a human clicking the app. The entire suite was green through both.** That
is the whole argument for the E2E, so these are the exact bugs it has to catch. If it does
not catch them, it is not wired to anything and should not be trusted — which is what this
script exists to find out, rather than to assume.

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


@dataclass(frozen=True)
class Sabotage:
    """One real historical bug, and the file edit that re-introduces it."""

    name: str
    history: str
    path: Path
    old: str
    new: str
    expect: str


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
        old="const pending = awaitingPublication(record);",
        new="const pending = proposals(record);",
        expect="the bar must park on 1 of 3 claims, and the E2E must refuse to call that settled",
    ),
)

# `proposals` is exported but imported by no component, so sabotage 2 needs the import too.
IMPORT_OLD = "import { verdictCounts, awaitingPublication } from '../api/types';"
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
    # Byte-exact original, kept for the restore. `normalized` is the editing surface: the
    # working tree is CRLF (core.autocrlf=true) and every anchor below is written with \n.
    original_bytes = RESULTS.read_bytes()
    raw = original_bytes.decode("utf-8")
    crlf = "\r\n" in raw
    normalized = raw.replace("\r\n", "\n")
    survived: list[str] = []

    print("=" * 78)
    print("  E2E VACUITY CHECK — two real bugs from 6903d6c, re-introduced")
    print("=" * 78)

    try:
        for s in SABOTAGES:
            print(f"\n--- SABOTAGE: {s.name}")
            print(f"    history : {s.history}")
            print(f"    expect  : {s.expect}")

            text = normalized
            if s.old not in text:
                raise SystemExit(
                    f"cannot apply '{s.name}': the anchor is gone from {s.path.name}.\n"
                    f"  looked for: {s.old!r}\n"
                    "The UI changed shape and this sabotage no longer reproduces the bug it "
                    "names. FIX THE SABOTAGE — do not delete it."
                )
            text = text.replace(s.old, s.new)
            if s.name == "the re-park":
                text = text.replace(IMPORT_OLD, IMPORT_NEW)
            out = text.replace("\n", "\r\n") if crlf else text
            s.path.write_bytes(out.encode("utf-8"))

            build()
            passed, out = run_e2e()

            if passed:
                survived.append(s.name)
                print(f"    RESULT  : *** THE E2E STAYED GREEN. '{s.name}' SURVIVED. ***")
            else:
                why = [ln for ln in out.splitlines() if ln.strip().startswith(("E ", "FAILED"))]
                print("    RESULT  : caught — the E2E went RED")
                for ln in why[:4]:
                    print(f"              {ln.strip()[:96]}")
    finally:
        RESULTS.write_bytes(original_bytes)  # byte-exact, whatever happened above
        print("\n--- restored AuditResults.tsx and rebuilding the honest bundle")
        build()

    print("\n" + "=" * 78)
    if survived:
        print(f"  FAILED: {len(survived)}/{len(SABOTAGES)} sabotage(s) survived: {survived}")
        print("  The E2E did not catch a bug that really shipped. It is not wired to what it")
        print("  claims to test, and a green run of it means nothing until this is fixed.")
        print("=" * 78)
        return 1

    print(f"  PASSED: all {len(SABOTAGES)} sabotages caught. The E2E can fail for a real")
    print("  reason — both of the reasons it was written for.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
