"""Capture the REPLAY fixtures: one real audit, through the real API, recorded verbatim.

WHAT THIS IS FOR. `docs/replay/` serves the real frontend to a judge who will not run an
8 GB Docker stack. It is the same React app, every real component, with ONE module swapped
(`api/replayClient.ts` in place of `api/client.ts`) so every call is answered from the JSON
this script writes. A replay is only worth anything if the thing being replayed is real, so
nothing here is authored: each fixture is the exact HTTP response body a browser received.

WHAT IS DELIBERATELY NOT DONE.

  * Nothing is reconstructed. The parked record (`POST /audit`) and the settled one
    (`POST /audit/{id}/approve`) are BOTH captured, because `AuditProgress` replays the
    parked run and `AuditResults` shows the settled one. Deriving one from the other — say,
    flipping `publication.status` back to `pending` — would be Attest fabricating its own
    audit trail, which is the one thing it exists not to do.
  * Nothing is edited after capture. `health.datahub_ui_url` names `localhost:9002`, so the
    replay's "View in DataHub" links are dead for a remote reader. That is the honest
    response; the replay banner says the links point at the local catalog that produced this
    record. A doctored URL would be a fabricated response body.
  * The decision set is READ OFF the run, never hardcoded. Whether the correction loop
    produced a proposal is the model's call at run time; `decisions.json` records what was
    actually submitted, and the replay client refuses any other decision set against it.

A DIRTY WORKING TREE IS REFUSED. The manifest names the commit this recording was produced
by, and the replay banner shows it; captured from a tree that does not match that commit, the
SHA names code that did not produce these fixtures. The hashes would still prove the fixture
bytes are unedited and the provenance beside them would still be approximate — a hole in the
one claim the artifact exists to make. `--allow-dirty` is the local-experiment escape hatch
and launders nothing: the flag stays true and the banner renders it as a warning. See
`_provenance`, which also explains why the flag used to be true on every capture ever taken.

THE PROCESS BOUNDARY IS HERE, not in the capture logic. This launches the SHIPPED uvicorn
command on an ephemeral port with its own store, exactly as `spikes/smoke_runner.py` does —
so a stray `just serve` on :8003 is neither used nor disturbed. The catalog it reads and
writes is the real one.

IT IS RESUMABLE, AND THAT IS NOT A CONVENIENCE. A claim artifact is content-addressed and
its verdict history is append-only, so every capture run appends one more verdict event to
the three artifacts — permanently, on a real catalog (`DELETE` on an assertion returns 200
and leaves the timeseries standing; only `just reset` clears it). A crash in the cheap tail
of this script must therefore never cost the expensive head. So the store lives at a FIXED
path, each fixture is written the moment it exists, and `--resume` re-captures only the
`GET /claims` matrix against the run already recorded in `audit-parked.json`.

Run: `just replay-capture` (needs DataHub up, seeded, and OPENAI_API_KEY).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "frontend" / "src" / "replay"
# Fixed, outside the repo, so `--resume` can reach the same store — see the module docstring.
STATE = Path(tempfile.gettempdir()) / "attest-replay-capture"

SOURCE_AGENT = "attest-ui"
REVIEWER = "attest-ui"

CUSTOMER_PROFILE = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customers.customer_profile,PROD)"
)
RAW_EVENTS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.staging.raw_events,PROD)"

# The three claims of the recorded demo, verbatim. Two verdicts on `customer_profile` (a
# Contradicted classification that the agent can honestly revise, and a Supported ownership)
# and one Insufficient-Coverage on the unowned `raw_events` — all three verdicts, one
# correction, two datasets, in one audit. The URNs are seeded ones, quoted not minted, and
# they are interpolated rather than typed out only to keep the source under the line limit:
# the prose that reaches the API must stay byte-for-byte what the recorded run submitted.
SAMPLE = "\n\n".join(
    (
        "Findings from the data platform review:",
        f"The dataset {CUSTOMER_PROFILE} contains no PII.",
        f"The dataset {CUSTOMER_PROFILE} is owned by alice.chen.",
        f"The dataset {RAW_EVENTS} is owned by bob.martinez.",
    )
)

VERDICTS = ("Supported", "Contradicted", "Insufficient-Coverage")
CLAIM_TYPES = ("freshness", "ownership", "classification", "schema")


def _claims_filter_matrix() -> list[dict[str, str]]:
    """Every `GET /claims` filter combination this replay records.

    THE SHAPE OF THIS MATRIX IS THE POINT, and it is chosen to keep the push-down disclosure
    TRUE rather than to be exhaustive. `verdict` and `claim_type` are pushed down by
    `searchAcrossEntities`; naming `target_urn` switches the entry point to
    `dataset.assertions`, which scopes server-side and filters nothing. Capturing those
    combinations preserves what DataHub really applied, so the "Where this was filtered"
    panel in the replay is the API's own answer and not a client-side guess.

    `reviewer` and `since` are never pushed down at EITHER entry point, so the replay client
    applies them locally over a captured page — which is exactly what the real backend does.

    Anything outside this matrix is answered "not recorded in this replay", never an empty
    list: a replay that returned no rows for an uncaptured query would be reporting absence
    as an answer, in the read path of the product built to refuse that.
    """
    combos: list[dict[str, str]] = [{}]
    for verdict in VERDICTS:
        combos.append({"verdict": verdict})
    for claim_type in CLAIM_TYPES:
        combos.append({"claim_type": claim_type})
    for verdict in VERDICTS:
        for claim_type in CLAIM_TYPES:
            combos.append({"verdict": verdict, "claim_type": claim_type})
    for urn in (CUSTOMER_PROFILE, RAW_EVENTS):
        combos.append({"target_urn": urn})
        for verdict in VERDICTS:
            combos.append({"target_urn": urn, "verdict": verdict})
    return combos


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(proc: subprocess.Popen[str], base: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"the capture server exited during startup:\n{output[-2000:]}")
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise RuntimeError(f"the capture server did not start on {base} within {timeout_s}s")


def _stop(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _body(response: httpx.Response, what: str) -> Any:
    if response.status_code >= 400:
        raise RuntimeError(f"{what} failed: HTTP {response.status_code} {response.text[:600]}")
    return response.json()


def _read_claims(base: str, filters: dict[str, str], attempts: int = 4) -> Any:
    """One `GET /claims`, ON ITS OWN CONNECTION, retried rather than aborting the capture.

    A POOLED connection is what broke this twice. Walking the 28-combination matrix through
    one `httpx.Client` hung on a read past 90s, four attempts running — while the identical
    query answered in 0.3s over a fresh socket, against the same store and the same catalog.
    The server never saw the request (nothing in its access log), which is the signature of a
    keep-alive connection the server had already closed: the request goes into a dead socket
    and the client waits for a response that was never going to come. Retrying over the same
    pool just hands back the same corpse.

    So each read gets its own client. It costs a TCP handshake against localhost and removes
    the failure entirely. Abandoning a capture is the one cost here that cannot be undone —
    re-running it appends another permanent verdict event to three real artifacts.
    """
    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(base_url=base, timeout=90) as fresh:
                return _body(fresh.get("/claims", params=filters), "GET /claims")
        except httpx.HTTPError as exc:
            if attempt == attempts:
                raise RuntimeError(f"GET /claims {filters} failed {attempts}x: {exc}") from exc
            print(f"     retrying {filters} after {type(exc).__name__}")
            time.sleep(2.0 * attempt)
    raise AssertionError("unreachable")


def _decisions_for(parked: dict[str, Any]) -> list[dict[str, Any]]:
    """The decision set to submit, READ OFF the parked run.

    Publish every claim, whatever its verdict — the Option A rule: a verdict Attest never
    records is indistinguishable from a claim Attest never checked. Rule on a correction only
    where one was actually proposed; whether the loop produced one is the model's call at run
    time, and asserting it here would hardcode an outcome this script is meant to observe.
    """
    decisions: list[dict[str, Any]] = []
    for claim in parked["claims"]:
        decision: dict[str, Any] = {
            "claim_index": claim["index"],
            "publish": True,
            "reviewer": REVIEWER,
        }
        correction = claim["correction"]
        if correction["outcome"] == "corrected" and correction["review"] == "pending":
            decision["accept_correction"] = True
        decisions.append(decision)
    return decisions


def _await_readable(base: str, run_id: str, timeout_s: float = 30) -> None:
    """Wait until every claim this run published reads back COMPLETE from the catalog.

    `pending-lag` is the normal state for the first seconds after an approval (median 2.1s,
    max 3.2s measured on the pinned server) and it resolves itself. Capturing mid-lag would
    freeze a transient into a fixture: the replay's explorer would poll five times over a
    state that can never change and then hand to a human for nothing.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = _read_claims(base, {})
        mine = [c for c in body["claims"] if c["audit_run"] == run_id]
        if len(mine) == 3 and all(c["state"] == "complete" for c in mine):
            return
        time.sleep(1.0)
    raise RuntimeError(
        f"this run's claims did not all read `complete` within {timeout_s}s. Capturing now "
        "would freeze a transient read-state into a committed fixture."
    )


