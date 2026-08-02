"""The whole transaction, driven by a REAL BROWSER against a REAL DataHub.

    browser -> POST /audit -> pipeline -> human publishes -> 3 catalog writes
            -> GET /claims -> the verdict, read back OUT of DataHub

Run with `just e2e`. Live tier: real Edge, real model, real catalog, real money.

**WHY THIS EXISTS, precisely.** `test_live.py` looks end-to-end and is not: it drives
`fastapi.testclient.TestClient`, an IN-PROCESS ASGI transport. It never binds a port, never
loads the built bundle, and never runs a `fetch`. So until this file, **no test in the repo
executed `frontend/src/api/client.ts` or `types.ts` at all** — the TS mirror was hand-kept in
sync with `record.py` by READING it.

That is not a hypothetical gap. It shipped, twice, in one commit's worth of drift (6903d6c):

  - `DecisionRequest` carried `accept: boolean` for a full session after the backend split it
    into `publish` / `accept_correction`. The backend forbids extras, so EVERY approve the UI
    sent came back **422**. The approve flow was dead for a session.
  - the review bar counted `proposals` while Option A parks on every claim's PUBLICATION, so
    a run with one proposal and three other claims submitted one decision, re-parked, and
    could never complete — while the UI called it settled.

**Both were found by a human clicking the app. The entire suite was green through both.** It
is the Session 5 rule at the browser boundary: a fake cannot fail in a way the real thing
fails through machinery the fake does not have, and `TestClient` has no fetch, no JSON
round-trip through TypeScript, and no `extra="forbid"` rejection to surface.

`spikes/e2e_sabotage.py` (`just e2e-sabotage`) re-introduces those bugs, the correction
strand, and the same-dataset receipt alias, and proves this file
goes RED for each. An E2E that cannot fail is a green light wired to nothing.

**WHAT THIS FILE DOES NOT PROVE**, stated so nobody reads more into it:

  - It is ONE path through the UI — the demo path. It is not UI coverage.
  - It does not prove the crash-orphan window is handled — but that is no longer untestable.
    `tests/test_settlement_recovery.py` (Session 22) dies the process for real, with a SIGKILL
    at four catalog-write points and once during recovery, and proves a fresh process replays
    the settlement from a durable intent. This file's job is the browser boundary, not that.
  - The browser is whatever Edge the machine has. Pinning playwright does not pin Edge.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from attest.config import settings
from attest.datahub import DataHubError

REPO = Path(__file__).resolve().parents[1]
DIST = REPO / "frontend" / "dist"

# `2/3 decided` — the review bar's progress counter (AuditResults.tsx).
_DECIDED_RE = re.compile(r"\d+/\d+ decided")

# What `GET /claims` returns at most, mirroring the route's default (`app.get_claims`,
# `limit: ... = 50`). The frontend sends no `limit`, so this is exactly what the browser
# gets. Held here as a named constant because the repair test's decay guard is a claim
# ABOUT the route's cap, and a bare 50 in an assertion would not say so.
_CLAIMS_PAGE_SIZE = 50

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.openai_api_key, reason="no OPENAI_API_KEY"),
]

# PLAYWRIGHT IS IMPORTED INSIDE THE FIXTURE, NEVER AT MODULE SCOPE, and that is not style.
# `pytest.importorskip` at module scope runs during COLLECTION — before mark filtering — so
# on a runner without playwright (which is every CI runner: `e2e` is not in `dev`) this file
# would report a SKIP into the offline tier's run. The offline tier's whole claim is that it
# NEVER skips, and `conftest.pytest_terminal_summary` exists to make a skip loud. A skip here
# would be a false alarm about lost coverage in a tier that had not lost any, which is the
# same "green tick about a different program" this repo keeps fighting. Deferred to the
# fixture, `-m 'not live'` deselects these cleanly and nothing is imported at all.


def _free_port() -> int:
    """An EPHEMERAL port, never :8003, and the reason is that a stale process can LIE.

    :8003 is the API's pinned port and `just serve` runs it under `--reload`, whose child
    worker is what holds the socket — so Ctrl-C can leave it LISTENING with its parent gone
    (justfile, `just port`). If this test bound 8003 it would not fail against that orphan;
    it would **silently talk to it** — a different process, a different store, a different
    build of the bundle — and go green about the wrong program. That is this repo's
    characteristic bug, and a test for UI/API drift is the last place it belongs.

    An ephemeral port makes the collision unreachable rather than merely unlikely.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory) -> str:
    """A REAL uvicorn on a real socket, serving the real bundle. Not TestClient.

    No `--reload`: one process, nothing to orphan, and `terminate()` actually ends it. That
    is the same reason `just demo` does not use it.

    The store and checkpoints are redirected into a tmp dir, so this never touches the
    developer's `attest.db`. The catalog is NOT redirected and cannot be — the whole point is
    that a verdict reaches real DataHub.
    """
    if not DIST.is_dir():
        pytest.skip(f"frontend not built ({DIST} absent) — run `just ui`")

    tmp = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    env = {
        **os.environ,
        "ATTEST_STORE_PATH": str(tmp / "attest.db"),
        "ATTEST_CHECKPOINT_PATH": str(tmp / "attest-checkpoints.db"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "attest.api.app:app", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"

    import httpx

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"the API died on startup:\n{proc.stdout.read()}")
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.4)
    else:
        proc.terminate()
        pytest.fail(f"the API never came up on {base}")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    """Installed Microsoft Edge. No browser download, so no CDN and no TLS trap.

    `channel="msedge"` drives the system Edge rather than a Playwright-managed chromium.
    That is deliberate: Playwright's browser downloader is its bundled NODE driver, which on
    a TLS-inspecting network needs `NODE_USE_SYSTEM_CA=1` (CLAUDE.md's system-CA table). Not
    downloading anything avoids the entire class of problem. The cost is named in
    pyproject.toml: the browser is then whatever Edge the machine has.
    """
    sync_playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright is not installed — `pip install -e '.[e2e]'`",
    ).sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel="msedge", headless=True)
        yield b
        b.close()


