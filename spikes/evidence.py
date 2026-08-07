"""Provenance and receipt-writing, shared by the external-catalog evidence scripts.

There is ONE implementation of the overwrite refusal and ONE shape for the provenance
block, and both facts are deliberate.

**The overwrite refusal.** `spikes/external_trial.py` used to write
`docs/external-trial/results.json` unconditionally from a module constant, so `just
external-trial` silently clobbered the committed historical receipt. Nothing in the tree
pins that file, so the only thing protecting it was whoever remembered. A receipt is
evidence; evidence that a command can overwrite by accident is evidence held on trust.
There is no `--force`: a second run must NAME a second file, which then shows up in `git
status` and in the diff. That is also what stops a run being repeated until the numbers
read better — a rerun cannot hide, because it lands somewhere new.

**One provenance shape.** Two receipts describing one catalog state are only comparable if
they agree on what they are describing. Two copies of this block would drift, and the drift
would be invisible until someone tried to line the receipts up. `tests/test_external_evidence.py`
pins this module, once.

Nothing here reads or writes the catalog and nothing here is imported by `src/attest/**`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent


class ReceiptExists(RuntimeError):
    """A receipt already exists at that path. Refused, with no way to force it."""


def sha256_file(path: Path) -> str | None:
    """The hash of a file, or None if it is not there. None is not a hash of nothing."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_receipt(path: Path, payload: dict[str, Any]) -> Path:
    """Write a receipt, REFUSING to overwrite one that is already there.

    The refusal names the file and says what to do, because the most likely way to hit it
    is running a command whose default path is the historical artifact -- which is exactly
    the accident this exists to stop.
    """
    if path.exists():
        raise ReceiptExists(
            f"{path} already exists and this will not overwrite a receipt.\n"
            f"A receipt is evidence: overwriting one destroys the artifact a committed "
            f"number traces to, and there is deliberately no --force.\n"
            f"Pass --receipt with a NEW path (a second run must land somewhere new, so "
            f"that it cannot hide in the diff)."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_state() -> dict[str, Any]:
    """The commit this evidence was produced at, and whether the tree was clean.

    `tree_dirty` is REPORTED, never hidden -- the benchmark's `scorer_tree_dirty`
    precedent (CLAUDE.md §25). A receipt produced from an edited tree is still a receipt;
    it just is not the receipt of a commit anyone can check out.
    """
    return {
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "tree_dirty": bool(_git("status", "--porcelain")),
    }


def datahub_version(gms_url: str) -> str:
    """The running DataHub Core version, off `/config`. Unknown is said, never guessed."""
    try:
        import httpx

        payload = httpx.get(f"{gms_url.rstrip('/')}/config", timeout=10).json()
        return (payload.get("versions") or {}).get("acryldata/datahub", {}).get(
            "version", "unknown"
        )
    except Exception as exc:  # noqa: BLE001 - provenance must not take the run down
        return f"unknown ({type(exc).__name__})"


def provenance(
    *,
    fix_under_test: str,
    gms_url: str,
    baseline_receipt: Path | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The block every receipt in this family carries.

    `baseline_receipt_sha256` is the point of the baseline fields: the after-artifact
    records WHICH before-artifact it is contrasted with, by content. Edit the baseline
    later and the mismatch is detectable from the after-receipt alone, without anyone
    having to have remembered to check.
    """
    state = git_state()
    block: dict[str, Any] = {
        "trial_commit": state["commit"],
        "trial_tree_dirty": state["tree_dirty"],
        "branch": state["branch"],
        "fix_under_test": fix_under_test,
        "datahub_core_version": datahub_version(gms_url),
        "datahub_gms_url": gms_url,
        "python": sys.version.split()[0],
        "generated_at": datetime.now(tz=UTC).isoformat(),
    }
    if baseline_receipt is not None:
        block["baseline_receipt"] = str(
            baseline_receipt.relative_to(REPO)
            if baseline_receipt.is_relative_to(REPO)
            else baseline_receipt
        ).replace("\\", "/")
        block["baseline_receipt_sha256"] = sha256_file(baseline_receipt)
    if extra:
        block.update(extra)
    return block
