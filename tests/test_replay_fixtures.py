"""The replay fixtures are only honest while they are still the API's own answers.

`docs/replay/` ships a bundle whose every response comes from `frontend/src/replay/*.json`,
captured off one real run (`spikes/capture_replay.py`). Nothing in the browser can tell a
captured response from an edited one, and the page is the most public thing this project has.
So three properties are pinned here, in the OFFLINE tier — no DataHub, no key, no network:

  1. **Every fixture still parses through the REAL response models.** Not a hand-written
     schema: the actual pydantic types `api/app.py` declares. This is the same instinct as
     `test_fixture_drift.py` one layer up — a captured artifact is exactly as trustworthy as
     the thing that re-checks it. When a wire type changes, this goes red and the fixtures
     have to be RE-CAPTURED rather than hand-patched into agreement.

  2. **The manifest's hashes match the files.** A hand-edit is the failure mode that matters:
     a verdict flipped, a number improved, a `null` filled in. The capture writes the hashes;
     this is what makes them mean something. `_edited` proves the check catches one.

  3. **The fixtures agree with each other.** The parked record, the settled record, the
     decision set and the catalog reads are four views of ONE run. A claim URN in one and not
     the others would mean the replay was assembled from more than one recording — which is
     exactly the fabrication the whole build is arranged to make impossible.

The BOUNDARY, so nobody reads more into a green: this proves the fixtures are unmodified and
still structurally what the API returns. It does not prove the CATALOG still says what they
recorded — nothing offline can, and the replay does not claim it does. It says it is a
recording, and dates it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from attest.api.schemas import ApprovalResponse, ClaimsResponse, DecisionRequest, HealthResponse
from attest.record import AuditRecord

REPLAY = Path(__file__).resolve().parents[1] / "frontend" / "src" / "replay"

HASHED = ("health.json", "audit-parked.json", "approval.json", "claims.json", "decisions.json")


def _load(name: str):
    return json.loads((REPLAY / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load("manifest.json")


def test_the_replay_fixtures_are_all_present() -> None:
    """A missing fixture is a bundle that cannot build, caught here rather than at deploy."""
    missing = [n for n in (*HASHED, "manifest.json") if not (REPLAY / n).exists()]
    assert not missing, f"the replay is missing {missing}. Re-run `just replay-capture`."


def test_health_parses_through_the_real_response_model() -> None:
    HealthResponse.model_validate(_load("health.json"))


def test_the_parked_record_parses_and_is_actually_parked() -> None:
    """`AuditProgress` replays THIS one, and it must be the pre-decision record.

    If it were the settled record, the replay would open with every verdict already published
    and the human checkpoint — the whole point of the flow — would have nothing to decide.
    """
    parked = AuditRecord.model_validate(_load("audit-parked.json"))
    assert parked.status.value == "awaiting-review", parked.status
    assert parked.claims, "a replay of an audit with no claims has nothing to show"
    assert all(c.publication.status.value == "pending" for c in parked.claims), (
        "a claim in the parked record is already published — this is not the pre-decision "
        "response, and the replay's checkpoint would be a formality"
    )
    assert parked.receipts.trajectory_ok, (
        "the recorded run FAILED its own trajectory verification. A run Attest could not "
        "vouch for is not a run to put on the front page."
    )


def test_the_approval_parses_and_settles_the_same_run() -> None:
    approval = ApprovalResponse.model_validate(_load("approval.json"))
    parked = AuditRecord.model_validate(_load("audit-parked.json"))
    assert approval.audit.run_id == parked.run_id, "two different runs were spliced together"
    assert approval.audit.status.value == "complete", approval.audit.status
    assert len(approval.writebacks) == len(parked.claims)
    assert all(w.ok for w in approval.writebacks), (
        f"a recorded catalog write FAILED: {[w for w in approval.writebacks if not w.ok]}. "
        "Re-capture against a healthy catalog rather than shipping a broken write as the demo."
    )


def test_the_recorded_decisions_parse_and_cover_every_parked_claim() -> None:
    """The set `replayClient.approve` holds every other decision set against.

    It has to cover every claim the run parked on, or the recorded outcome could not have
    settled the run — and the replay would refuse the only decision set that works.
    """
    parked = AuditRecord.model_validate(_load("audit-parked.json"))
    decisions = [DecisionRequest.model_validate(d) for d in _load("decisions.json")]
    assert {d.claim_index for d in decisions} == {c.index for c in parked.claims}
    corrected = [
        c.index
        for c in parked.claims
        if c.correction.outcome.value == "corrected" and c.correction.review.value == "pending"
    ]
    ruled = {d.claim_index for d in decisions if d.accept_correction is not None}
    assert set(corrected) <= ruled, (
        f"claims {sorted(set(corrected) - ruled)} had a proposed correction nobody ruled on. "
        "The run re-parks on an unruled correction, so the recorded outcome cannot settle it."
    )


def test_every_recorded_claims_read_parses_through_the_real_response_model() -> None:
    entries = _load("claims.json")
    assert entries, "no GET /claims responses were recorded"
    for entry in entries:
        assert set(entry) == {"filters", "response"}, entry.keys()
        ClaimsResponse.model_validate(entry["response"])


def test_the_unfiltered_read_is_recorded_because_the_explorer_opens_with_it() -> None:
    """`ClaimsExplorer` loads unfiltered on mount. Without this capture it opens refusing."""
    entries = _load("claims.json")
    assert any(e["filters"] == {} for e in entries), (
        "the catalog-wide read is not in the recording, so the claims explorer would refuse "
        "on its very first render"
    )


def test_the_claim_urns_are_consistent_across_every_fixture() -> None:
    """One run, four views of it. A URN in one and not the others means two recordings."""
    approval = ApprovalResponse.model_validate(_load("approval.json"))
    written = {w.claim_urn for w in approval.writebacks}
    assert written and all(written), "a write-back carries no claim URN"

    catalog_wide = next(e for e in _load("claims.json") if e["filters"] == {})
    retrieved = {c["claim_urn"] for c in catalog_wide["response"]["claims"]}
    assert written == retrieved, (
        "the claims the approval says it wrote are not the claims the catalog read returned:\n"
        f"  only in the write-back: {sorted(written - retrieved)}\n"
        f"  only in the catalog read: {sorted(retrieved - written)}"
    )

    # And every scoped read must be a SUBSET of the catalog-wide one, or a filtered view is
    # showing a claim the unfiltered view does not — two recordings again, one filter down.
    for entry in _load("claims.json"):
        urns = {c["claim_urn"] for c in entry["response"]["claims"]}
        assert urns <= retrieved, f"filter {entry['filters']} returned claims from elsewhere"


def test_the_recorded_claims_are_about_the_datasets_the_audit_claimed_about() -> None:
    parked = AuditRecord.model_validate(_load("audit-parked.json"))
    audited = {c.target_urn for c in parked.claims}
    catalog_wide = next(e for e in _load("claims.json") if e["filters"] == {})
    published = {c["target_urn"] for c in catalog_wide["response"]["claims"]}
    assert published == audited, (
        f"the recording's catalog read covers {published}, but the audit was about {audited}"
    )


def test_the_manifest_hashes_match_the_files_on_disk(manifest: dict) -> None:
    """THE ANTI-EDIT PIN. Everything above checks SHAPE; only this checks the BYTES.

    A hand-edited fixture parses perfectly — that is what makes it dangerous. Flip a verdict,
    round a cost down, fill in a `null`, and every structural test above stays green while the
    most public page in the project shows something no run produced.
    """
    assert set(manifest["files"]) == set(HASHED), manifest["files"].keys()
    for name, expected in manifest["files"].items():
        actual = hashlib.sha256((REPLAY / name).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{name} does not match its manifest hash. A replay fixture is a RECORDING; if it "
            "needs to change, re-capture it with `just replay-capture` rather than editing it."
        )


@pytest.mark.parametrize("name", (*HASHED, "manifest.json"))
def test_a_fixture_contains_no_carriage_return(name: str) -> None:
    """THE PIN ABOVE IS ONLY A PIN IF THE BYTES ARE THE SAME EVERYWHERE. They were not.

    The capture wrote these through Python's text mode on Windows, which turns every `\\n` into
    `\\r\\n`, and the manifest recorded the hashes of those CRLF bytes. Git, with
    `core.autocrlf=true`, normalised them to LF on the way into the index. So the manifest
    described bytes that existed on exactly ONE machine: the hash test passed for the author
    and failed in CI on the first push, measuring an LF checkout against a CRLF digest.

    That is this repo's recurring failure in a new place — a green tick about a different
    program — and it landed in the test written to stop the fixtures being tampered with. The
    weakening fix would be to compare hashes with line endings normalised away, which would let
    a whitespace edit through the one check that exists to catch edits. So the bytes are made
    canonical instead, at all three points that can move them: the capture writes LF,
    `.gitattributes` checks out LF, and this fails on a CR that arrives by any other route.
    """
    assert b"\r" not in (REPLAY / name).read_bytes(), (
        f"{name} contains a carriage return, so its bytes — and therefore its manifest hash — "
        "depend on the platform that wrote or checked it out. Re-capture with `just "
        "replay-capture`, which writes LF; do not normalise the hash comparison instead."
    )


def test_the_hash_check_catches_a_hand_edit(tmp_path: Path, manifest: dict) -> None:
    """The vacuity check for the pin above — run RED before trusting its green.

    A hash test that only ever passes is a green light wired to nothing. This performs the
    edit that matters (one character of a recorded response) and asserts the hash moves.
    """
    original = (REPLAY / "approval.json").read_bytes()
    recorded = manifest["files"]["approval.json"]
    assert hashlib.sha256(original).hexdigest() == recorded  # non-vacuous: the real one matches

    edited = original.replace(b'"ok": true', b'"ok": false', 1)
    assert edited != original, "the edit did not apply, so this proves nothing"
    assert hashlib.sha256(edited).hexdigest() != recorded, (
        "a hand-edited fixture produced the manifest's hash — the pin cannot detect an edit"
    )

    # And the edit is one a shape check would happily wave through, which is the point.
    ApprovalResponse.model_validate(json.loads(edited.decode("utf-8")))


def test_the_manifest_records_what_this_was_captured_against(manifest: dict) -> None:
    """The banner dates the recording from these, so they may not be blank or invented."""
    parked = AuditRecord.model_validate(_load("audit-parked.json"))
    assert manifest["run_id"] == parked.run_id, "the manifest names a different run"
    assert manifest["datahub_core_version"].startswith("v"), manifest["datahub_core_version"]
    assert manifest["model"], "the manifest does not say which model produced the explanations"
    assert len(manifest["capturing_commit"]) == 40, manifest["capturing_commit"]
    assert manifest["captured_at"]


def test_a_fixture_that_stopped_matching_the_wire_types_fails_loudly() -> None:
    """The vacuity check for the model-parsing tests.

    They are the drift alarm for a wire-type change, so they have to be able to ring. If
    `ClaimsResponse` ever stopped rejecting a malformed body, every parse test above would be
    a green light wired to nothing.
    """
    catalog_wide = next(e for e in _load("claims.json") if e["filters"] == {})
    broken = json.loads(json.dumps(catalog_wide["response"]))
    del broken["retrieval"]["entry_point"]
    with pytest.raises(ValidationError):
        ClaimsResponse.model_validate(broken)