class ApiCalls:
    """Every API response the BROWSER actually received. The 422 detector.

    This is what makes the `accept: boolean` regression catchable **by name** rather than
    incidentally. Without it, a 422 on approve surfaces only as "the run never completed",
    which is the same symptom as five other bugs. With it, the test says which call failed
    and what the server said about it.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, int, str]] = []

    def attach(self, page) -> None:
        def record(resp) -> None:
            path = resp.url.split("127.0.0.1", 1)[-1].split("/", 1)[-1]
            if not path.startswith(("audit", "claims", "health")):
                return
            body = ""
            if resp.status >= 400:
                try:
                    body = resp.text()[:400]
                except Exception:
                    body = "<unreadable>"
            self.seen.append((resp.request.method, resp.url, resp.status, body))

        page.on("response", record)

    @property
    def failures(self) -> list[tuple[str, str, int, str]]:
        return [c for c in self.seen if c[2] >= 400]

    def assert_all_ok(self, what: str) -> None:
        assert not self.failures, (
            f"the browser's API calls failed during {what}:\n"
            + "\n".join(f"  {m} {u} -> {s}\n    {b}" for m, u, s, b in self.failures)
        )


def test_the_whole_transaction_from_a_browser_to_datahub_and_back(
    server, browser, client, capsys
):
    """A person drives the demo, and the verdict lands in DataHub and reads back out.

    The agent output is the one `test_live.py` already proves reaches all three verdicts with
    exactly one correctable claim. That mix is required rather than decorative:

        DOCUMENTED owned by ALICE      -> Supported            no proposal
        UNREVIEWED contains no PII     -> Insufficient-Coverage no proposal, never corrected
        OWNED_BY_CAROL owned by DANA   -> Contradicted          CORRECTED, a proposal

    Three claims, ONE proposal. That ratio is what makes the `proposals()` regression fatal
    and therefore catchable: counting proposals, the bar submits one decision, the run
    re-parks on the other two, and it can never complete.
    """
    from tests.conftest import ALICE, DANA, DOCUMENTED, OWNED_BY_CAROL, UNREVIEWED

    agent_output = (
        f"The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"The dataset {UNREVIEWED} contains no PII. "
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}."
    )

    page = browser.new_page()
    calls = ApiCalls()
    calls.attach(page)
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    try:
        # --- HOP 1: the browser loads the BUILT BUNDLE from the real server -----
        page.goto(server, wait_until="networkidle")
        assert page.get_by_role("heading", name="Attest").is_visible()

        # --- HOP 2: POST /audit, driven by a human ------------------------------
        box = page.get_by_placeholder("Paste the AI agent's claims")
        box.fill(agent_output)
        page.get_by_role("button", name="Run Audit").click()

        # The audit is a real pipeline run: real model, real catalog. On completion the
        # progress screen replays the run and HOLDS — there is no auto-advance. A human clicks
        # Continue to reach the results checkpoint, and this test drives that same click. (An
        # auto-advance timer a person never sees is not a real path forward; waiting on it here
        # would have tested a clock, not the button.)
        continue_btn = page.get_by_role("button", name="Continue to results")
        continue_btn.wait_for(timeout=120_000)
        calls.assert_all_ok("the audit")
        continue_btn.click()
        page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)

        # The TS mirror deserialized a real AuditRecord. If `types.ts` had drifted from
        # `record.py`, this counter is where it shows: it is computed from
        # `awaitingPublication(record)`, which reads `claim.publication.status`.
        counter = page.get_by_text(_DECIDED_RE)
        assert counter.is_visible(), "the review bar never rendered — the record did not parse"
        assert counter.inner_text().startswith("0/"), counter.inner_text()
        total = int(counter.inner_text().split("/")[1].split()[0])
        assert total == 3, f"expected 3 claims parked for publication, got {total}"

        # --- HOP 3: a human publishes every verdict -----------------------------
        # EVERY claim carries a publish control, whatever its verdict — that is Option A,
        # and a Supported claim having no control would be the old gate creeping back.
        publish = page.get_by_role("button", name="Publish verdict", exact=True)
        assert publish.count() == 3, (
            f"expected a publish control on all 3 claims, found {publish.count()}. Every "
            "audited claim parks for publication, whatever its verdict (Option A)."
        )
        # Click FIRST, repeatedly, rather than iterating a snapshot: selecting Publish
        # rewrites the button's own label to "Publishing" (PublicationPanel.tsx), so the
        # matching set SHRINKS as we go and a captured `.all()` list goes stale under us.
        for _ in range(total):
            publish.first.click()
        assert publish.count() == 0, "a publish control did not register the click"

        # There is exactly one proposal; rule on it too, or the run rightly re-parks —
        # publishing a verdict is not accepting a correction, and that split is deliberate.
        accept = page.get_by_role("button", name="Accept correction", exact=True)
        assert accept.count() == 1, (
            f"expected exactly one correctable claim, found {accept.count()}"
        )
        accept.first.click()

        assert counter.inner_text().startswith(f"{total}/"), counter.inner_text()

        # --- HOP 4: POST /approve -> three catalog writes -----------------------
        #
        # THE APPROVE RESPONSE IS CAUGHT AT THE WIRE, BEFORE ANY WAIT ON THE UI, and that
        # ordering is the whole point rather than a style choice. Waiting on the success text
        # first and checking the calls afterwards is what the first version of this test did,
        # and under the real `accept: boolean` regression it failed as a 90-SECOND TIMEOUT
        # waiting for text that was never coming — the check written to name the 422 sat
        # below the wait and never ran. A mystery hang is the same symptom as five other
        # bugs; `expect_response` makes the 422 the FIRST thing that fails, by name, with the
        # server's own words. Measured: caught in ~2s instead of 90.
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/approve" in r.url, timeout=90_000
        ) as caught:
            page.get_by_role("button", name="Submit decisions").click()
        approve = caught.value
        assert approve.status == 200, (
            f"the browser's approve came back {approve.status}, not 200:\n"
            f"  {approve.text()[:400]}\n"
            "The UI is sending a body the API refuses. This is the exact shape of the "
            "6903d6c regression (`extra='forbid'` on a field the TS mirror invented)."
        )

        page.get_by_text("Verdict written to catalog").first.wait_for(timeout=90_000)
        calls.assert_all_ok("the approval")

        # The run SETTLED. Under the `proposals()` regression it re-parks here forever.
        assert page.get_by_role("button", name="Submit decisions").count() == 0, (
            "the run is still parked after every verdict was published — the review bar is "
            "counting something other than what the checkpoint parks on"
        )

        # --- HOP 5: the browser reads it back OUT OF DATAHUB --------------------
        page.get_by_role("button", name="Published claims").first.click()
        page.get_by_role("heading", name="What the next agent inherits").wait_for(timeout=30_000)
        page.get_by_text("In the catalog").first.wait_for(timeout=30_000)
        calls.assert_all_ok("the catalog read")

        claims_calls = [c for c in calls.seen if "/claims" in c[1]]
        assert claims_calls, "the explorer never called /claims"

        assert not console_errors, f"the browser logged errors: {console_errors[:3]}"
    finally:
        page.close()

    # --- HOP 6: AND WITHOUT ATTEST AT ALL. The second agent. --------------------
    #
    # The browser saw it, but the browser was talking to Attest. The thesis is that the
    # CATALOG carries this, so the last hop consults neither the store nor the service.
    from attest.retrieval import ClaimQuery, ClaimReader, ReadState

    reader = ClaimReader(client, store=None)
    inherited = reader.list(ClaimQuery(target_urn=OWNED_BY_CAROL))
    published = [c for c in inherited.claims if c.artifact.verdict]
    assert published, (
        f"nothing about {OWNED_BY_CAROL} is readable from DataHub alone after a human "
        "published it through the browser"
    )
    for c in published:
        assert c.state is ReadState.COMPLETE

    with capsys.disabled():
        print(f"\n\n  {'=' * 72}")
        print("  BROWSER -> API -> DATAHUB -> BROWSER, and back out with no Attest store")
        print(f"  {'=' * 72}")
        print(f"\n  API calls the browser made: {len(calls.seen)}, failures: {len(calls.failures)}")
        for m, u, s, _ in calls.seen:
            if m != "GET" or "/claims" in u:
                print(f"    {m:5} {u.split('127.0.0.1')[-1].split('/',1)[-1][:52]:54} {s}")
        print(f"\n  ClaimReader(client, store=None) on {OWNED_BY_CAROL.split(',')[1]}:")
        for c in published:
            print(f"    {c.artifact.claim_type:14} {str(c.artifact.verdict):22} {c.state.value}")
        print()


# --- the corrections-only strand: publish everything, rule NOTHING on the fix -----


def test_publishing_every_verdict_but_leaving_a_correction_pending_does_not_strand_the_run(
    server, browser, client, capsys
):
    """The one path the demo E2E never walks, and the dead-end it was hiding.

    The main test above clicks BOTH controls — every Publish AND the one Accept — so the run
    always settles. That masks a whole axis. The backend parks while EITHER is pending
    (`report.ClaimAudit.awaits_human = publication.awaits_human OR correction.awaits_human`),
    but the frontend review gate counted ONLY publications: once every verdict was published,
    `reviewMode` went false, every control went dead, and a still-pending correction re-parked
    the run into a permanent `awaiting-review` with nothing clickable. On the DEFAULT demo
    path, triggered by NOT clicking an optional-looking control.

    This is the SAME class as the `proposals()` publication-axis bug the file's header names —
    a gate blind to one of the two axes the checkpoint parks on. The fix teaches the gate to
    see BOTH (`awaitingDecision` = publication-pending OR unruled-correction), so it cannot be
    stranded and it says so: Submit stays disabled until the correction is ruled, because a
    published verdict beside an unruled proposal does NOT finish the run.

    RED-FIRST HINGE: after publishing all three verdicts with the correction still pending,
    the fixed gate reads `2/3 decided` and Submit is DISABLED (the corrected claim is not
    fully decided). The unfixed gate reads `3/3` and enables Submit — so the `2/3`/disabled
    assertion is what fails against current code, reproducing the dead-end's cause before it
    can be reached. `spikes/e2e_sabotage.py` reverts the correction gate and demands this
    goes red, permanently.
    """
    from tests.conftest import ALICE, DANA, DOCUMENTED, OWNED_BY_CAROL, UNREVIEWED

    # The same mix the main test uses, and proven by test_live: three claims, exactly ONE
    # correctable. That ratio is the whole point — publishing all three while leaving the one
    # correction pending is precisely the state the old gate could not represent.
    agent_output = (
        f"The dataset {DOCUMENTED} is owned by {ALICE}. "
        f"The dataset {UNREVIEWED} contains no PII. "
        f"The dataset {OWNED_BY_CAROL} is owned by {DANA}."
    )

    page = browser.new_page()
    calls = ApiCalls()
    calls.attach(page)
    console_errors: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    try:
        page.goto(server, wait_until="networkidle")
        page.get_by_placeholder("Paste the AI agent's claims").fill(agent_output)
        page.get_by_role("button", name="Run Audit").click()

        continue_btn = page.get_by_role("button", name="Continue to results")
        continue_btn.wait_for(timeout=120_000)
        calls.assert_all_ok("the audit")
        continue_btn.click()
        page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)

        counter = page.get_by_text(_DECIDED_RE)
        assert counter.is_visible(), "the review bar never rendered — the record did not parse"
        assert counter.inner_text().startswith("0/"), counter.inner_text()
        total = int(counter.inner_text().split("/")[1].split()[0])
        assert total == 3, f"expected 3 claims parked for publication, got {total}"

        # --- publish EVERY verdict, and deliberately rule NOTHING on the fix -----
        publish = page.get_by_role("button", name="Publish verdict", exact=True)
        assert publish.count() == 3, (
            f"expected a publish control on all 3 claims, found {publish.count()}"
        )
        for _ in range(total):
            publish.first.click()
        assert publish.count() == 0, "a publish control did not register the click"

        # THE HINGE. Every verdict is published; the one correction is untouched. A gate that
        # sees both axes reports the corrected claim as NOT fully decided (2/3) and keeps
        # Submit disabled — a decision is still required. A gate blind to the correction reads
        # 3/3 and lets you submit straight into the re-park dead-end. This assertion is what
        # goes RED against the unfixed gate, and it is caught at the wire — no 90s hang waiting
        # on text that is never coming (the file header's rule).
        after_publish = counter.inner_text()
        assert after_publish.startswith("2/"), (
            f"after publishing every verdict with the correction still pending, the counter "
            f"reads {after_publish!r}. The unfixed gate reads '3/3': it is blind to the "
            f"correction axis and Submit would strand the run on re-park."
        )
        submit = page.get_by_role("button", name="Submit decisions")
        assert submit.is_disabled(), (
            "Submit is enabled while a proposed correction is still unruled. The backend "
            "re-parks on it (report.awaits_human), so submitting here strands the run in "
            "awaiting-review with no live control — the dead-end this test exists to forbid."
        )
        # And the correction control is still LIVE — the run remains actionable, not dead.
        accept = page.get_by_role("button", name="Accept correction", exact=True)
        assert accept.count() == 1, (
            f"expected exactly one live correction control, found {accept.count()} — the fix "
            "must keep the correction decidable while the run is parked on it"
        )

        # --- rule the correction; now the gate opens and the run can finish -----
        accept.first.click()
        assert counter.inner_text().startswith("3/"), counter.inner_text()
        assert submit.is_enabled(), "Submit did not open once every axis was decided"

        with page.expect_response(
            lambda r: r.request.method == "POST" and "/approve" in r.url, timeout=90_000
        ) as caught:
            submit.click()
        approve = caught.value
        assert approve.status == 200, f"approve came back {approve.status}:\n{approve.text()[:400]}"

        page.get_by_text("Verdict written to catalog").first.wait_for(timeout=90_000)
        calls.assert_all_ok("the approval")

        # TERMINAL AND ACTIONABLE: the run settled COMPLETE, not re-parked. Under the strand
        # bug it would still be awaiting-review with the review bar gone — parked forever.
        assert page.get_by_role("button", name="Submit decisions").count() == 0, (
            "the run is still parked after every verdict was published AND the correction was "
            "ruled — the gate is still blind to an axis the checkpoint parks on"
        )
        assert not console_errors, f"the browser logged errors: {console_errors[:3]}"
    finally:
        page.close()

    with capsys.disabled():
        print(f"\n\n  {'=' * 72}")
        print("  CORRECTIONS-ONLY STRAND: publish all, rule the fix, run FINISHES (not parked)")
        print(f"  {'=' * 72}")
        print("    publish x3, correction pending -> 2/3 decided, Submit DISABLED (required)")
        print("    accept the correction          -> 3/3 decided, Submit enabled")
        print("    submit                         -> approve 200, run COMPLETE, not re-parked")
        print()


# --- the repair path, from the browser ---------------------------------------


def _await_read_state(service, claim_urn: str, want, timeout_s: float = 30.0):
    """Poll the service's read of ONE claim until it reaches `want`. Returns what it saw.

    DataHub's index is eventually consistent, so a verdict is not readable the instant it is
    written (measured median 2.1s, max 3.2s — CLAUDE.md §11). The honest read is therefore a
    poll with a bound and an answer either way, not a sleep and a hope. `None` means the
    catalog held no artifact at that URN for the whole window — a real answer, distinct from
    every ReadState, and the caller is given it rather than a crash.

    **BY IDENTITY — `service.claim(urn)` — NEVER by scanning a dataset listing, and that is
    the whole point rather than a refactor.** This used to page `service.claims(target_urn)`
    and look for the claim in the result. That read is capped (`GET /claims` defaults to 50)
    and `dataset.assertions` returns a dataset's artifacts OLDEST FIRST, while the claim this
    test makes is FRESH by construction and therefore always the NEWEST. So the moment the
    dataset accumulated more than 50 artifacts the poll was scanning 50 claims all older than
    the one it wanted, every time, and could never find it.

    That is exactly how this test died: `support_tickets` reached 62 artifacts (measured: the
    claim sat at position 60, absent at `limit=50`, present at `limit=None`), the poll
    returned `None`, and the assertion below crashed on `None.value` — a green light with an
    expiry date, and the expiry had passed. Each run of this file writes ~2 more permanent
    artifacts and nothing prunes them, so the failure was deterministic and worsening.

    An identity read is O(1), order-independent and cap-independent: it asks the catalog for
    the artifact it actually wants instead of hoping to find it in a truncated page. It
    cannot decay. See NOTES.md, "E2E runs accumulate unprunable claim artifacts".
    """
    from attest.api.service import ClaimNotFound

    seen = None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            seen = service.claim(claim_urn).state
        except ClaimNotFound:
            # Not in the catalog at all — yet, or ever. Distinct from a claim that is there
            # with no verdict, which is what the four ReadStates are about.
            seen = None
        if seen is want:
            return seen
        time.sleep(1)
    return seen


def _named(state) -> str:
    """A read-state for a failure message, including the state of having no artifact."""
    return state.value if state is not None else "<no artifact at this URN>"


def _await_dataset_visibility(
    client, dataset_urn: str, claim_urn: str, timeout_s: float = 30.0
) -> float:
    """Wait until the claim is reachable through the DATASET-SCOPED read. Returns seconds.

    **THE BROWSER AND THE BACKEND ASSERTION USE DIFFERENT ENTRY POINTS, and this is the
    barrier between them.** The state assertion above reads the artifact BY IDENTITY
    (`get_assertion`), which is a direct entity fetch and answers as soon as the entity
    exists. The explorer reads `dataset.assertions` — a RELATIONSHIP read served from an
    eventually-consistent index, which sees a newly upserted assertion a moment later. So
    "the backend can see it" does not imply "the browser can see it", and the gap is real.

    That gap used to be hidden: the state poll itself paged `service.claims(target_urn=…)`,
    the same relationship read, so it could not return until the index had caught up. Moving
    it to an identity read (correctly — see `_await_read_state`) removed that ACCIDENTAL
    barrier, and the browser step began racing the index. It passed once and failed on the
    very next run. So the barrier is made explicit here rather than left implicit somewhere
    it can be optimised away again.

    **It must be waited for HERE, before the browser is told to select the dataset, because
    the explorer FETCHES ONCE.** `ClaimsExplorer` loads on the filter change and re-polls
    only while something reads `pending-lag` — and this claim reads `incomplete`, which is
    deliberately not a polling state (a failed write never resolves itself, so a spinner over
    it would be a lie). A `wait_for` on the badge is therefore waiting on a page that will
    never re-request: it can only time out. That is the file header's own rule — a wait that
    cannot succeed is a mystery hang, not a diagnostic.

    `limit=None` paginates the whole dataset, so this cannot decay the way the capped scan
    did. The elapsed time is RETURNED and printed by the test, so the bound stays backed by a
    number measured on every run rather than by a comment written once.
    """
    started = time.monotonic()
    deadline = started + timeout_s
    while time.monotonic() < deadline:
        nodes, _ = client.list_dataset_assertions(dataset_urn, limit=None)
        if any(n.get("urn") == claim_urn for n in nodes):
            return time.monotonic() - started
        time.sleep(0.5)
    raise AssertionError(
        f"{claim_urn} is not reachable through `dataset.assertions` on {dataset_urn} after "
        f"{timeout_s:.0f}s. The entity exists (the state assertion above already read it by "
        "identity), so this is the dataset->assertion relationship index not catching up — "
        "not a missing write. The browser reads that entry point and fetches ONCE, so it "
        "could only ever show a stale page from here."
    )


def _serve_in_process(service):
    """A REAL uvicorn on a real socket, in a THREAD. Returns (base_url, stop).

    In-process ONLY because the fault has to be injected into the service's client, and the
    alternative is a fault hook inside product code. See the repair test's docstring.
    """
    import threading

    import uvicorn

    from attest.api.app import app, get_service

    app.dependency_overrides[get_service] = lambda: service
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    import httpx

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.3)
    else:
        raise AssertionError("the in-process API never came up")

    def stop() -> None:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()

    return f"http://127.0.0.1:{port}", stop


class _BreaksTheReportStep:
    """A client whose REPORT step fails while `armed`. Test-side wrapper, by necessity.

    **The fault injection is OUT-OF-BAND and stays that way.** `writeback.py` and
    `service.py` get no env-var hook, no test-only branch, and no `if broken:` — their
    correctness IS the product, and a test-only branch inside them is the one place
    scaffolding must never leak. So the failure is injected by wrapping the client the
    service was handed, which is a thing a test constructs and product code never sees.

    `armed` flips off before the repair, because a repair against a permanently broken
    client would prove only that a broken thing stays broken.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._real, name)

    def report_assertion_result(self, *args, **kwargs):
        if self.armed:
            # DataHubError, not a bare RuntimeError, because that is what a real report
            # failure raises and it is the ONLY thing `write_claim_artifact` catches
            # (writeback.py). Injecting the wrong type does not simulate a partial write —
            # it simulates an unhandled crash, which 500s the approve and tests a different
            # program. The first version of this wrapper got that wrong and the test failed
            # loudly, which is the trap working.
            raise DataHubError("injected: the report step did not land")
        return self._real.report_assertion_result(*args, **kwargs)


