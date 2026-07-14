"""Self-correction: hand a contradicted claim back to the agent, with the facts.

When the catalog contradicts a claim, the useful thing to do is not merely to score it.
It is to tell the agent what the catalog actually says and let it restate itself. That is
the difference between a scorer and a system, and it is the only step in Attest where a
model is asked to produce a *claim* rather than to transcribe or to phrase one.

Which makes this the most dangerous module in the project, so it is the most constrained.

**A revision may not change the subject.** The revised claim must carry the SAME
`target_urn` and the SAME `claim_type` as the claim it revises. This is strictly stronger
than decompose.py's quote-the-URN rule, and it is the invariant that makes the loop an
audit rather than a negotiation: an agent told "dana.wu does not own support_tickets"
could otherwise "correct" itself by talking about a different table, or by downgrading a
falsified ownership claim into a vacuous freshness one. Both would re-verify green. Both
are the agent wriggling out from under the finding. Neither is a correction, and both are
rejected here by comparison, not by prompting — the model is *told* the rule and then
*checked* against it, because a rule a model is merely asked to follow is not a rule.

**The revision is not believed.** What comes back is re-run through the same deterministic
checker, against the same snapshot the original verdict came from. So the outcome of a
correction is decided by code, exactly as the original verdict was, and the model's
contribution is a *proposal* that code then accepts or rejects. This is what keeps the
loop demo-safe: whatever the model says, the outcome is one of a fixed set, and "the
revision was still wrong" is a reported result rather than a failure.

The same snapshot, deliberately — not a re-fetch. The agent is being asked to reconcile
itself with the facts it was *shown*. Re-fetching would introduce a race in which the
catalog changed underneath the loop, and a correction could pass or fail for reasons
nobody in the transcript ever saw.

**And it is never auto-published.** A revision that re-verifies as Supported becomes a
*proposal*, parked at a human checkpoint (see graph.py). Attest does not settle a
correction on its own — see the accountability note there.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter, ValidationError

from attest.claims import CheckResult, Claim
from attest.config import Step
from attest.llm import LLM, LLMError

log = logging.getLogger(__name__)

_CLAIM_ADAPTER: TypeAdapter[Claim] = TypeAdapter(Claim)

SYSTEM = """\
You are the data agent whose claim was just audited against the catalog and found to be
CONTRADICTED. You are being shown what the catalog actually says. Restate your claim so
that it is consistent with the catalog.

Rules, all of them enforced by code after you reply:
- Keep the SAME target_urn. You are correcting what you said about this one dataset. You
  may not switch to a different dataset.
- Keep the SAME claim_type. A contradicted ownership claim is corrected by an ownership
  claim, not by abandoning it for a claim about something else.
- Use ONLY the catalog facts in the evidence below. Do not invent a fact to replace the
  one that was wrong, and do not hedge into vagueness — state what the catalog says.
- FILL IN THE FIELD YOUR CLAIM TYPE OWNS. It carries the correction, and a revision that
  leaves it null is not a revision — it will be thrown away. An ownership claim must set
  owner_urn; a freshness claim must set max_age_hours; a classification claim must set
  labels (and `present`); a schema claim must set columns.
- raw_text is your corrected sentence, written the way you would have written it had you
  known. It must mention the target_urn.
- Leave every field that does not apply to this claim's type null.

