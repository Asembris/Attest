"""PROVE the staged replay: a real browser, a static server, and NOTHING ELSE.

`docs/replay/` claims to be a self-contained recording of one real audit that a judge can
drive without running an 8 GB Docker stack. Two halves of that are claims, not facts, until
something checks them:

  1. IT RESOLVES FROM A SUBDIRECTORY. The bundle is built with `base: './'` because it has to
     serve from `/Attest/replay/` on github.io AND from `/replay/` when the built docs/ tree
     is opened locally. A rooted base works in exactly one of those and is silently broken in
     the other — the page loads, the assets 404, and you find out from a judge. So both
     shapes are served here, from two static servers, and the flow is driven through each.

  2. IT MAKES NO NETWORK CALL BEYOND ITS OWN ASSETS. "It works offline" is the whole premise;
     a single stray request to a backend, a CDN or a font host would mean the replay works on
     the machine that built it and nowhere else. Every request the page issues is recorded and
     checked against the replay's own directory prefix. Not sampled — every one.

AND IT DRIVES THE REFUSALS, not just the happy path. The two places this build could lie are
a decision set that was never recorded and a `/claims` filter that was never recorded, and in
both the honest answer is to refuse and say why. A refusal nobody exercises is a comment. So
this walks the recorded run end to end, then withholds a verdict and demands the 409, then
filters to an unrecorded dataset and demands the "not recorded" answer rather than an empty
list — which would be the silent absence this whole product exists to refuse.

Run: `just replay-verify`, after `just replay-build`. Needs `pip install -e ".[e2e]"` and
Edge. No DataHub, no API key, no model, no network: the thing under test has no backend, and
neither does the test.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
REPLAY = DOCS / "replay"

# THE BROWSER, and why it is settable. Locally this drives INSTALLED Edge, so playwright
# downloads nothing -- its downloader is the bundled Node driver, which would need
# NODE_USE_SYSTEM_CA=1 on this TLS-inspecting network. That default does not move.
#
# CI is the other way round: there is no corporate CA to trip over and no Edge to ASSUME. A
# runner image that quietly stopped shipping Edge would fail this as "the replay is broken",
# which is a lie about the thing under test -- the same shape as the smoke sabotage failing
# at the wrong boundary. So the channel is an env var, and an EMPTY value means playwright's
# own bundled Chromium, which CI installs explicitly and by version.
BROWSER_CHANNEL = os.environ.get("ATTEST_BROWSER_CHANNEL", "msedge")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(directory: Path) -> tuple[subprocess.Popen[bytes], str]:
    """A plain static file server — the same thing GitHub Pages is, minus the CDN."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(directory)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{origin}/", timeout=1):
                return proc, origin
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError(f"static server for {directory} never came up")


class Requests:
    """Every request the page makes, and whether it stayed inside the replay.

    `page.on("request")` fires for the document, every asset, every font, every XHR and every
    dynamic import — so this is the whole network log, not a filter over the interesting bits.
    """

    def __init__(self, allowed_prefix: str) -> None:
        self.allowed = allowed_prefix
        self.urls: list[str] = []

    def attach(self, page: object) -> None:
        page.on("request", lambda r: self.urls.append(r.url))  # type: ignore[attr-defined]

    @property
    def foreign(self) -> list[str]:
        # `data:` and `blob:` never leave the page. Everything else must be under the
        # replay's own directory — not merely the same origin, which would let a stray
        # `/health` (the shape a forgotten real client call would take) pass.
        return [
            u
            for u in self.urls
            if not u.startswith(self.allowed) and not u.startswith(("data:", "blob:"))
        ]

    def assert_self_contained(self, where: str) -> None:
        if self.foreign:
            raise AssertionError(
                f"{where}: the replay made {len(self.foreign)} request(s) outside its own "
                f"directory ({self.allowed}). A static replay that needs a network is not a "
                f"replay:\n  " + "\n  ".join(sorted(set(self.foreign))[:10])
            )


def _console_sink(sink: list[str]):
    """Bound explicitly rather than closed over the loop variable — a lambda capturing `errors`
    from the enclosing loop would report the LAST shape's errors against every shape."""

    def on_message(message: object) -> None:
        if message.type == "error":  # type: ignore[attr-defined]
            sink.append(message.text)  # type: ignore[attr-defined]

    return on_message