class _BreaksOnlyTheFirstReportStep:
    """A one-shot report failure, so two same-dataset receipts deliberately disagree."""

    def __init__(self, real) -> None:
        self._real = real
        self.armed = True

    def __getattr__(self, name):
        return getattr(self._real, name)

    def report_assertion_result(self, *args, **kwargs):
        if self.armed:
            self.armed = False
            raise DataHubError("injected: only claim 0's report step did not land")
        return self._real.report_assertion_result(*args, **kwargs)


def test_same_dataset_claim_cards_show_their_own_writeback_receipts(
    browser, client, tmp_path, capsys
):
    """Two claims can share a dataset; their publication receipts cannot share an identity.

    The first write fails at `report` and the second succeeds. Matching receipts by target
    URN paints BOTH cards with the first failure. The stable identity at this boundary is
    the claim index already used by settlement and the decision log.
    """
    from tests.conftest import CAROL, OWNED_BY_CAROL

    from attest.api.service import AuditService
    from attest.graph import Pipeline
    from attest.llm import LLM
    from attest.store import AuditStore

    window = 10_000 + int(time.time()) % 10_000
    agent_output = (
        f"The dataset {OWNED_BY_CAROL} is refreshed within {window} hours. "
        f"The dataset {OWNED_BY_CAROL} is owned by {CAROL}."
    )

    real_client = client
    broken = _BreaksOnlyTheFirstReportStep(real_client)
    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=real_client),
        store=AuditStore(tmp_path / "same-dataset-receipts.db"),
        client=broken,
    )

    base, stop = _serve_in_process(service)
    page = browser.new_page()
    calls = ApiCalls()
    calls.attach(page)
    try:
        page.goto(base, wait_until="networkidle")
        page.get_by_placeholder("Paste the AI agent's claims").fill(agent_output)
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.rstrip("/").endswith("/audit"),
            timeout=120_000,
        ) as caught:
            page.get_by_role("button", name="Run Audit").click()
        run_id = caught.value.json()["run_id"]

        page.get_by_role("button", name="Continue to results").click()
        page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)

        stored = service.get(run_id)
        assert len(stored.claims) == 2, [c.raw_text for c in stored.claims]
        assert [c.index for c in stored.claims] == [0, 1]
        assert {c.target_urn for c in stored.claims} == {OWNED_BY_CAROL}

        publish = page.get_by_role("button", name="Publish verdict", exact=True)
        assert publish.count() == 2
        for _ in range(2):
            publish.first.click()

        with page.expect_response(
            lambda r: r.request.method == "POST" and "/approve" in r.url, timeout=90_000
        ) as caught:
            page.get_by_role("button", name="Submit decisions").click()
        approve = caught.value
        assert approve.status == 200, approve.text()[:400]
        receipts = approve.json()["writebacks"]
        assert [w["ok"] for w in receipts] == [False, True], receipts

        first = page.locator("div.surface-card").filter(
            has_text=stored.claims[0].raw_text
        ).first
        second = page.locator("div.surface-card").filter(
            has_text=stored.claims[1].raw_text
        ).first
        first.get_by_text("Published, but the catalog write failed").wait_for(timeout=60_000)
        second.get_by_text("Verdict written to catalog").wait_for(timeout=60_000)
        calls.assert_all_ok("same-dataset receipt attribution")
    finally:
        page.close()
        stop()

    with capsys.disabled():
        print("\n\n  SAME DATASET, TWO RECEIPTS, TWO CARDS")
        print("    claim 0 -> failed receipt -> failed card")
        print("    claim 1 -> success receipt -> success card")

