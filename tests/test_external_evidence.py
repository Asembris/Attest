"""Offline pins for the external-catalog EVIDENCE tooling.

These guard three things that no other test can, and each of them is a way the after-trial
could quietly stop being evidence:

  1. **A receipt cannot be overwritten.** `spikes/external_trial.py` used to write the
     committed historical receipt from a module constant, unconditionally. Nothing in the
     tree pinned that file, so the only protection was memory.
  2. **Exactly ONE expectation moved, and the freeze is proven against the COMMITTED
     BASELINE RECEIPT** rather than against a hand-copied table in this file. A hand-copy
     would be a second place to keep the truth, and it would be updated by whoever was
     editing the cases -- which is precisely the person the freeze exists to constrain.
  3. **The census's legacy arm is really a different query.** Its whole method is comparing
     the shipped `DATASET_QUERY` against a copy with one line deleted; if that deletion ever
     silently no-ops, both arms become identical and the census reports a clean "no
     difference" that means nothing at all.

Truly offline: no DataHub, no model, no network. Nothing here imports `src/attest` for
anything but pure parsing helpers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SPIKES = REPO / "spikes"
if str(SPIKES) not in sys.path:
    sys.path.insert(0, str(SPIKES))

import evidence  # noqa: E402
import external_census  # noqa: E402
import external_trial  # noqa: E402

BASELINE = REPO / "docs" / "external-trial" / "results.json"


@pytest.fixture(scope="module")
def baseline() -> dict:
    """The committed 2026-08-04 receipt: the historical BEFORE artifact."""
    return json.loads(BASELINE.read_text(encoding="utf-8"))


# --- 1. a receipt is never overwritten ---------------------------------------


def test_write_receipt_refuses_a_path_that_already_exists(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    evidence.write_receipt(path, {"n": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}

    with pytest.raises(evidence.ReceiptExists) as exc:
        evidence.write_receipt(path, {"n": 2})
    # The refusal must say what to do, because the likeliest way to hit it is a command
    # whose DEFAULT path is the historical artifact.
    assert "--receipt" in str(exc.value)

    # And the first receipt is still the first receipt.
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 1}


def test_there_is_no_way_to_force_an_overwrite() -> None:
    """No --force, and no keyword that could grow into one.

    A forcing flag is the whole guarantee gone: with one, the accident this prevents is
    always one argument away, and a rerun that lands on a nicer number can hide in place.
    """
    import inspect

    params = set(inspect.signature(evidence.write_receipt).parameters)
    assert params == {"path", "payload"}


def test_the_baseline_receipt_is_intact(baseline: dict) -> None:
    """The historical artifact still says what it said. A pin, not a formality.

    If this goes red, the "before" half of the before/after pair has been edited and every
    comparison drawn from it is worthless.
    """
    assert len(baseline["claims"]) == 15
    assert baseline["totals"]["matched_expectation"] == 15
    assert baseline["totals"]["answered_the_intended_question"] == 14
    assert baseline["totals"]["right_by_luck_ids"] == ["ext-class-01"]
    by_id = {c["id"]: c for c in baseline["claims"]}
    assert by_id["ext-own-04"]["expected"] == "ClaimError"
    assert by_id["ext-own-04"]["verdict"] == "ClaimError"


def test_the_trial_default_receipt_path_is_the_baseline_so_a_careless_run_fails() -> None:
    """The default is the file that must not be written, which is what makes it safe.

    Pointing the default at a fresh name would let a bare `just external-trial` succeed and
    quietly mint an unnamed receipt; pointing it here means the guard fires immediately.
    """
    assert external_trial.RECEIPT == BASELINE
    assert BASELINE.exists()


# --- 2. exactly one expectation moved ----------------------------------------


def test_exactly_one_case_has_a_predeclared_expectation_change() -> None:
    moved = [c for c in external_trial.CLAIMS if c.expected_before]
    assert [c.id for c in moved] == ["ext-own-04"]

    (case,) = moved
    assert case.expected_before == "ClaimError"
    assert case.expected == "Supported"
    # The reason must name the commit under test and the URN whose readability is the
    # whole hypothesis -- a bare "the fix landed" is not a predeclaration.
    assert "2d7eaf9" in case.expectation_moved_because
    assert "CorpGroup" in case.expectation_moved_because
    assert "corpGroup" in case.expectation_moved_because


def test_no_other_case_moved_at_all_measured_against_the_committed_baseline(
    baseline: dict,
) -> None:
    """The freeze, proven against the artifact rather than against a copy in this file.

    Every field the baseline receipt records for a case is compared back to the case as it
    stands today. Change a prose string, a target URN, an intended-question family, a
    `human_reading` or an unmoved `expected`, and this names the case and the field.
    """
    by_id = {c["id"]: c for c in baseline["claims"]}
    assert {c.id for c in external_trial.CLAIMS} == set(by_id)

    drift: list[str] = []
    for case in external_trial.CLAIMS:
        before = by_id[case.id]
        for attr in ("family", "prose", "target_urn", "human_reading", "basis", "probes"):
            if getattr(case, attr) != before[attr]:
                drift.append(f"{case.id}.{attr}")
        # `expected` may differ ONLY where the move was predeclared, and then it must
        # differ from exactly the value the baseline recorded.
        if case.expected_before:
            if case.expected_before != before["expected"]:
                drift.append(f"{case.id}.expected_before != the baseline's expected")
        elif case.expected != before["expected"]:
            drift.append(f"{case.id}.expected")
    assert drift == []


def test_the_publish_set_is_frozen() -> None:
    """Not recoverable from the baseline receipt, so it is pinned by hand, once.

    Publishing writes to someone's catalog. Silently nominating a fourth claim would change
    what the run DOES, not just what it reports.
    """
    assert {c.id for c in external_trial.CLAIMS if c.publish} == {
        "ext-fresh-01",
        "ext-fresh-02",
        "ext-class-02",
    }


def test_the_hypothesis_block_is_derived_from_the_cases_not_restated() -> None:
    """A hand-written hypothesis list would be a second copy of the truth, free to drift."""
    changes = external_trial.HYPOTHESIS["predeclared_changes"]
    assert [c["id"] for c in changes] == ["ext-own-04"]
    assert changes[0]["before"] == "ClaimError"
    assert changes[0]["after"] == "Supported"
    # 15 -> 0 must be carried as a HYPOTHESIS, never as a threshold the run has to clear.
    assert "NOT AS AN ACCEPTANCE REQUIREMENT" in external_trial.HYPOTHESIS[
        "census_hypothesis"
    ]


def test_the_trial_receipt_no_longer_reports_an_unmeasured_catalog_census() -> None:
    """The two literals are gone, and gone as None rather than as 0.

    They were hardcoded in `build_receipt`, so an after-run would have emitted "15 refused"
    whatever the fix did -- a fabricated figure inside the artifact measuring the fix.
    None-is-not-zero is the same rule `Trace.cost` follows for an unpriced model.
    """
    source = (SPIKES / "external_trial.py").read_text(encoding="utf-8")
    assert '"datasets_readable_by_attest": None' in source
    assert '"datasets_refused": None' in source
    assert '"datasets_refused": 15' not in source


# --- 3. the census's two arms are really two arms -----------------------------


def test_the_legacy_query_is_the_shipped_one_minus_exactly_the_group_arm() -> None:
    from attest.datahub.client import DataHubClient

    shipped = DataHubClient.DATASET_QUERY
    legacy = external_census.legacy_query(shipped)

    assert legacy != shipped, "the strip did nothing; both arms would be identical"
    assert external_census.CORPGROUP_ARM in shipped
    assert external_census.CORPGROUP_ARM not in legacy
    # The CorpUser arm MUST survive, or the comparison stops isolating the group arm and
    # starts measuring a query broken in a second way.
    assert external_census.CORPUSER_ARM in legacy
    # Exactly one line removed, nothing else touched.
    assert len(legacy.splitlines()) == len(shipped.splitlines()) - 1


def test_a_reshaped_query_fails_loudly_instead_of_producing_two_identical_arms() -> None:
    """The vacuity check. A silent no-op here is a census that always says "no difference".

    Rename or reformat the arm upstream and a naive `.replace()` returns the string
    unchanged; the census would then compare a query with itself and report a clean result
    meaning nothing. That must be a crash, not a number.
    """
    with pytest.raises(external_census.LegacyQueryUndeducible):
        external_census.legacy_query("query dataset { ownership { owners { owner { urn } } } }")


# --- the census's classification mirrors what a checker actually sees ---------

_URN = "urn:li:dataset:(urn:li:dataPlatform:snowflake,b2fd91.x.y,PROD)"


def test_a_fabricated_entity_is_not_found_not_readable() -> None:
    """DataHub answers `dataset(urn:)` for ANY well-formed URN; only `exists` tells them
    apart. A census that trusted a non-null response would count every typo as readable."""
    assert external_census.classify_dataset_payload(None)[0] == "not-found"
    assert external_census.classify_dataset_payload({"urn": _URN})[0] == "not-found"
    assert (
        external_census.classify_dataset_payload({"urn": _URN, "exists": False})[0]
        == "not-found"
    )


def test_a_well_formed_response_reads_ok() -> None:
    assert external_census.classify_dataset_payload({"urn": _URN, "exists": True}) == (
        "ok",
        "",
    )


def test_a_group_owned_response_without_the_arm_is_a_malformed_response() -> None:
    """The exact shape the pre-fix query produced: an owner entry whose `owner` is `{}`.

    This is the census's whole subject, so it is pinned rather than assumed -- and it pins
    that the classification is `MalformedResponseError`, which is what made every claim
    about those datasets a `ClaimError`.
    """
    outcome, detail = external_census.classify_dataset_payload(
        {
            "urn": _URN,
            "exists": True,
            "ownership": {
                "owners": [
                    {
                        "owner": {},
                        "ownershipType": {
                            "urn": "urn:li:ownershipType:__system__technical_owner"
                        },
                    }
                ]
            },
        }
    )
    assert outcome == "MalformedResponseError"
    assert "ValueError" in detail

    # And the boundary that must NOT move with it: an aspect present and listing nobody is
    # a VALID state, not a malformed one (Session 23, Hole 2).
    assert external_census.classify_dataset_payload(
        {"urn": _URN, "exists": True, "ownership": {"owners": []}}
    ) == ("ok", "")


# --- the census's arithmetic --------------------------------------------------


def _row(urn: str, with_arm: str, without_arm: str) -> dict[str, str]:
    return {
        "urn": urn,
        "with_corpgroup_arm": with_arm,
        "with_corpgroup_detail": "",
        "without_corpgroup_arm": without_arm,
        "without_corpgroup_detail": "",
    }


def test_the_census_separates_fixed_from_still_refused_from_regressed() -> None:
    """Three different facts, computed rather than narrated.

    "Still refused" is the one that matters most: it is the hypothesis failing, and it must
    surface as a classified finding rather than be absorbed into a headline count.
    """
    result = external_census.summarize_rows(
        [
            _row("a", "ok", "ok"),  # never affected
            _row("b", "ok", "MalformedResponseError"),  # the fix
            _row("c", "MalformedResponseError", "MalformedResponseError"),  # survives
            _row("d", "not-found", "ok"),  # a regression, if it ever happened
        ]
    )
    assert result["datasets_enumerated"] == 4
    assert result["with_corpgroup_arm"]["readable"] == 2
    assert result["without_corpgroup_arm"]["readable"] == 2
    assert result["refusals_the_corpgroup_arm_fixed_urns"] == ["b"]
    assert result["still_refused_with_the_arm_urns"] == ["c", "d"]
    assert result["regressed_by_the_arm_urns"] == ["d"]
    assert result["with_corpgroup_arm"]["refusals_by_reason"] == {
        "MalformedResponseError": 1,
        "not-found": 1,
    }


def test_the_hypothesis_holding_looks_like_this() -> None:
    """The shape the receipt takes if 15 -> 0. Recorded so the reading is unambiguous."""
    result = external_census.summarize_rows(
        [_row(str(i), "ok", "MalformedResponseError") for i in range(15)]
        + [_row("ok-" + str(i), "ok", "ok") for i in range(52)]
    )
    assert result["without_corpgroup_arm"]["refused"] == 15
    assert result["with_corpgroup_arm"]["refused"] == 0
    assert result["still_refused_with_the_arm"] == 0
    assert result["regressed_by_the_arm"] == 0


# --- provenance ---------------------------------------------------------------


def test_provenance_records_the_commit_the_tree_state_and_the_baseline_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The version lookup is the ONE thing in this module that would touch a socket, and the
    # offline tier's whole claim is that it never does. Stubbed rather than pointed at a
    # dead port: an unreachable host is still a connection ATTEMPT, and in CI it is a
    # timeout that reads as a slow test rather than as the rule being broken.
    monkeypatch.setattr(evidence, "datahub_version", lambda _url: "v1.5.0.6")
    block = evidence.provenance(
        fix_under_test="x", gms_url="http://localhost:8080", baseline_receipt=BASELINE
    )
    assert block["datahub_core_version"] == "v1.5.0.6"
    assert set(block) >= {
        "trial_commit",
        "trial_tree_dirty",
        "branch",
        "fix_under_test",
        "datahub_core_version",
        "python",
        "generated_at",
        "baseline_receipt",
        "baseline_receipt_sha256",
    }
    assert isinstance(block["trial_tree_dirty"], bool)
    # By CONTENT, so a later edit to the baseline is detectable from the after-receipt alone.
    assert block["baseline_receipt_sha256"] == evidence.sha256_file(BASELINE)
    assert block["baseline_receipt"] == "docs/external-trial/results.json"


def test_a_missing_file_hashes_to_None_not_to_the_hash_of_nothing(tmp_path: Path) -> None:
    assert evidence.sha256_file(tmp_path / "nope.json") is None
