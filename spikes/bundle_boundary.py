"""THE REPLAY MUST NOT LEAK INTO THE PRODUCTION BUNDLE — checked, not hashed by hand.

`docs/replay/` is the same React app built with `api/client` aliased to `api/replayClient`,
so every response comes from committed JSON. The shipped app must contain NONE of that: a
production bundle carrying a recorded verdict, or the banner's copy, is a build that could
answer a real user from a file. The alias in vite.config.ts is what keeps the two module
graphs apart, and this is what proves the separation held on THIS build rather than in
principle.

WHY A SCRIPT AND NOT A TEST. `frontend/dist` is gitignored, so there is nothing for the
offline tier to read — and a test that skipped when the bundle was absent would be a skip in
the one tier whose whole claim is that it never skips. So the check runs where the bundle
exists: right after the build, in `just check-ui` and in CI.

IT CARRIES ITS OWN VACUITY CHECK, and that is the point of taking two directories. A grep
that finds nothing is indistinguishable from a grep for the wrong string — the failure mode
this repo has hit often enough to name it (a green light wired to nothing). So the SAME
detector is run against the replay bundle, where every marker MUST be found. Direction A is
the guarantee; direction B is the proof that direction A could have failed.

Run: `python spikes/bundle_boundary.py`, after `npm run build` and `npm run build:replay`.
No network, no DataHub, no key — it reads two directories.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROD = REPO / "frontend" / "dist"
REPLAY = REPO / "frontend" / "dist-replay"
MANIFEST = REPO / "frontend" / "src" / "replay" / "manifest.json"


def _markers() -> dict[str, str]:
    """Strings that exist in the replay build and must exist in no other.

    Minification renames identifiers but never string literals, so banner copy and the
    inlined fixture data survive both builds intact and are what this can key on. The run id
    is read from the manifest rather than pasted here, so a re-capture cannot leave this
    checking for a recording that no longer exists.
    """
    run_id = json.loads(MANIFEST.read_text(encoding="utf-8"))["run_id"]
    return {
        "banner copy ('recorded audit')": "recorded audit",
        "banner copy ('Nothing here is live')": "Nothing here is live",
        "the refusal a replay gives instead of a verdict": "nothing here is simulated",
        # The banner's own stylesheet, which is §21's leak in its exact form: adding a file
        # under src/replay/ once compiled its classes into the PRODUCTION css, via Tailwind's
        # content glob, from a file no production module imports.
        #
        # NOTE WHICH STRING, because the obvious one is a FALSE POSITIVE. `replay-banner`
        # alone is in the production bundle legitimately: CatalogDrawer.tsx reads
        # `var(--replay-banner-h, 0px)` so a sticky header sits below the banner when there is
        # one and at the top when there is not. Production may READ the variable; only the
        # replay may DEFINE it. So the marker is the declaration, spacing and all.
        "the replay stylesheet's own variable declaration": "--replay-banner-h: 52px",
        f"the recorded run's id ({run_id[:8]}...)": run_id,
    }


def _scan(root: Path, needles: dict[str, str]) -> dict[str, list[str]]:
    """Every marker, and the files it was found in. Reads the whole tree, not a sample."""
    found: dict[str, list[str]] = {label: [] for label in needles}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".js", ".css", ".html"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, needle in needles.items():
            if needle in text:
                found[label].append(path.relative_to(root).as_posix())
    return found


def main() -> int:
    for label, root, build in (
        ("production", PROD, "npm run build"),
        ("replay", REPLAY, "npm run build:replay"),
    ):
        if not root.exists():
            print(f"no {label} bundle at {root}. Run `{build}` first.", file=sys.stderr)
            return 1

    needles = _markers()

    # DIRECTION A — the guarantee. Nothing of the replay may be in the shipped app.
    leaked = {label: files for label, files in _scan(PROD, needles).items() if files}
    if leaked:
        print("THE REPLAY LEAKED INTO THE PRODUCTION BUNDLE:", file=sys.stderr)
        for label, files in leaked.items():
            print(f"  {label}: {', '.join(files)}", file=sys.stderr)
        print(
            "\nThe production build must not contain the replay's module graph. Check that "
            "vite.config.ts still applies the replay aliases ONLY in replay mode, and that no "
            "component imports anything under src/replay/.",
            file=sys.stderr,
        )
        return 1

    # DIRECTION B — the vacuity check. The same detector, on a build that MUST trip it.
    absent = [label for label, files in _scan(REPLAY, needles).items() if not files]
    if absent:
        print(
            "THE DETECTOR CANNOT FIRE. These markers are missing from the REPLAY bundle "
            "too, so finding none of them in production proves nothing:",
            file=sys.stderr,
        )
        for label in absent:
            print(f"  {label}", file=sys.stderr)
        print(
            "\nEither the replay build is broken or the markers no longer describe it. Fix "
            "the markers -- do not delete the check.",
            file=sys.stderr,
        )
        return 1

    print(f"production bundle carries none of the {len(needles)} replay markers,")
    print("and the replay bundle carries all of them, so the check could have failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
