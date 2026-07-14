"""Cross-family calibration: a second, independent labeler for the benchmark's ground truth.

    python -m benchmark.labeler          # label all 40 cases with Nemotron, report agreement

--------------------------------------------------------------------------------
Why a second model, and why it must be a different FAMILY
--------------------------------------------------------------------------------

The benchmark's labels are mine. Every number in benchmark/README.md is measured against
them, so if they are wrong, everything downstream is wrong in a way no amount of green
output would reveal. The obvious check — ask a model whether my labels are right — has a
well-documented flaw: **LLM judges favor their own outputs.** Zheng et al. (2023), "Judging
LLM-as-a-Judge with MT-Bench and Chatbot Arena" (arXiv:2306.05685), measure GPT-4 preferring
its own generations at roughly a **10% higher win rate**, and Claude-v1 at roughly **25%**.

Attest's pipeline runs on gpt-4o-mini. Validating GPT-labeled ground truth with a GPT judge
would therefore inflate the agreement number by construction, quietly, in the direction that
flatters the report. So the second labeler is **Nemotron — a Llama-family model served by
NVIDIA**, and the family is the point. A different *provider* serving similar weights would
break nothing; a different *family* is what makes the agreement number mean something.

**It never touches the verdict path.** It labels the benchmark and nothing else. No model
decides a verdict in Attest (trajectory.py asserts it on every run), and that does not
soften because a second model has appeared in the repository. This one lives in
`benchmark/`, resolves its model through `settings.model_for(Step.CALIBRATION)` like every
other model call in the project, and goes through `attest.llm` — the only module allowed to
call a model — so the invariant is upheld rather than excepted.

--------------------------------------------------------------------------------
Few-shot, and why
--------------------------------------------------------------------------------

The prompt carries four worked examples. Zheng et al. measure few-shot prompting raising a
judge's self-consistency from **65% to 77.5%**, and the effect is worth having here for a
specific reason: this is not a taste judgement, it is the application of a declared policy
(checkers/policy.py) to catalog facts, and the examples are where that policy is *shown*
rather than merely stated.

**The examples are synthetic and none of them is a benchmark case.** They are about datasets
that do not exist. A few-shot example drawn from the set being labeled is answer-key leakage,
and it would show up as agreement.

--------------------------------------------------------------------------------
THE JUDGE IS NOT DETERMINISTIC, AND THAT IS A MEASUREMENT, NOT A CAVEAT
--------------------------------------------------------------------------------

Run this twice, with identical code, an identical prompt and temperature=0, and it disputes
a DIFFERENT SET OF CASES. Measured here, in this repository, not quoted from a paper: the
agreement RATE is stable while WHICH cases it disputes drifts between runs.

That has one immediate consequence for how this tool is used: **one run's dispute list is
not a fact about the labels.** Publishing it as though it were would make the finding an
artefact of which run happened to get written to disk. So the labeler runs k times (default
3) and separates two things that look identical from a single run:

  - **disputed in EVERY run** — a real disagreement. Either the label is wrong or the case
    is genuinely contested, and both are worth knowing.
  - **disputed in SOME runs** — judge noise. It says nothing about the label.

And it reports **judge self-consistency**: how often the model gives the same answer to the
same question across runs. That number is about the JUDGE, not about the benchmark, and it
is the single most important thing on the output — because a judge that answers its own
question differently at temperature=0 cannot be a source of ground truth. It can only
calibrate one.

Which is, in the end, the sharpest available argument for this entire architecture. Attest's
verdicts are date math and set membership. They cannot do this.

--------------------------------------------------------------------------------
What agreement does and does not prove
--------------------------------------------------------------------------------

Say this plainly, because it is easy to oversell. The labeler is given the same catalog facts
and the same governance policy a human labeler had. So agreement means: *an independent model
family, applying the declared policy to the declared facts, reaches the label I recorded.* It
validates that the labels FOLLOW FROM the policy — that I did not mis-apply my own rules, or
fat-finger a verdict.

It does NOT validate the policy itself. If the rule "an untagged column on an unreviewed
table is Insufficient-Coverage, not clean" is the wrong rule, both of us apply the wrong rule
and agree. That is a design question, argued in checkers/policy.py, and no amount of
model-labeling settles it.

**Disagreements are surfaced, never silently resolved.** A case where Nemotron and I differ
is a finding: either my label is wrong, or the case is ambiguous and should not be in a
benchmark. Both are things I want to know. Taking my own side by default and reporting the
agreement rate anyway would make this whole exercise decorative.
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from attest.checkers import policy
from attest.config import Step, settings
from attest.datahub import DatasetSnapshot
from attest.llm import LLM
from benchmark.cases import CASES, VERDICTS, Case
from benchmark.run_eval import RESULTS_DIR, Catalog

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "reason", "decisive_field"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(VERDICTS),
            "description": (
                "Supported: the catalog affirms the claim. Contradicted: the catalog "
                "positively disagrees with it. Insufficient-Coverage: the catalog is "
                "silent — it neither affirms nor denies."
            ),
        },
        "reason": {
            "type": "string",
            "description": "One sentence. Cite the catalog fact that decided it.",
        },
        "decisive_field": {
            "type": "string",
            "description": (
                "The single catalog field you decided on, e.g. 'table tags', 'column "
                "tags', 'lastModified', 'owners', 'schema columns', or 'nothing "
                "(the catalog is silent)'."
            ),
        },
    },
}

SYSTEM = f"""\
You are labeling ground truth for a data-catalog groundedness benchmark. You are given a
claim an AI agent made about a dataset, and the catalog's actual metadata for that dataset.
Decide what the CATALOG says about the claim.

