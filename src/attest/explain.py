"""Explanation generation: turn a verdict and its evidence into an audit-quality writeup.

The second — and last — job the semantic layer is allowed to do. The model gets to
*phrase* a verdict. It does not get to reach one, and it does not get to add to one.

What the model sees is deliberately narrow: the claim, the verdict, and the evidence
fields the deterministic checker returned. Never the raw catalog, never the snapshot,
never the claim on its own. It cannot go looking for a better fact, because it is not
given one.

What comes back is not trusted:

  1. **crosscheck** — the model also reports which verdict it thinks the evidence shows.
     Disagreement never changes the verdict; it is surfaced as a Conflict.
  2. **faithfulness** — every factual token in the prose must appear in the evidence.
     A hallucinated owner, column, tag, date, or number fails here.
  3. **rejection is not retried forever** — one repair attempt, with the violations
     handed back, and then we fall back to the deterministic template.

The template is the floor, and it is a floor worth standing on: it is built from the
checker's own reason string and the evidence, so it is always correct, always faithful,
and merely less fluent. **A failed explanation degrades to something true.** It never
degrades to something plausible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from attest import faithfulness
from attest.claims import CheckResult
from attest.config import Step
from attest.crosscheck import Conflict, crosscheck
from attest.faithfulness import Faithfulness
from attest.llm import LLM, LLMError

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

        # A conflicted explanation is rejected even if every token in it is faithful: it
        # is arguing with a verdict it was told was final, and shipping its prose next to
        # the opposite verdict would be incoherent. The conflict itself still surfaces.
        if last.ok and not conflicts:
            return Explanation(
                text=text,
                source="model",
                faithfulness=last,
                conflicts=conflicts,
                rejected=tuple(rejected),
            )

        why = "; ".join(
            [*(str(c) for c in conflicts), *(str(v) for v in last.violations)]
        )
        log.warning("rejected explanation draft (attempt %d): %s", attempt, why)
        rejected.append(f"attempt {attempt}: {why}")

        if attempt < max_attempts:
            prompt = (
                f"{_prompt(result)}\n\n"
                f"Your previous explanation was REJECTED: {why}\n"
                f"Every name, number, and identifier you write must appear in the evidence "
                f"above, verbatim. Do not compute new figures. Do not argue with the "
                f"verdict. Rewrite it."
            )

    # Nothing the model produced could be trusted, so we say the true thing plainly.
    return Explanation(
        text=template(result),
        source="template",
        faithfulness=faithfulness.check(template(result), result),
        conflicts=conflicts,
        rejected=tuple(rejected),
    )