def test_a_half_written_claim_reads_incomplete_and_a_human_repairs_it_from_the_browser(
    browser, client, tmp_path, capsys
):
    """The write is three steps and cannot be atomic. This is the recovery, driven by hand.

    `report` is broken during the approval, so the claim artifact exists with NO verdict —
    `incomplete`, the one read-state that is a real bug and the only one that is repairable.
    Then a human clicks Repair in the browser and the verdict lands.

    **THE SERVER IS IN-PROCESS HERE, and the other test's is a subprocess. That is a real
    difference and it is deliberate.** The main test runs `python -m uvicorn ...` exactly as
    `just demo` does, which proves the shipped command works. This one needs a client whose
    report step fails, and injecting that into a subprocess would mean a fault hook in
    product code. An in-process uvicorn is still a real server on a real socket answering a
    real browser over real HTTP — the only thing given up is process separation, and what is
    bought is that `writeback.py` stays free of scaffolding. That trade is the right way
    round.

    **THE CLAIM IS FRESH ON EVERY RUN, AND IT HAS TO BE.** A claim artifact is
    CONTENT-ADDRESSED, so a fixed claim maps to the same URN forever. Run this twice and the
    second run's `report` failure lands on an artifact that ALREADY carries the verdict the
    first run repaired: it reads `complete`, the Repair control never appears, and the test
    fails for a reason that has nothing to do with the code. That is CLAUDE.md's
    content-addressing trap — *demo read-states on fresh claims* — and it bites tests exactly
    as it bites demos.

    **Deleting the artifact instead does NOT work, and that was measured here rather than
    assumed.** `DELETE /openapi/v3/entity/assertion/{urn}` returns **HTTP 200** and the
    artifact still reads back `complete` with its verdict history intact — the timeseries run
    events survive the entity delete. A delete that reports success and changes nothing is
    the loud-then-silent failure this repo already names for the TLS repair, and building the
    test on it would have made the test lie the moment it went green once.

    So the freshness window varies per run. It is an odd-looking number and a REAL claim: a
    genuine `FreshnessClaim` about a real dataset, decided by the real checker.
    `RECENTLY_MODIFIED` carries a fresh timestamp — the seed declares freshness claims about
    it Supported — so any window in this range is honestly Supported, which keeps the
    correction loop out of a test that is about the write's tail, not about the loop.

    **AND THE DATASET IS NOT `support_tickets` ANY MORE, WHICH IS THE OTHER HALF.** A fresh
    claim per run means a new permanent artifact per run, and nothing prunes them (the DELETE
    above). The browser step below reads `GET /claims`, which is capped at 50 and returns a
    dataset's artifacts oldest first — so once a dataset passes 50 artifacts, the claim this
    test just made is off the page and its badge can never render. `support_tickets` reached
    62 (this file hammered it from three tests), and this test went permanently red. Moving to
    a dataset nothing else in the E2E writes to restores the headroom; the guard below makes
    the day it runs out an immediate named failure instead of a 30-second mystery. The real
    fix is pagination in the UI, and that stays an honestly documented deferred item.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from tests.conftest import RECENTLY_MODIFIED

    from attest.api.service import AuditService
    from attest.graph import Pipeline
    from attest.llm import LLM
    from attest.retrieval import ClaimQuery, ClaimReader, ReadState
    from attest.store import AuditStore

    # THE DECAY GUARD, and it is this test's own history rather than belt-and-braces.
    # Checked BEFORE the audit, so a saturated dataset costs no tokens and fails by name.
    _, held = client.list_dataset_assertions(RECENTLY_MODIFIED, limit=None)
    assert held < _CLAIMS_PAGE_SIZE, (
        f"{RECENTLY_MODIFIED} holds {held} claim artifacts and `GET /claims` returns at most "
        f"{_CLAIMS_PAGE_SIZE}, oldest first. The fresh claim this test is about to publish "
        "would be off the page, so the browser could never see its badge — which is how this "
        "test died on `support_tickets` at 62 artifacts. Artifacts cannot be deleted (the "
        "DELETE returns 200 and removes nothing), so the remedy is `just reset` to rebuild "
        "the catalog from the seed. See NOTES.md."
    )

    window = 1000 + int(time.time()) % 9000
    agent_output = f"The dataset {RECENTLY_MODIFIED} is refreshed within {window} hours."

    real_client = client
    broken = _BreaksTheReportStep(real_client)
    service = AuditService(
        pipeline=Pipeline(llm=LLM(), client=real_client),
        store=AuditStore(tmp_path / "attest.db"),
        client=broken,
    )

    base, stop = _serve_in_process(service)
    page = browser.new_page()
    calls = ApiCalls()
    calls.attach(page)
    try:
        page.goto(base, wait_until="networkidle")
        page.get_by_placeholder("Paste the AI agent's claims").fill(agent_output)
        with page.expect_response(
            lambda r: r.request.method == "POST" and r.url.rstrip("/").endswith("/audit"),
            timeout=120_000,
        ) as caught:
            page.get_by_role("button", name="Run Audit").click()
        run_id = caught.value.json()["run_id"]
        # The replay holds on completion; a human clicks Continue to reach the checkpoint.
        page.get_by_role("button", name="Continue to results").click()
        page.get_by_role("heading", name="Audit Complete").wait_for(timeout=30_000)

        # The artifact this claim will land on — DERIVED from the claim the pipeline actually
        # stored, never re-implemented here. The URN is a sha256 over the claim's canonical
        # JSON; deriving it a second way in the test would be two implementations of an
        # identity that must never disagree, which is the reason the UI does not do it either.
        from attest import writeback as wb

        stored = service.get(run_id)
        assert len(stored.claims) == 1, [c.raw_text for c in stored.claims]
        # The OTHER thing that can drift out from under this test: the seeded `fresh`
        # timestamp ages, and once the dataset is older than the window floor (1000h) this
        # claim becomes Contradicted and drags in the correction loop, which this test is
        # not about. Assert the precondition here so that arrives as one clear line rather
        # than as a confusing extra control on the checkpoint screen. Remedy: `just seed`.
        assert stored.claims[0].verdict == "Supported", (
            f"the freshness claim came back {stored.claims[0].verdict}, not Supported. "
            f"{RECENTLY_MODIFIED} has aged past the {window}h window this test asserts, so "
            "the seeded timestamps need regenerating: `just seed`."
        )
        artifact_urn = wb.claim_urn(stored.claims[0].claim)

        # NOT deleted first, and the docstring says why: the delete returns 200 and leaves
        # the verdict history standing. This claim is fresh because its CONTENT is fresh.

        # --- publish. The report step is armed to fail. ------------------------
        page.get_by_role("button", name="Publish verdict", exact=True).first.click()
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/approve" in r.url, timeout=90_000
        ) as caught:
            page.get_by_role("button", name="Submit decisions").click()
        assert caught.value.status == 200, caught.value.text()[:300]

        # THE DECISION STANDS AND THE CATALOG DOES NOT KNOW. The UI says so rather than
        # reporting a success — a silent failure here would leave DataHub disagreeing with
        # the audit history and nobody any the wiser.
        page.get_by_text("catalog write failed").first.wait_for(timeout=60_000)

        # --- the reader sees INCOMPLETE, not "no verdict" ----------------------
        broken.armed = False  # the network "recovers"; the recorded failure remains

        # ASSERT THE BACKEND STATE FIRST, so a failure below names the layer that is wrong.
        # Without this, "the UI never showed Incomplete" is indistinguishable from "the
        # backend never produced Incomplete", and the first version of this test spent its
        # time hunting the UI for a state the read had not reached.
        state = _await_read_state(service, artifact_urn, ReadState.INCOMPLETE)
        assert state is ReadState.INCOMPLETE, (
            f"the half-written claim reads {_named(state)}, not incomplete.\n"
            "  <no artifact at this URN>: the `upsert` step did not land either, so there is "
            "nothing half-written to read — the injected failure is at `report`, which runs "
            "after it.\n"
            "  complete: a verdict is present when none should be. This claim was not fresh, "
            "so it is showing an EARLIER run's verdict and the state under test is masked "
            "(the content-addressing trap — the freshness window must vary per run).\n"
            "  unknown/pending-lag: the catalog is silent and Attest's own record disagrees "
            "with the write that was just recorded as failed."
        )

        # THE INDEX BARRIER, crossed BEFORE the browser is asked to look. The explorer
        # fetches once per filter change, so it must not be pointed at the dataset until the
        # relationship read can actually return this claim. See `_await_dataset_visibility`.
        visible_after = _await_dataset_visibility(client, RECENTLY_MODIFIED, artifact_urn)

        page.get_by_role("button", name="Published claims").first.click()
        page.get_by_role("heading", name="What the next agent inherits").wait_for(timeout=30_000)

        # SCOPE TO THE DATASET, and this is not tidiness. The explorer opens UNFILTERED,
        # which is `searchAcrossEntities` — an EVENTUALLY-CONSISTENT index that has not seen
        # an artifact upserted seconds ago. Naming the dataset switches the entry point to
        # `dataset.assertions`, which is scoped to the entity. The first version of this test
        # asserted on the unfiltered list, found the word "Incomplete" belonging to some OTHER
        # run's card, and then looked for a Repair button that was never going to be there.
        # Scoping is what makes the assertions be about OUR claim.
        page.get_by_label("Dataset").select_option(RECENTLY_MODIFIED)
        badge = page.get_by_text("Incomplete").first
        try:
            badge.wait_for(timeout=30_000)
        except PlaywrightTimeout as exc:
            # NEVER let this be a bare 30-second timeout. The explorer fetches ONCE per
            # filter change, so "the badge is not there" has several very different causes —
            # the listing did not contain the claim, it contained it in another state, or the
            # request failed — and a timeout cannot tell them apart. That is the file
            # header's rule (a mystery hang is the same symptom as five other bugs), and it
            # is exactly what cost this test two sessions of misdiagnosis. So the failure
            # says what the backend served and what the browser asked for.
            listed = service.claims(ClaimQuery(target_urn=RECENTLY_MODIFIED))
            asked = [(m, u.split("127.0.0.1")[-1], s) for m, u, s, _ in calls.seen]
            # WHICH QUERY IS THE PAGE ACTUALLY SHOWING? The explorer renders the entry point
            # it was served ("Where this was filtered"). If the dropdown names the dataset
            # but the page still shows `searchAcrossEntities`, the browser is displaying the
            # response to a DIFFERENT request than the one the filter asked for.
            showing = (
                "searchAcrossEntities"
                if page.get_by_text("searchAcrossEntities").count()
                else "dataset.assertions"
                if page.get_by_text("dataset.assertions").count()
                else "<no retrieval panel>"
            )
            selected = page.get_by_label("Dataset").input_value()
            rendered = "\n".join(
                f"      {c.state.value:12} verdict={c.artifact.verdict!s:22} "
                f"{c.artifact.claim_urn}"
                for c in listed.claims
            )
            raise AssertionError(
                "the explorer never rendered an `Incomplete` badge for the half-written "
                f"claim.\n  looking for : {artifact_urn}\n"
                f"  entry point : {listed.retrieval.entry_point}, considered "
                f"{listed.retrieval.considered} of {listed.retrieval.total}\n"
                f"  backend served this listing for the dataset:\n{rendered or '      (empty)'}\n"
                f"  the PAGE is showing : {showing}\n"
                f"  the dropdown says   : {selected}\n"
                f"  browser API calls: {asked}"
            ) from exc

        # EXPAND THE CARD. The repair control lives inside the collapsed panel
        # (ClaimArtifactCard.tsx), so it is not in the DOM until a human opens the card —
        # which is exactly what a human does, and why this drives it rather than reaching
        # past it. The badge is the summary; the remedy is one click in.
        badge.click()

        repair = page.get_by_role("button", name="Repair write")
        repair.first.wait_for(timeout=10_000)
        assert repair.count() == 1, (
            f"expected one repair control on the half-written claim, found {repair.count()}. "
            "`incomplete` is the one read-state that is actionable."
        )

        # --- a human repairs it ------------------------------------------------
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/writeback" in r.url, timeout=90_000
        ) as caught:
            repair.first.click()
        assert caught.value.status == 200, caught.value.text()[:300]
        calls.assert_all_ok("the repair")

        # THE REPAIR LANDED. Polled, because a verdict is not readable the instant it is
        # written — measured median 2.1s, max 3.2s (CLAUDE.md §11). The bound below is ~10x
        # that, so a timeout here is NOT "DataHub was slow": it means the write did not land.
        # That distinction is what keeps this assertion honest rather than flaky, and it is
        # why this polls the STATE rather than sleeping a fixed time and hoping.
        state = _await_read_state(service, artifact_urn, ReadState.COMPLETE, timeout_s=30)
        assert state is ReadState.COMPLETE, (
            f"the repaired claim reads {_named(state)} after 30s. The readable-verdict lag "
            "is a measured ~2s, so this is a write that did not land, not an index catching "
            "up."
        )
    finally:
        page.close()
        stop()

    # --- and the catalog really holds it now, read with no store ---------------
    #
    # Not racing: COMPLETE is decided by DataHub ALONE (`read_state` asks the catalog first
    # and its answer is final), so once the poll above saw COMPLETE, a reader with no store
    # sees it too. The store can only ever EXPLAIN an absent verdict, never conjure one.
    # DELIBERATELY the DATASET-SCOPED read, not `reader.get(urn)`: this is the thesis query
    # ("show me what the next agent inherits about this dataset") answered by a reader with
    # no store, and that is worth exercising here as well as by identity. It is therefore the
    # one scan left in this test that a saturated dataset could decay, so the failure names
    # that possibility instead of leaving it to be rediscovered.
    reader = ClaimReader(client, store=None)
    inherited = reader.list(ClaimQuery(target_urn=RECENTLY_MODIFIED))
    got = [c for c in inherited.claims if c.artifact.claim_urn == artifact_urn]
    assert got, (
        f"{artifact_urn} is not on the dataset after the repair. The read considered "
        f"{inherited.retrieval.considered} of {inherited.retrieval.total} artifacts; if those "
        "differ, "
        "this listing was TRUNCATED at the cap and the claim is newer than everything on the "
        "page rather than absent from the catalog — `just reset`. See NOTES.md."
    )
    assert got[0].state is ReadState.COMPLETE
    assert got[0].artifact.verdict == "Supported"
    # EXACTLY ONE verdict, not two. The repair re-runs an idempotent write keyed by the run's
    # own timestamp; a `now()` in there would append a second verdict for an audit that never
    # happened, corrupting an append-only history.
    assert len(got[0].artifact.history) == 1, (
        f"the repair appended {len(got[0].artifact.history)} verdicts for one audit"
    )

    with capsys.disabled():
        print(f"\n\n  {'=' * 72}")
        print("  A HALF-WRITTEN CLAIM, REPAIRED FROM THE BROWSER")
        print(f"  {'=' * 72}")
        print(f"\n  {artifact_urn}")
        print(f"    dataset.assertions saw it after {visible_after:.1f}s (relationship index)")
        print("    report step broken -> read `incomplete`, Repair offered")
        print("    POST /writeback    -> read `complete`, same artifact URN")
        print(f"    verdict            : {got[0].artifact.verdict}")
        print(f"    history            : {len(got[0].artifact.history)} verdict (not 2)")
        print()