THE THREE VERDICTS. The third one is the difficult one and it is not a weaker second one.
- Supported: the catalog affirms the claim.
- Contradicted: the catalog POSITIVELY DISAGREES with the claim.
- Insufficient-Coverage: the catalog is SILENT. It does not say. Absence of evidence, not
  evidence of absence.

THE GOVERNANCE POLICY YOU MUST APPLY. These are declared rules, not your judgement:

1. ABSENCE IS NOT DENIAL. An untagged table does not say "no PII". An unowned table does
   not say "nobody owns it". A dataset with no lastModified is not stale. These are all
   Insufficient-Coverage.

2. CLOSED-WORLD REASONING IS GRANTED, NEVER ASSUMED. Only if a table carries the
   `{policy.TAG}Verified` tag has its classification been declared COMPLETE. On such a
   table, and only there, a missing PII tag is a reviewed finding — so "this table is
   PII-free" is Supported, and "it contains PII" is Contradicted. Without Verified, both
   are Insufficient-Coverage.

3. THREE THINGS COUNT AS PII, ANY ONE IS ENOUGH:
   (a) the `{policy.PII_TAG}` tag;
   (b) a glossary term filed under the `{policy.PII_NODE}` node — membership of that node
       is what makes a term a PII signal, NOT how personal the term's name sounds;
   (c) the `{policy.HAS_PII_PROPERTY}` custom property set truthy.
   `{policy.HAS_PII_PROPERTY}=false` counts for NOTHING in either direction: a scanner
   that looked and found nothing is not a review.

4. PRECEDENCE WHEN SIGNALS DISAGREE:
   (a) COLUMN OVER TABLE. A claim about a column is decided by THAT COLUMN'S labels. A
       table's PII tag does not make its untagged columns PII ("contains PII" means
       somewhere in it, not everywhere in it).
   (b) BUT A COLUMN'S PII TAG SETTLES A TABLE-LEVEL CLAIM, because a table-level PII claim
       is existential: one PII column means the table contains PII.
   (c) WITHIN ONE GRAIN, AN EXPLICIT TAG BEATS AN IMPLIED SIGNAL. A NonPII tag is a human's
       classification act and outranks a glossary term (subject matter) or a scanner's
       property (a guess).
   (d) THE COMPLETENESS MARKER REACHES DOWN INTO COLUMNS, even though PII SIGNALS do not.
       These are different kinds of thing and that is why both are true at once: a signal
       is a fact about the DATA (and "contains PII" is existential, so it does not
       distribute over columns), while `Verified` is a fact about the REVIEW (the team
       looked at this dataset and tagged what they found). So on a Verified table an
       UNTAGGED column is a reviewed finding, not silence: "this column contains PII" is
       Contradicted, and "this column is PII-free" is Supported. On a table WITHOUT
       Verified, the same untagged column is Insufficient-Coverage.

