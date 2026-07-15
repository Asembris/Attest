"""Explanation generation: turn a verdict and its evidence into an audit-quality writeup.

The second — and last — job the semantic layer is allowed to do. The model gets to
*phrase* a verdict. It does not get to reach one, and it does not get to add to one.

**The deterministic reason is the authoritative explanation.** The template below is built
from the checker's own reason string and the evidence it returned; it is always correct,
always faithful, and it states the verdict's direction from code, never from the model. Model
prose is OPTIONAL presentation layered on top of it: a more fluent rephrasing that ships only
if it clears every guard, and is discarded for the template the moment it does not. That
inversion is deliberate (Session 13) — the reader's guarantee comes from the deterministic
floor, not from trusting the sentence the model wrote.

What the model sees is deliberately narrow: the claim, the verdict, and the evidence
fields the deterministic checker returned. Never the raw catalog, never the snapshot,
never the claim on its own. It cannot go looking for a better fact, because it is not
given one.

What comes back is not trusted, and three independent gates decide whether it is used at all:

  1. **crosscheck** — the model also reports which verdict it thinks the evidence shows.
     Disagreement never changes the verdict; it is surfaced as a Conflict.
  2. **faithfulness** — every factual token in the prose must appear in the evidence.
     A hallucinated owner, column, tag, date, or number fails here.
  3. **polarity** — the prose may assert only the DIRECTION the verdict reached. "the catalog
     supports the claim" beside a Contradicted verdict names no fabricated fact, so it passes
     faithfulness clean — and it is a fluent lie. The polarity guard (polarity.py) is what
     catches it: an explanation that affirms where the verdict denies, denies where it
     affirms, or claims silence over a definite verdict is rejected. This is the hole that
     Session 13 was called to close.
  4. **rejection is not retried forever** — one repair attempt, with the violations
     handed back, and then we fall back to the deterministic template.

**A failed explanation degrades to something true.** It never degrades to something plausible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from attest import faithfulness, polarity
from attest.claims import CheckResult
from attest.config import Step
from attest.crosscheck import Conflict, crosscheck
from attest.faithfulness import Faithfulness
from attest.llm import LLM, LLMError
from attest.polarity import PolarityCheck

log = logging.getLogger(__name__)

SYSTEM = """\
You write the explanation for a data-catalog audit verdict.

The verdict has already been decided by deterministic code. You are not reviewing it and
you cannot change it. Your job is to explain it clearly to an engineer, using only what
you are given.

Absolute rules:
- Use ONLY the facts in the evidence below. Every name, URN, column, tag, date, and
  number in your explanation must appear in the evidence verbatim.
- Do NOT compute new numbers. If the evidence says "10009.9h", do not write "417 days".
  Quote the figure you were given.
- Do NOT introduce catalog facts you were not given, however plausible they seem.
- Do NOT expand acronyms or coin new capitalised terms. Write "PII", never "Personally
  Identifiable Information". Any capitalised word you write must appear in the evidence.
- Match the VERDICT's direction and never the opposite. If the verdict is Contradicted, do
  not write that the catalog "supports", "confirms" or "agrees with" the claim; if it is
  Supported, do not write that the catalog "contradicts" or "disagrees with" it. Describe the
  catalog facts and, if you state the relationship at all, state only the one the verdict
  reached. The safest explanation states the facts and lets the verdict speak for itself.
- If the verdict is Insufficient-Coverage, say plainly that the catalog is silent — it is
  not evidence against the claim. Do not imply the agent was wrong.
- 2-4 sentences. Plain, precise, no hedging, no marketing.
- cited_fields: copy each `field:` path from the evidence EXACTLY as written, including
  any " + " in it. Do not split one path into two and do not add a "field:" prefix.

Also report which verdict YOU think the evidence supports, and which evidence fields you
used. These are cross-checked against the deterministic result; they do not decide
anything. Report them honestly even if you disagree — a disagreement is useful signal.\
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["explanation", "implied_verdict", "cited_fields"],
    "properties": {
        "explanation": {
            "type": "string",
            "description": "2-4 sentences explaining the verdict, using only the evidence.",
        },
        "implied_verdict": {
            "type": "string",
            "enum": ["Supported", "Contradicted", "Insufficient-Coverage"],
            "description": "Which verdict YOU read the evidence as supporting.",
        },
        "cited_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The evidence `field` paths you relied on, copied verbatim.",
        },
    },
}


@dataclass(frozen=True)
class Explanation:
    """A verdict, explained — and the record of what it took to trust the explanation."""

    text: str
    source: Literal["model", "template"]
    faithfulness: Faithfulness
    # Whether the shipped text asserts only the direction the verdict reached. The template
    # is polarity-safe by construction (it leads with the verdict word from code), so this is
    # ok on every fallback; on a model draft it is the gate that stops a fluent lie.
    polarity: PolarityCheck | None = None
    conflicts: tuple[Conflict, ...] = ()
    # Every model draft that was thrown away, and why. An auditor that quietly retries
    # until something passes is hiding its own failure rate.
    rejected: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_fallback(self) -> bool:
        return self.source == "template"