def _write(path: Path, payload: Any) -> str:
    """Write one fixture, LF-ONLY, and return the sha256 of the bytes that reach disk.

    `newline=""` IS LOAD-BEARING, and leaving it out shipped a broken pin. Python's text mode
    translates `\\n` to `\\r\\n` on Windows, so the fixtures were written CRLF and
    `manifest.json` recorded the hashes of those CRLF bytes — while git, with
    `core.autocrlf=true`, normalised them to LF on the way into the index. The manifest
    therefore described bytes that existed on exactly one machine: the hash pin passed for the
    author and failed in CI on the very first push, comparing an LF checkout against a CRLF
    digest.

    A recording's hash has to be a fact about the recording, not about the operating system
    that wrote it. So: LF here, `text eol=lf` in `.gitattributes` so every checkout agrees, and
    `test_replay_fixtures` fails on a stray CR rather than letting the next platform find out.
    """
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n"
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.strip()


def _dirty_paths() -> list[str]:
    """Every path git reports as modified, staged or untracked, with its status code intact.

    NOT via `_git`, which strips — and strip eats the leading space of porcelain's two-column
    status code, so the first entry rendered as `M file` (staged) while identical unstaged
    entries below it rendered ` M file`. A refusal that misreports the state of the first path
    it names is a diagnostic arguing with itself.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def _provenance(allow_dirty: bool) -> tuple[str, bool]:
    """The commit this recording was produced BY, and whether the tree actually matched it.

    MEASURED HERE, BEFORE THE SERVER STARTS AND BEFORE A SINGLE FIXTURE IS WRITTEN — and the
    timing is the whole fix, not a detail. The manifest used to compute this at the END of
    `capture()`, AFTER the five fixtures had been written to the working tree. Those writes
    dirty the tree by construction: every capture mints a new run id and new timestamps, so
    the bytes always move. `capturing_commit_dirty` was therefore **structurally true on
    every capture**, including a perfectly clean one. It was not recording the provenance of
    the CODE; it was recording the capture's own output, one line after producing it.

    That is worse than a missing check, because the field looked like a provenance guarantee
    and was one the capture could never satisfy. And it sits on the artifact whose entire
    purpose is provenance: the manifest's hashes prove the fixture BYTES have not been edited
    since capture, and this flag is the only thing that says the CODE which produced them is
    the code at `capturing_commit`. A reader who trusts the hashes has no reason to suspect
    the commit beside them is approximate.

    So the question is asked once, at the only moment it has an answer: before this script
    touches anything. A dirty tree is REFUSED rather than recorded, because a recording whose
    provenance is already known to be unverifiable is not worth the three permanent verdict
    events it appends to a real catalog. `--allow-dirty` exists for local experimentation and
    does not launder anything — the flag stays true and the banner says so, visibly.
    """
    dirty = _dirty_paths()
    if dirty and not allow_dirty:
        listed = "\n  ".join(dirty[:20])
        more = f"\n  ... and {len(dirty) - 20} more" if len(dirty) > 20 else ""
        raise SystemExit(
            "REFUSING to capture from a dirty working tree.\n\n"
            f"  {listed}{more}\n\n"
            "The manifest records the commit this recording was produced by, and the replay "
            "banner shows it. Captured from a tree that does not match that commit, the SHA "
            "names code that did not produce these fixtures — a provenance claim with a hole "
            "in it, on the artifact whose whole purpose is provenance.\n\n"
            "Commit or stash the above and re-run. `--allow-dirty` captures anyway for local "
            "experimentation; the manifest keeps `capturing_commit_dirty: true` and the "
            "banner renders a visible warning, so such a capture cannot be shipped quietly."
        )
    return _git("rev-parse", "HEAD"), bool(dirty)


def _run_audit(client: httpx.Client) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """The expensive, unrepeatable half: one real audit and one real human decision."""
    parked = _body(
        client.post("/audit", json={"agent_output": SAMPLE, "source_agent": SOURCE_AGENT}),
        "POST /audit",
    )
    run_id = parked["run_id"]
    print(
        f"  audit            run={run_id[:8]} status={parked['status']} "
        f"claims={len(parked['claims'])} trajectory_ok={parked['receipts']['trajectory_ok']}"
    )
    if parked["status"] != "awaiting-review":
        raise RuntimeError(
            f"the run did not park at the human checkpoint (status={parked['status']!r}). "
            "The replay needs a parked record to hand to AuditProgress."
        )

    decisions = _decisions_for(parked)
    approval = _body(
        client.post(f"/audit/{run_id}/approve", json={"decisions": decisions}),
        "POST /audit/{run_id}/approve",
    )
    settled = approval["audit"]
    print(
        f"  approve          status={settled['status']} "
        f"writebacks={[w['ok'] for w in approval['writebacks']]}"
    )
    if settled["status"] != "complete":
        raise RuntimeError(
            f"the run did not settle (status={settled['status']!r}); the replay would show a "
            "checkpoint that never closes."
        )
    failed = [w for w in approval["writebacks"] if not w["ok"]]
    if failed:
        raise RuntimeError(
            f"a catalog write failed: {failed}. The replay records the happy path; re-run "
            "once the catalog is healthy rather than shipping a broken write."
        )

    # Written BEFORE the claims matrix: a failure down there must not cost this run.
    OUT.mkdir(parents=True, exist_ok=True)
    _write(OUT / "audit-parked.json", parked)
    _write(OUT / "approval.json", approval)
    _write(OUT / "decisions.json", decisions)
    return parked, decisions, approval


def capture(base: str, resume: bool, provenance: tuple[str, bool]) -> None:
    with httpx.Client(base_url=base, timeout=300) as client:
        health = _body(client.get("/health"), "GET /health")
        if health.get("datahub") != "up":
            raise RuntimeError(
                f"DataHub is not reachable from the capture server (health.datahub="
                f"{health.get('datahub')!r}). A replay must be captured against the real catalog."
            )
        print(f"  health           datahub={health['datahub']} model={health['model']}")
        OUT.mkdir(parents=True, exist_ok=True)
        _write(OUT / "health.json", health)

        if resume:
            parked = json.loads((OUT / "audit-parked.json").read_text(encoding="utf-8"))
            decisions = json.loads((OUT / "decisions.json").read_text(encoding="utf-8"))
            approval = json.loads((OUT / "approval.json").read_text(encoding="utf-8"))
            print(
                f"  audit            REUSED run={parked['run_id'][:8]} "
                "(no new audit, no new verdict event)"
            )
        else:
            parked, decisions, approval = _run_audit(client)
        run_id = parked["run_id"]

        _await_readable(base, run_id)

        recorded: list[dict[str, Any]] = []
        for filters in _claims_filter_matrix():
            recorded.append({"filters": filters, "response": _read_claims(base, filters)})
        print(f"  claims           {len(recorded)} filter combinations recorded")
        _write(OUT / "claims.json", recorded)

    hashes = {
        name: hashlib.sha256((OUT / name).read_bytes()).hexdigest()
        for name in (
            "health.json",
            "audit-parked.json",
            "approval.json",
            "claims.json",
            "decisions.json",
        )
    }
    manifest = {
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run_id": run_id,
        "datahub_core_version": httpx.get("http://localhost:8080/config", timeout=10).json()[
            "versions"
        ]["acryldata/datahub"]["version"],
        "model": health["model"],
        # Both read off the tree as it stood BEFORE this script wrote anything — see
        # `_provenance`. Computed here they would describe the fixtures just written.
        "capturing_commit": provenance[0],
        "capturing_commit_dirty": provenance[1],
        "files": hashes,
    }
    _write(OUT / "manifest.json", manifest)

    print(f"\n  -> {OUT.relative_to(REPO)}")
    for name, digest in hashes.items():
        print(f"     {digest[:16]}  {name}")
    print(f"     run {run_id}  ·  DataHub Core {manifest['datahub_core_version']}")
    print(f"     commit {provenance[0][:7]}  ·  dirty {str(provenance[1]).lower()}")


def main() -> int:
    args = sys.argv[1:]
    resume = "--resume" in args
    if resume and not (OUT / "audit-parked.json").exists():
        raise SystemExit("--resume needs a previous capture's audit-parked.json; there is none.")

    # FIRST, before the server, the audit and the three permanent verdict events. A capture
    # that is going to be refused must be refused while refusing is still free.
    provenance = _provenance(allow_dirty="--allow-dirty" in args)

    STATE.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ATTEST_STORE_PATH"] = str(STATE / "attest.db")
    env["ATTEST_CHECKPOINT_PATH"] = str(STATE / "attest-checkpoints.db")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "attest.api.app:app", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    print(f"capture server on {base}  ·  store {STATE}  ·  :8003 untouched")
    try:
        _wait_for_server(proc, base)
        capture(base, resume, provenance)
    finally:
        _stop(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