5. FRESHNESS IS ARITHMETIC. Compare the age against the window the claim allows. Nothing
   else. A table is not "the stale one"; it is older or younger than a stated window.

6. OWNERS AND SCHEMA COLUMNS ARE EXHAUSTIVE LISTS. If the catalog lists owners and the
   claimed owner is not among them, that is Contradicted. If a schema exists and the claimed
   column is not in it, that is Contradicted. But if there is NO ownership aspect, or NO
   schema at all, that is Insufficient-Coverage — absence of a schema is not absence of a
   column.

Decide only from the catalog facts you are shown. Do not reason about what the data
probably contains. A column named `email` that nobody classified is UNCLASSIFIED — it is
not clean and it is not dirty.\
"""

# Worked examples. SYNTHETIC — none of these datasets exists, and none is a benchmark case.
# A few-shot example drawn from the set being labeled is answer-key leakage and would show
# up as agreement. Each example shows one rule being APPLIED rather than restated.
SHOTS: list[tuple[str, dict[str, str]]] = [
    (
        """\
CLAIM: "The table urn:li:dataset:(urn:li:dataPlatform:hive,demo.logs.clickstream,PROD) \
contains no PII."
CLAIM TYPE: classification (asserts the PII label is ABSENT, at table scope)

CATALOG FACTS
  owners: none recorded (the ownership aspect is absent)
  table tags: none
  table glossary terms: none
  hasPII property: absent
  columns: session_id (VARCHAR) [no labels]; page_url (VARCHAR) [no labels]\
""",
        {
            "verdict": "Insufficient-Coverage",
            "reason": (
                "The table carries no tags, no terms and no completeness marker, so the "
                "catalog has never reviewed it and cannot certify it clean."
            ),
            "decisive_field": "nothing (the catalog is silent)",
        },
    ),
    (
        """\
CLAIM: "The table urn:li:dataset:(urn:li:dataPlatform:hive,demo.sales.invoices,PROD) \
is free of PII."
CLAIM TYPE: classification (asserts the PII label is ABSENT, at table scope)

CATALOG FACTS
  owners: urn:li:corpuser:sam.lee
  table tags: urn:li:tag:Verified, urn:li:tag:Tier1
  table glossary terms: urn:li:glossaryTerm:Revenue (under node: none)
  hasPII property: absent
  columns: invoice_id (uuid) [tags: urn:li:tag:NonPII]; amount (numeric) [no labels]\
""",
        {
            "verdict": "Supported",
            "reason": (
                "No PII signal fires anywhere and the table is tagged Verified, so the "
                "absence is a reviewed finding rather than silence."
            ),
            "decisive_field": "table tags",
        },
    ),
    (
        """\
CLAIM: "The user_hash column of \
urn:li:dataset:(urn:li:dataPlatform:hive,demo.web.sessions,PROD) contains PII."
CLAIM TYPE: classification (asserts the PII label is PRESENT, on column 'user_hash')

CATALOG FACTS
  owners: urn:li:corpuser:sam.lee
  table tags: urn:li:tag:PII
  table glossary terms: urn:li:glossaryTerm:EmailAddress (under node: urn:li:glossaryNode:PII)
  hasPII property: true
  columns: user_hash (VARCHAR) [tags: urn:li:tag:NonPII]; ts (TIMESTAMP) [no labels]\
""",
        {
            "verdict": "Contradicted",
            "reason": (
                "The column itself is explicitly tagged NonPII, and a column-scoped claim "
                "is decided by that column's own labels, not by the table's."
            ),
            "decisive_field": "column tags",
        },
    ),
    (
        """\
CLAIM: "The table urn:li:dataset:(urn:li:dataPlatform:hive,demo.ops.heartbeat,PROD) \
is refreshed hourly."
CLAIM TYPE: freshness (the claim allows a maximum age of 1 hour)

CATALOG FACTS
  lastModified: absent — the catalog records no timestamp for this dataset
  owners: urn:li:corpuser:sam.lee
  table tags: urn:li:tag:Tier2\
