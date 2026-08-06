"""GitHub Pages serves `docs/` VERBATIM, so what is committed there is what a judge opens.

Nothing else in the suite reads that tree. `test_replay_fixtures.py` proves the recorded
RESPONSES are unedited and still parse through the real wire types; `spikes/replay_verify.py`
drives the built page in a browser. Between them sits the deployed tree itself, and two ways
it can be broken while every other check stays green:

  1. **A LINK OR ASSET THAT IS NOT THERE.** `docs/index.html` is a hand-written landing page
     referencing screenshots, the replay, and a video. A renamed screenshot or a moved
     directory is a 404 on the most public page the project has, and it is invisible from
     inside the repo because nothing loads that page in CI.

  2. **A STAGED BUNDLE OLDER THAN THE FIXTURES IT CLAIMS TO REPLAY.** `docs/replay/` is a
     BUILD ARTIFACT of `frontend/src/replay/*.json`. Re-capture (`just replay-capture`) and
     forget to rebuild (`just replay-build`), and every fixture test stays green -- they read
     the source fixtures, which are perfectly consistent -- while the deployed page serves the
     PREVIOUS recording. The banner then dates and attributes a run whose responses are not
     the ones on screen: a provenance claim that is wrong, on the page whose entire argument
     is that a recording is honest about what it is.

Offline tier: this reads committed files and nothing else. No DataHub, no key, no network,
no build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
REPLAY_SRC = REPO / "frontend" / "src" / "replay"
STAGED = DOCS / "replay"

# Every page GitHub Pages actually serves as an entry point.
PAGES = (DOCS / "index.html", STAGED / "index.html")

# A build's leftovers, in the two shapes this project could plausibly ship one: a token
# somebody meant to replace, and the placeholder video id the submission docs once carried.
PLACEHOLDERS = (
    "REPLACE-YOUTUBE-ID",
    "REPLACE-ME",
    "REPLACE_ME",
    "TODO-REPLACE",
    "PLACEHOLDER",
    "XXXXXXX",
    "lorem ipsum",
)

_REF = re.compile(r"""\b(?:href|src)=["']([^"']+)["']""", re.IGNORECASE)


def _local_refs(page: Path) -> list[str]:
    """Every href/src that names something in this repo.

    Anchors, external URLs and non-fetching schemes are dropped -- this pins what WE ship,
    not what a third party still hosts. A network check would fail on a flaky DNS and teach
    people to re-run the suite until it went green.
    """
    refs = []
    for raw in _REF.findall(page.read_text(encoding="utf-8")):
        ref = raw.split("#")[0].split("?")[0].strip()
        if not ref or ref.startswith(("#", "http://", "https://", "//", "mailto:", "data:")):
            continue
        refs.append(ref)
    return refs


def _resolve(page: Path, ref: str) -> Path:
    """A directory reference (`./replay/`) is served by its index.html, exactly as Pages does."""
    target = (page.parent / ref).resolve()
    return target / "index.html" if target.is_dir() else target


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_every_local_asset_a_pages_entry_point_references_is_committed(page: Path) -> None:
    assert page.exists(), f"{page} is not committed, so GitHub Pages would serve a 404"
    refs = _local_refs(page)
    assert refs, f"{page.name} references no local asset at all -- the regex stopped matching"
    missing = [ref for ref in refs if not _resolve(page, ref).exists()]
    assert not missing, (
        f"{page.relative_to(REPO).as_posix()} references {len(missing)} file(s) that are not "
        f"committed: {missing}. GitHub Pages serves docs/ verbatim, so each of these is a 404 "
        "for every reader."
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.relative_to(REPO).as_posix())
def test_a_pages_entry_point_carries_no_placeholder(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    left = [token for token in PLACEHOLDERS if token.lower() in text.lower()]
    assert not left, (
        f"{page.relative_to(REPO).as_posix()} still contains {left} -- a placeholder that "
        "reached the deployed page."
    )


def test_the_missing_asset_check_can_actually_fail(tmp_path: Path) -> None:
    """The vacuity check for the pin above.

    A link checker that only ever passes is a green light wired to nothing, and this one is
    a regex over hand-written HTML -- the most plausible way for it to silently stop matching.
    So it is pointed at a page that IS broken, and must say so.
    """
    broken = tmp_path / "index.html"
    broken.write_text(
        '<a href="./assets/gone.png">x</a><img src="./also-gone.svg">', encoding="utf-8"
    )
    refs = _local_refs(broken)
    assert refs == ["./assets/gone.png", "./also-gone.svg"], refs
    assert [r for r in refs if not _resolve(broken, r).exists()] == refs

    # And it must not fire on a reference that resolves, or every page would fail forever.
    (tmp_path / "here.png").write_bytes(b"")
    ok = tmp_path / "ok.html"
    ok.write_text('<img src="./here.png">', encoding="utf-8")
    assert [r for r in _local_refs(ok) if not _resolve(ok, r).exists()] == []


def _staged_bundle_text() -> str:
    scripts = sorted(STAGED.glob("assets/*.js"))
    assert scripts, (
        f"no JavaScript in {STAGED}/assets -- the staged replay is not a built bundle. "
        "Run `just replay-build`."
    )
    return "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in scripts)


def test_the_staged_replay_was_built_from_the_fixtures_that_are_committed_now() -> None:
    """THE STALENESS PIN. Everything else about the replay checks the SOURCE fixtures.

    Vite inlines the imported JSON into the bundle, so the recording's own identity travels
    into the built JavaScript verbatim and can be read back out. All three fields move on a
    re-capture -- `captured_at` even on a `--resume`, which keeps the same run -- so this
    catches the case where the fixtures were re-captured and the bundle was not rebuilt.

    What it does NOT claim: that the bundle is byte-for-byte what this source builds today.
    Proving that means rebuilding in CI and comparing, and cross-platform byte-determinism of
    this toolchain has been measured on ONE machine (§21), never between two. A gate that
    might go red for a reason nobody can act on is worse than the narrower one that cannot.
    """
    manifest = json.loads((REPLAY_SRC / "manifest.json").read_text(encoding="utf-8"))
    bundle = _staged_bundle_text()
    for field in ("run_id", "capturing_commit", "captured_at"):
        assert manifest[field] in bundle, (
            f"the staged replay in docs/ does not carry the fixtures' {field} "
            f"({manifest[field]}), so it was built from a DIFFERENT recording than the one "
            "committed under frontend/src/replay/. Re-run `just replay-build` (and "
            "`just replay-verify`) so the deployed page and its provenance agree."
        )


def test_the_staged_replay_carries_the_claims_the_recording_published() -> None:
    """Identity is not content. The manifest could match while the responses moved.

    The claim URNs are the recording's payload rather than its label, and they are inlined
    the same way -- so a re-capture that published different claims is caught here even if
    the manifest fields somehow did not move.
    """
    approval = json.loads((REPLAY_SRC / "approval.json").read_text(encoding="utf-8"))
    urns = [w["claim_urn"] for w in approval["writebacks"]]
    assert urns, "the recorded approval wrote no claims"
    bundle = _staged_bundle_text()
    absent = [u for u in urns if u not in bundle]
    assert not absent, (
        f"the staged replay does not contain {len(absent)} of the {len(urns)} claim artifacts "
        f"the recorded approval published ({absent}). docs/replay/ is out of date with "
        "frontend/src/replay/ -- re-run `just replay-build`."
    )


def test_the_staleness_pin_can_actually_fail() -> None:
    """The vacuity check for the two pins above: an id that was never recorded is not found."""
    bundle = _staged_bundle_text()
    assert "urn:li:assertion:attest-000000000000000000" not in bundle
    assert "00000000-0000-0000-0000-000000000000" not in bundle


def test_the_staged_replay_entry_point_names_the_bundle_it_ships() -> None:
    """`just replay-build` renames index.replay.html and copies the tree; a half-staged
    directory (an index that names an asset the copy missed) is a blank page for every reader.
    The reference check above proves the assets exist; this proves the entry point is the
    REPLAY one rather than a stray production index copied in by hand."""
    index = (STAGED / "index.html").read_text(encoding="utf-8")
    assert "index.replay-" in index, (
        "docs/replay/index.html does not load a replay bundle -- it names "
        f"{_REF.findall(index)}. The staged entry point must be the built index.replay.html."
    )