def _banner(page: object, where: str) -> None:
    banner = page.locator(".replay-banner")  # type: ignore[attr-defined]
    if not banner.is_visible():
        raise AssertionError(f"{where}: the REPLAY banner is not visible")
    text = banner.inner_text()
    # The three claims the banner has to make, in the words it makes them in: what the page
    # is, that the responses are real, and that none of it is live. The provenance row is
    # deliberately NOT checked phrase-by-phrase — it is detail, and pinning it here would make
    # every copy edit a test edit for no gain.
    claims = (
        "Replay",
        "A recorded audit",
        "Every response is a real one",
        "Nothing here is live",
    )
    for phrase in claims:
        if phrase.lower() not in text.lower():
            raise AssertionError(f"{where}: the banner does not say {phrase!r}:\n{text}")
    # The capture date must not render in the VIEWER's locale — a French browser showed
    # "4 août 2026" in an otherwise English banner, which is what `toLocaleDateString` does.
    for month in ("janvier", "février", "mars", "avril", "juin", "juillet", "août"):
        if month in text.lower():
            raise AssertionError(
                f"{where}: the banner rendered a localized month ({month!r}), so the date is "
                f"being formatted per viewer rather than fixed to English:\n{text}"
            )


def _walk_recorded_run(page: object, calls: Requests) -> None:
    """The recorded path, exactly as a judge would click it."""
    _banner(page, "hero")

    # The textarea must show the prose the recording is OF. If this ever diverges, the page
    # is showing one sentence and replaying the verdict of another.
    box = page.get_by_placeholder("Paste the AI agent's claims")  # type: ignore[attr-defined]
    shown = box.input_value()
    for fragment in ("customer_profile", "contains no PII", "raw_events", "alice.chen"):
        assert fragment in shown, (
            f"the replay textarea does not show the recorded claims: {shown[:200]}"
        )

    page.get_by_role("button", name="Run Audit").click()  # type: ignore[attr-defined]
    _banner(page, "audit in progress")

    # AuditProgress replays the run from `record.steps` and HOLDS — no auto-advance, exactly
    # as against a live backend. That the audit UNFOLDS is the difference between an
    # interactive replay and a screenshot.
    cont = page.get_by_role("button", name="Continue to results")  # type: ignore[attr-defined]
    cont.wait_for(timeout=60_000)
    cont.click()
    page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)  # type: ignore[attr-defined]
    _banner(page, "results")

    counter = page.get_by_text("decided")  # type: ignore[attr-defined]
    total = int(counter.first.inner_text().split("/")[1].split()[0])
    assert total == 3, f"expected 3 claims parked for publication, got {total}"

    publish = page.get_by_role("button", name="Publish verdict", exact=True)  # type: ignore[attr-defined]
    assert publish.count() == 3, f"expected 3 publish controls, found {publish.count()}"
    for _ in range(3):
        publish.first.click()
    accept = page.get_by_role("button", name="Accept correction", exact=True)  # type: ignore[attr-defined]
    assert accept.count() == 1, f"expected exactly one proposed correction, found {accept.count()}"
    accept.first.click()

    page.get_by_role("button", name="Submit decisions").click()  # type: ignore[attr-defined]
    # The recorded write-backs really landed, so the settled record says so.
    page.get_by_text("Verdict written to catalog").first.wait_for(timeout=30_000)  # type: ignore[attr-defined]
    assert page.get_by_role("button", name="Submit decisions").count() == 0, (  # type: ignore[attr-defined]
        "the run is still parked after every claim was decided"
    )

    # The inheritance half: read the published verdicts back out of the recorded catalog.
    page.get_by_role("button", name="Published claims").first.click()  # type: ignore[attr-defined]
    page.get_by_role("heading", name="What the next agent inherits").wait_for(timeout=30_000)  # type: ignore[attr-defined]
    page.get_by_text("In the catalog").first.wait_for(timeout=30_000)  # type: ignore[attr-defined]
    _banner(page, "claims explorer")

    # The push-down disclosure is the API's own answer, replayed verbatim — never computed here.
    page.get_by_text("Where this was filtered").first.wait_for(timeout=10_000)  # type: ignore[attr-defined]
    calls.assert_self_contained("the recorded run")


def _refuses_an_unrecorded_filter(page: object) -> None:
    """A filter outside the recorded matrix is REFUSED, never answered with an empty list.

    This is the read-path twin of the whole thesis. "Nothing was recorded for this query" and
    "the catalog holds nothing" are different facts, and a replay that rendered the second for
    the first would be committing this product's cardinal sin in its own output.
    """
    page.get_by_role("heading", name="What the next agent inherits").wait_for(timeout=30_000)  # type: ignore[attr-defined]
    # `orders_fact` is a seeded dataset the audited claims were not about, so it is offered by
    # the dropdown and is outside the recorded matrix.
    page.locator("select").first.select_option(  # type: ignore[attr-defined]
        "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders.orders_fact,PROD)"
    )
    page.get_by_text("not recorded").first.wait_for(timeout=15_000)  # type: ignore[attr-defined]
    body = page.locator("body").inner_text()  # type: ignore[attr-defined]
    assert "NOT a statement that the catalog holds no such claim" in body, (
        "the unrecorded filter was refused, but without saying that absence of a recording is "
        "not absence from the catalog — which is the only reason the refusal is worth having"
    )
    assert "No claim artifacts match this filter" not in body, (
        "an unrecorded filter rendered the EMPTY-RESULT screen — a replay reporting absence "
        "as an answer, which is the exact collapse this product exists to refuse"
    )


