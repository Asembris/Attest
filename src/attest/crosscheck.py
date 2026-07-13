"""Cross-check: what the model thinks the evidence says, against what the code decided.

The deterministic verdict always stands. That is not negotiable and it is not what this
module is for. It exists because a *disagreement* is information, and throwing it away
would be the second-worst thing Attest could do with it — the worst being to let the
model win.

When the explanation step writes its prose, it also reports which verdict it believes the
evidence supports and which fields it relied on. Neither is used to decide anything. Both
are compared against the checker's result, and any divergence is surfaced as a Conflict:

  - **verdict-disagreement** — the model read the same evidence and reached a different
    verdict. Sometimes that means the model is wrong (usually). Sometimes it means the
    evidence a checker returned does not actually justify the verdict it returned, which
    is a bug in the checker, and the only way anyone would ever find out is if someone
    were watching for exactly this.
  - **uncited-field** — the model justified itself with a catalog field the checker never
    read. Whatever produced that sentence, it was not the evidence.

Either way the explanation is rejected and the deterministic template is used, so a
conflict can never reach a user disguised as an explanation. It reaches them as a
conflict.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from attest.claims import CheckResult, Verdict

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Conflict:
    """A disagreement between the model and the deterministic core. Never resolved here."""

    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


def crosscheck(
    result: CheckResult,
    implied_verdict: str | None,
    cited_fields: tuple[str, ...] = (),
) -> tuple[Conflict, ...]:
    """Compare the model's reading of the evidence with the checker's. Report, never act."""
    conflicts: list[Conflict] = []

    if implied_verdict and implied_verdict != result.verdict.value:
        # Note the asymmetry: we do not care *which* verdict the model preferred. The
        # deterministic one is the verdict. We care that it looked at code-selected
        # evidence and read it differently, which is worth a human's attention.
        conflicts.append(
            Conflict(
                kind="verdict-disagreement",
                detail=(
                    f"the checker returned {result.verdict.value} from the evidence; the "
                    f"model read the same evidence as {implied_verdict}"
                ),
            )
        )

    known = {e.field for e in result.evidence}
    for field in cited_fields:
        if field not in known:
            conflicts.append(
                Conflict(
                    kind="uncited-field",
                    detail=(
                        f"the model cited {field!r}, which is not among the fields the "
                        f"checker read ({', '.join(sorted(known))})"
                    ),
                )
            )

    if conflicts:
        log.warning(
            "model/checker conflict on %s: %s",
            result.target_urn,
            "; ".join(str(c) for c in conflicts),
        )

    return tuple(conflicts)


def implied(value: str | None) -> Verdict | None:
    """Parse the model's claimed verdict, tolerantly. An unparseable one is no signal."""
    if not value:
        return None
    try:
        return Verdict(value)
    except ValueError:
        return None