If the evidence does not tell you what the correct value is, say so by repeating your
original claim unchanged. A revision that guesses is worse than no revision: it will be
re-verified against the same catalog and it will fail again.\
"""

# One claim, not a list — a revision corrects one claim. The flat shape mirrors
# decompose.SCHEMA for the same reason it does there: OpenAI's strict mode requires every
# property in `required`, so optionality is a nullable type and the union is rebuilt by
# pydantic, which is where claim validation belongs.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "claim_type",
        "target_urn",
        "raw_text",
        "max_age_hours",
        "owner_urn",
        "labels",
        "present",
        "field_path",
        "columns",
        "unchanged",
    ],
    "properties": {
        "claim_type": {
            "type": "string",
            "enum": ["freshness", "ownership", "classification", "schema"],
        },
        "target_urn": {"type": "string", "description": "The SAME dataset URN. Copy it."},
        "raw_text": {"type": "string", "description": "Your corrected sentence."},
        # Every field below carries its description for the same reason decompose.SCHEMA's
        # do, and the reason is not decoration: the first live run of the correction loop
        # returned owner_urn=null on an ownership revision and the claim was rejected as
        # malformed. The model was not being evasive — it had been handed a bare
        # `["string", "null"]` and told nothing about what belonged in it. A schema that
        # does not say what a field is for is a prompt bug, and it surfaced as a failed
        # correction. THE FIELD YOUR CLAIM TYPE OWNS IS THE ONE YOU ARE CORRECTING: say so.
        "max_age_hours": {
            "type": ["number", "null"],
            "description": (
                "freshness only, and REQUIRED for a freshness revision. The corrected "
                "maximum age in hours: 'daily' is 24, 'this week' is 168."
            ),
        },
        "owner_urn": {
            "type": ["string", "null"],
            "description": (
                "ownership only, and REQUIRED for an ownership revision. The CORRECT owner, "
                "copied from the evidence: urn:li:corpuser:... or urn:li:corpGroup:... . If "
                "the catalog lists several owners, name one of them."
            ),
        },
        "labels": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": (
                "classification only, and REQUIRED for a classification revision. Tag or "
                "glossaryTerm URNs, e.g. urn:li:tag:PII."
            ),
        },
        "present": {
            "type": ["boolean", "null"],
            "description": (
                "classification only. false means you are now claiming the label is ABSENT "
                "('PII-free'); true means present. Flipping this is often the whole "
                "correction."
            ),
        },
        "field_path": {
            "type": ["string", "null"],
            "description": "classification only. The column, if the claim is about one.",
        },
        "columns": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "native_type"],
                "properties": {
                    "name": {"type": "string"},
                    "native_type": {"type": ["string", "null"]},
                },
            },
            "description": (
                "schema only, and REQUIRED for a schema revision. The corrected columns, "
                "with the types the evidence gives."
            ),
        },
        "unchanged": {
            "type": "boolean",
            "description": (
                "True if the evidence does not tell you what the correct value is, so you "
                "are standing by your original claim rather than guessing."
            ),
        },
    },
}

# Which fields each claim type owns. Same rationale as decompose._FIELDS_BY_TYPE: the flat
# schema forces every field onto every claim, and a model that volunteers an irrelevant one
# would otherwise fail the strict claim models ("extra inputs are not permitted") and lose
# an otherwise perfect revision to a transcription artefact.
_FIELDS_BY_TYPE: dict[str, frozenset[str]] = {
    "freshness": frozenset({"max_age_hours"}),
    "ownership": frozenset({"owner_urn"}),
    "classification": frozenset({"labels", "present", "field_path"}),
    "schema": frozenset({"columns"}),
}
_COMMON = frozenset({"claim_type", "target_urn", "raw_text"})


@dataclass(frozen=True)
class Revision:
    """What the agent said when shown the catalog. Not yet believed, not yet verified.

    `claim` is None when nothing usable came back — the model changed the subject, stood
    by its original claim, or produced something the claim schema rejects. Each is a
    distinct, reported outcome; none of them is silently retried into existence.
    """

    claim: Claim | None
    rejected: str | None = None
    # The agent declined to revise because the evidence does not say what the truth is.
    # An honest non-answer, and NOT the same as a failed revision.
    stood_firm: bool = False

    @property
    def ok(self) -> bool:
        return self.claim is not None


def _prompt(result: CheckResult) -> str:
    """The agent's entire world: its own claim, the verdict, and the evidence."""
    claim = result.claim
    lines = [
        f"YOUR ORIGINAL CLAIM: {claim.raw_text}",
        f"CLAIM TYPE (keep this): {claim.claim_type.value}",
        f"TARGET (keep this): {claim.target_urn}",
        "",
        f"THE AUDIT'S VERDICT: {result.verdict.value}",
        f"WHY: {result.reason}",
        "",
        "WHAT THE CATALOG ACTUALLY SAYS — the only facts you may use:",
    ]
    for e in result.evidence:
        held = "nothing (the catalog is silent here)" if e.value is None else repr(e.value)
        lines.append(f"- field: {e.field}")
        lines.append(f"  the catalog holds: {held}")
        if e.note:
            lines.append(f"  note: {e.note}")
    return "\n".join(lines)


def _validate(raw: dict[str, Any], original: Claim) -> Revision:
    """Hold the revision to the two invariants, then to the claim schema."""
    if raw.get("unchanged"):
        return Revision(
            claim=None,
            stood_firm=True,
            rejected="the agent stood by its original claim: the evidence does not say "
            "what the correct value is",
        )

    urn = (raw.get("target_urn") or "").strip()
    if urn != original.target_urn:
        # Changing the subject is the one way a self-correction loop can be gamed into
        # laundering a false claim, so it is refused rather than re-prompted.
        return Revision(
            claim=None,
            rejected=(
                f"retargeted the claim: revised {urn!r}, but the claim under audit is "
                f"about {original.target_urn!r}. A revision may correct what is said "
                f"about an entity, never which entity is discussed."
            ),
        )

    claim_type = raw.get("claim_type")
    if claim_type != original.claim_type.value:
        return Revision(
            claim=None,
            rejected=(
                f"changed the claim type: revised as {claim_type!r}, but the claim under "
                f"audit is a {original.claim_type.value!r} claim. Abandoning a "
                f"contradicted claim is not correcting it."
            ),
        )

    allowed = _COMMON | _FIELDS_BY_TYPE.get(claim_type or "", frozenset())
    fields = {k: v for k, v in raw.items() if v is not None and k in allowed}

    try:
        revised = _CLAIM_ADAPTER.validate_python(fields)
    except ValidationError as exc:
        return Revision(
            claim=None,
            rejected=f"invalid revised claim: {exc.errors()[0].get('msg', 'validation failed')}",
        )

    if revised == original:
        # Byte-identical to what was already contradicted. Re-verifying it would burn a
        # retry to reach the verdict we already have.
        return Revision(
            claim=None,
            stood_firm=True,
            rejected="the revision is identical to the contradicted claim",
        )

    return Revision(claim=revised)


def revise(result: CheckResult, llm: LLM | None = None) -> Revision:
    """Ask the agent to restate a contradicted claim, given the catalog's facts.

    Returns a *proposal*. It has not been verified — graph.py re-runs the deterministic
    checker on it, and only code decides whether the correction stands.
    """
    llm = llm or LLM()

    try:
        payload = llm.json(
            step=Step.ENTAILMENT,
            system=SYSTEM,
            user=_prompt(result),
            schema=SCHEMA,
            schema_name="revised_claim",
        )
    except LLMError as exc:
        log.error("revision failed for %s: %s", result.target_urn, exc)
        return Revision(claim=None, rejected=f"revision-failed: {exc}")

    revision = _validate(payload, result.claim)
    if revision.rejected:
        log.warning("rejected a revision of %s — %s", result.target_urn, revision.rejected)
    return revision