def _refuses_a_divergent_decision(page: object) -> None:
    """Withhold a verdict the recording published, and demand the honest 409.

    The captured response is what one human's decision really produced. Returning it for a
    DIFFERENT decision would show a judge a published verdict for a claim they withheld — a
    fabricated consequence, in the flow whose whole point is that consequences are real.
    """
    box = page.get_by_placeholder("Paste the AI agent's claims")  # type: ignore[attr-defined]
    box.wait_for(timeout=30_000)
    page.get_by_role("button", name="Run Audit").click()  # type: ignore[attr-defined]
    cont = page.get_by_role("button", name="Continue to results")  # type: ignore[attr-defined]
    cont.wait_for(timeout=60_000)
    cont.click()
    page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)  # type: ignore[attr-defined]

    # Publish two, WITHHOLD one — a decision a human is entitled to make, and one this
    # recording has no result for.
    #
    # `.last`, not `.first`, and the difference is a real trap: EVERY pending claim carries a
    # Withhold control, so withholding the FIRST one overwrites a publish decision already
    # made and leaves the third claim undecided — Submit stays disabled and the test hangs on
    # a button that was never going to enable. Withhold the claim nothing has ruled on yet.
    publish = page.get_by_role("button", name="Publish verdict", exact=True)  # type: ignore[attr-defined]
    publish.first.click()
    publish.first.click()
    page.get_by_role("button", name="Withhold", exact=True).last.click()  # type: ignore[attr-defined]
    page.get_by_role("button", name="Accept correction", exact=True).first.click()  # type: ignore[attr-defined]
    page.get_by_role("button", name="Submit decisions").click()  # type: ignore[attr-defined]

    page.get_by_text("one recorded outcome").first.wait_for(timeout=20_000)  # type: ignore[attr-defined]
    body = page.locator("body").inner_text()  # type: ignore[attr-defined]
    assert "nothing here is simulated" in body.lower(), (
        "the divergent decision was refused without explaining WHY, which is the whole "
        f"value of refusing:\n{body[:500]}"
    )
    assert "Verdict written to catalog" not in body, (
        "a withheld verdict was reported as written to the catalog — the recorded outcome "
        "was replayed for a decision nobody made"
    )


def main() -> int:
    if not (REPLAY / "index.html").exists():
        print(f"no staged replay at {REPLAY}. Run `just replay-build` first.", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    servers: list[subprocess.Popen[bytes]] = []
    tmp = Path(tempfile.mkdtemp(prefix="attest-replay-pages-"))
    failures: list[str] = []
    try:
        # SHAPE 1 — the built docs/ tree, as a local static check: /replay/
        proc_a, origin_a = _serve(DOCS)
        servers.append(proc_a)
        # SHAPE 2 — the GitHub Pages path: https://asembris.github.io/Attest/replay/. Pages
        # serves the repo under its NAME, so the deployed URL has a segment the local check
        # does not. That extra segment is exactly what a rooted base gets wrong.
        shutil.copytree(DOCS, tmp / "Attest")
        proc_b, origin_b = _serve(tmp)
        servers.append(proc_b)

        shapes = [
            ("local docs/ tree", f"{origin_a}/replay/"),
            ("GitHub Pages path shape", f"{origin_b}/Attest/replay/"),
        ]

        with sync_playwright() as p:
            # See BROWSER_CHANNEL: installed Edge by default, playwright's own Chromium when
            # the env var is empty. Printed rather than assumed, so a run that verified a
            # different browser than you think says so in its own output.
            launch = {"channel": BROWSER_CHANNEL} if BROWSER_CHANNEL else {}
            print(f"browser: {BROWSER_CHANNEL or 'playwright chromium (bundled)'}")
            browser = p.chromium.launch(headless=True, **launch)
            for label, url in shapes:
                page = browser.new_page()
                calls = Requests(url)
                calls.attach(page)
                errors: list[str] = []
                page.on("console", _console_sink(errors))
                try:
                    page.goto(url, wait_until="networkidle")
                    _walk_recorded_run(page, calls)
                    _refuses_an_unrecorded_filter(page)

                    page.goto(url, wait_until="networkidle")
                    _refuses_a_divergent_decision(page)

                    calls.assert_self_contained(label)
                    if errors:
                        raise AssertionError(f"the browser logged errors: {errors[:3]}")
                    print(f"  OK   {label:26} {url}")
                    print(f"       {len(calls.urls)} requests, all under the replay directory")
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    failures.append(f"{label} ({url}): {exc}")
                    print(f"  FAIL {label:26} {url}\n       {exc}", file=sys.stderr)
                finally:
                    page.close()
            browser.close()
    finally:
        for proc in servers:
            proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} shape(s) failed", file=sys.stderr)
        return 1
    print("\nthe staged replay drives its whole recorded flow from both path shapes, refuses")
    print("what it has no recording for, and never leaves its own directory.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