""",
        {
            "verdict": "Insufficient-Coverage",
            "reason": (
                "The catalog holds no lastModified for this dataset, and not knowing when "
                "it last ran is not knowing that it is stale."
            ),
            "decisive_field": "nothing (the catalog is silent)",
        },
    ),
]


def describe_claim(case: Case) -> str:
    """The claim, in words, with no hint of the expected verdict anywhere near it."""
    claim = case.claim
    kind = claim["claim_type"]

    if kind == "freshness":
        what = f"freshness (the claim allows a maximum age of {claim['max_age_hours']} hours)"
    elif kind == "ownership":
        what = f"ownership (asserts the owner is {claim['owner_urn']})"
    elif kind == "classification":
        labels = ", ".join(claim["labels"])
        scope = (
            f"on column '{claim['field_path']}'" if claim.get("field_path") else "at table scope"
        )
        direction = "PRESENT" if claim["present"] else "ABSENT"
        what = f"classification (asserts {labels} is {direction}, {scope})"
    else:
        cols = ", ".join(
            c["name"] + (f" of type {c['native_type']}" if c["native_type"] else "")
            for c in claim["columns"]
        )
        what = f"schema (asserts the dataset has: {cols})"

    return f'CLAIM: "{case.agent_text}"\nCLAIM TYPE: {what}'


def describe_catalog(snapshot: DatasetSnapshot, now: datetime) -> str:
    """The catalog's facts, rendered deterministically. Exactly what a human labeler saw.

    Rendered by code, not summarized by a model: a labeler shown a model's summary of the
    catalog is labeling the summary. Absence is written as absence, everywhere, because the
    whole difficulty of this task is telling absence from denial.
    """
    lines = ["CATALOG FACTS"]

    if snapshot.last_modified is None:
        lines.append("  lastModified: absent — the catalog records no timestamp for this dataset")
    else:
        age = (now - snapshot.last_modified).total_seconds() / 3600
        lines.append(
            f"  lastModified: {snapshot.last_modified.isoformat()} "
            f"({age:.1f} hours before the reference time {now.isoformat()})"
        )

    if snapshot.owners is None:
        lines.append("  owners: none recorded (the ownership aspect is absent)")
    elif not snapshot.owners:
        lines.append("  owners: the ownership aspect exists but lists nobody")
    else:
        lines.append(f"  owners: {', '.join(snapshot.owners)}")

    lines.append(f"  table tags: {', '.join(snapshot.tags) if snapshot.tags else 'none'}")

    if snapshot.terms:
        rendered = [
            f"{t} (under node: {', '.join(snapshot.term_parents.get(t, ())) or 'none'})"
            for t in snapshot.terms
        ]
        lines.append(f"  table glossary terms: {'; '.join(rendered)}")
    else:
        lines.append("  table glossary terms: none")

    props = snapshot.custom_properties or {}
    has_pii = props.get(policy.HAS_PII_PROPERTY)
    lines.append(f"  {policy.HAS_PII_PROPERTY} property: {has_pii if has_pii else 'absent'}")

    if snapshot.fields is None:
        lines.append("  columns: NO SCHEMA has ever been ingested for this dataset")
    else:
        lines.append(f"  columns ({len(snapshot.fields)}, and the list is exhaustive):")
        for f in snapshot.fields:
            labels = []
            for label in f.labels:
                parents = snapshot.term_parents.get(label)
                labels.append(
                    f"{label} (under node: {', '.join(parents)})" if parents else label
                )
            shown = f"[{'; '.join(labels)}]" if labels else "[no labels]"
            lines.append(f"    - {f.path} ({f.native_type}) {shown}")

    return "\n".join(lines)


@dataclass
class Label:
    """One case, labeled twice: by a human, and by an independent model family."""

    case_id: str
    mine: str
    theirs: str
    reason: str
    decisive_field: str
    my_rationale: str

    @property
    def agrees(self) -> bool:
        return self.mine == self.theirs


def nemotron() -> LLM:
    """An LLM handle pointed at NVIDIA. Built here, called only through attest.llm."""
    if not settings.nvidia_api_key:
        raise SystemExit(
            "NVIDIA_API_KEY is not set. The benchmark's second labeler needs it; nothing "
            "else in Attest does — the pipeline runs on gpt-4o-mini and always will."
        )
    # certifi's bundle fails behind a TLS-inspecting network, and unlike llm.py's own
    # client this one is constructed here, so the repair is made here too. See llm.py.
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:  # pragma: no cover — a machine without truststore
        pass

    from openai import OpenAI

    # A TIMEOUT, and it is not boilerplate. The SDK's default is 10 minutes with retries, so
    # ONE wedged request stalls a 40-case run for half an hour and looks identical to a slow
    # one. This is a reasoning model — a labeling call legitimately takes ~10s and never 120.
    client = OpenAI(
        api_key=settings.nvidia_api_key,
        base_url=settings.nvidia_base_url,
        timeout=120.0,
        max_retries=3,
    ).chat.completions
    return LLM(client=client)  # type: ignore[arg-type]


def label(case: Case, catalog: Catalog, llm: LLM) -> Label:
    """Label one case with the second family. It never sees my verdict."""
    facts = describe_catalog(catalog.snapshot(case.target_urn), catalog.now)

    shots = "\n\n".join(
        f"--- worked example ---\n{prompt}\n\nYOUR ANSWER:\n{json.dumps(answer, indent=2)}"
        for prompt, answer in SHOTS
    )
    user = f"{shots}\n\n--- now label this one ---\n{describe_claim(case)}\n\n{facts}"

    payload = llm.json(
        step=Step.CALIBRATION,
        system=SYSTEM,
        user=user,
        schema=SCHEMA,
        schema_name="benchmark_label",
    )
    return Label(
        case_id=case.id,
        mine=case.expected_verdict,
        theirs=payload["verdict"],
        reason=payload.get("reason", ""),
        decisive_field=payload.get("decisive_field", ""),
        my_rationale=case.rationale,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-k",
        type=int,
        default=3,
        help=(
            "label the benchmark k times. NOT a nicety: this judge is not deterministic "
            "even at temperature=0, and one run's dispute list is not a fact about the "
            "labels. See the module docstring."
        ),
    )
    ap.add_argument("--out", type=Path, default=RESULTS_DIR / "calibration.json")
    args = ap.parse_args()

    catalog = Catalog()
    llm = nemotron()
    model = settings.model_for(Step.CALIBRATION)

    print(f"\nCross-family calibration: {len(CASES)} cases, k={args.k}")
    print("  my labels     benchmark/cases.py (hand-labeled)")
    print(f"  their labels  {model}")
    print(
        "  why           GPT must not grade GPT's homework: LLM judges favor their own\n"
        "                outputs (~10% higher win rate for GPT-4, ~25% for Claude-v1;\n"
        "                Zheng et al. 2023, arXiv:2306.05685). Nemotron is Llama-family."
    )

    # Each run is written to disk the moment it finishes. A single wedged request used to
    # stall the job and throw away every COMPLETED run with it — twenty minutes of real
    # measurement lost to one bad socket. A partial result is still a result.
    runs: list[list[Label]] = []
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for n in range(args.k):
        runs.append([label(case, catalog, llm) for case in CASES])
        rate = sum(x.agrees for x in runs[-1]) / len(CASES)
        print(f"  run {n + 1}/{args.k}: {rate:.1%} agreement", flush=True)
        (args.out.parent / f"calibration-run{n + 1}.json").write_text(
            json.dumps(
                [
                    {"case": x.case_id, "mine": x.mine, "theirs": x.theirs,
                     "their_reason": x.reason}
                    for x in runs[-1]
                ],
                indent=2,
            ),
            encoding="utf-8",
            newline="\n",
        )

    rates = [sum(x.agrees for x in run) / len(CASES) for run in runs]

    # Per case: how many of the k runs agreed with me, and how many distinct verdicts the
    # judge gave to the SAME question. The second is the interesting one.
    by_case: dict[str, list[Label]] = {}
    for run in runs:
        for x in run:
            by_case.setdefault(x.case_id, []).append(x)

    unstable = {c: v for c, v in by_case.items() if len({x.theirs for x in v}) > 1}
    always_disputed = {c: v for c, v in by_case.items() if not any(x.agrees for x in v)}
    sometimes = {
        c: v
        for c, v in by_case.items()
        if any(x.agrees for x in v) and not all(x.agrees for x in v)
    }

    print(f"\n{'=' * 78}\nAGREEMENT\n{'=' * 78}")
    print(f"  per run       {', '.join(f'{r:.1%}' for r in rates)}")
    print(f"  mean          {statistics.fmean(rates):.1%}")

    # THE NUMBER THAT MATTERS, and it is about the JUDGE rather than about the labels.
    consistency = 1 - len(unstable) / len(CASES)
    print(f"\n  judge self-consistency (same answer to the same question across {args.k} runs)")
    print(f"                {consistency:.1%}  —  {len(unstable)}/{len(CASES)} cases moved")
    if unstable:
        print("\n  A judge that answers its own question differently across runs, at")
        print("  temperature=0, cannot be a source of ground truth. It is a CALIBRATION")
        print("  instrument, and this is the measurement that says so. It is also exactly")
        print("  why Attest's verdicts come from date math instead of from a model.")
        for case_id, seen in unstable.items():
            print(f"    {case_id}: {[x.theirs for x in seen]}  (mine: {seen[0].mine})")

    if always_disputed:
        print(f"\n  DISPUTED IN EVERY RUN ({len(always_disputed)}) — a real disagreement.")
        print("  Either my label is wrong, or the case is genuinely contested. Surfaced,")
        print("  never resolved by taking my own side.\n")
        for case_id, seen in always_disputed.items():
            d = seen[0]
            print(f"    {case_id}")
            print(f"      mine    : {d.mine:22s} {d.my_rationale}")
            print(f"      theirs  : {d.theirs:22s} {d.reason}")

    if sometimes:
        print(f"\n  DISPUTED IN SOME RUNS ONLY ({len(sometimes)}) — judge noise, not a finding")
        print("  about the label. Reporting one run's list as 'the disputes' would be an")
        print("  artefact of which run got published.")
        for case_id, seen in sometimes.items():
            print(f"    {case_id}: agreed in {sum(x.agrees for x in seen)}/{args.k} runs")

    print(
        "\n  WHAT THIS DOES NOT PROVE: the labeler is given the same policy I used, so this"
        "\n  validates that my labels FOLLOW FROM the policy — not that the policy is right."
        "\n  That is a design argument, and it lives in checkers/policy.py."
    )

    # The unpriced-model rule, and it is not decorative here: Nemotron has no entry in
    # cost.py's price table, so its cost comes back None rather than a plausible zero.
    tokens = sum(u.total_tokens for u in llm.usage)
    print(f"\n  {len(llm.usage)} calls, {tokens} tokens, cost: unknown (no price for {model})")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "labeler": model,
                "family": "llama (NVIDIA Nemotron) — deliberately NOT the pipeline's family",
                "n_cases": len(CASES),
                "k": args.k,
                "agreement_per_run": rates,
                "agreement_mean": statistics.fmean(rates),
                "judge_self_consistency": consistency,
                "unstable_across_runs": {
                    c: [x.theirs for x in v] for c, v in unstable.items()
                },
                "disputed_in_every_run": [
                    {
                        "case": c,
                        "mine": v[0].mine,
                        "my_rationale": v[0].my_rationale,
                        "theirs": v[0].theirs,
                        "their_reason": v[0].reason,
                    }
                    for c, v in always_disputed.items()
                ],
                "disputed_in_some_runs": {
                    c: sum(x.agrees for x in v) for c, v in sometimes.items()
                },
                "tokens": tokens,
                "cost_usd": None,
            },
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"\nwrote {args.out}")

    # A benchmark whose ground truth an independent family disputes on more than ~10% of
    # cases has shakier ground truth than its author thinks. Judged on the MEAN, because
    # one run's number is not a fact about the labels either.
    mean = statistics.fmean(rates)
    if mean < 0.90:
        print(
            f"\n*** MEAN AGREEMENT IS {mean:.1%}, BELOW THE 90% THRESHOLD. The ground truth "
            f"is shakier than it looks — fix the labels or cut the ambiguous cases. ***"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