def template(result: CheckResult) -> str:
    """The deterministic explanation. Always correct, always faithful, never fluent.

    Built only from the checker's own reason and the evidence it returned, so it passes
    the faithfulness guard by construction — which is exactly why it is the fallback.

    Note the lowercase scaffolding ("based on the catalog fields..."). An earlier draft
    wrote a tidy "Evidence:" header and the guard rejected its own fallback, because
    "Evidence" is a capitalized word that appears nowhere in the evidence. The guard was
    right: the floor has to be built out of trusted words, and structural prose is the
    only kind of word this function is allowed to add.
    """
    lines = [
        f"{result.verdict.value}. {result.reason}",
        "",
        "based on the catalog fields the checker read:",
    ]
    for e in result.evidence:
        held = "nothing" if e.value is None else repr(e.value)
        note = f" {e.note}" if e.note else ""
        lines.append(f"- {e.field}: the catalog holds {held}.{note}")
    return "\n".join(lines)


def _prompt(result: CheckResult) -> str:
    """The model's entire world: the claim, the verdict, and the evidence. Nothing else."""
    claim = result.claim
    lines = [
        f"CLAIM (asserted by the agent): {claim.raw_text}",
        f"CLAIM TYPE: {claim.claim_type.value}",
        f"TARGET: {claim.target_urn}",
        "",
        f"VERDICT (decided by deterministic code, final): {result.verdict.value}",
        f"THE CODE'S OWN REASONING: {result.reason}",
        "",
        "EVIDENCE — the only catalog facts you may use:",
    ]
    for e in result.evidence:
        held = "nothing (the catalog is silent here)" if e.value is None else repr(e.value)
        lines.append(f"- field: {e.field}")
        lines.append(f"  the catalog holds: {held}")
        if e.note:
            lines.append(f"  note: {e.note}")
    return "\n".join(lines)


def explain(result: CheckResult, llm: LLM | None = None, max_attempts: int = 2) -> Explanation:
    """Explain a verdict. Falls back to the template rather than ship an unverified word."""
    llm = llm or LLM()
    prompt = _prompt(result)
    rejected: list[str] = []
    conflicts: tuple[Conflict, ...] = ()
    last: Faithfulness = Faithfulness(ok=False)
    last_polarity: PolarityCheck | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            payload = llm.json(
                step=Step.VERDICT,
                system=SYSTEM,
                user=prompt,
                schema=SCHEMA,
                schema_name="verdict_explanation",
            )
        except LLMError as exc:
            log.error("explanation generation failed: %s", exc)
            rejected.append(f"attempt {attempt}: {exc}")
            break

        text = (payload.get("explanation") or "").strip()
        conflicts = crosscheck(
            result,
            implied_verdict=payload.get("implied_verdict"),
            cited_fields=tuple(payload.get("cited_fields") or ()),
        )
        last = faithfulness.check(text, result)
        last_polarity = polarity.check(text, result)

        # Three gates, all of which must pass for the model's prose to ship. A conflicted
        # draft is arguing with a verdict it was told was final; an unfaithful one invented
        # a fact; a polarity-violating one asserts a DIRECTION the verdict never reached —
        # the fluent lie faithfulness cannot see. Any one failing sends us to the template.
        if last.ok and last_polarity.ok and not conflicts:
            return Explanation(
                text=text,
                source="model",
                faithfulness=last,
                polarity=last_polarity,
                conflicts=conflicts,
                rejected=tuple(rejected),
            )

        why = "; ".join(
            [
                *(str(c) for c in conflicts),
                *(str(v) for v in last.violations),
                *(str(v) for v in last_polarity.violations),
            ]
        )
        log.warning("rejected explanation draft (attempt %d): %s", attempt, why)
        rejected.append(f"attempt {attempt}: {why}")

        if attempt < max_attempts:
            prompt = (
                f"{_prompt(result)}\n\n"
                f"Your previous explanation was REJECTED: {why}\n"
                f"Every name, number, and identifier you write must appear in the evidence "
                f"above, verbatim. Do not compute new figures. Do not claim the catalog "
                f"supports, contradicts, or is silent in any direction other than the "
                f"verdict. Do not argue with the verdict. Rewrite it."
            )

    # Nothing the model produced could be trusted, so we say the true thing plainly. The
    # template leads with the verdict word from code, so it is faithful AND polarity-safe by
    # construction — which is exactly why it is the authoritative floor.
    return Explanation(
        text=template(result),
        source="template",
        faithfulness=faithfulness.check(template(result), result),
        polarity=polarity.check(template(result), result),
        conflicts=conflicts,
        rejected=tuple(rejected),
    )
