"""THE EXTERNAL TRIAL: Attest against a catalog it did not author.

    just external-trial --receipt docs/external-trial/<name>.json    run it, write the receipt
    just external-trial --dry-run  resolve every target, print what the catalog holds, audit
                                   nothing (free, no model, no writes)

**A receipt is never overwritten and there is no --force** (`spikes/evidence.py`). The
default path is the committed historical artifact, so a bare run now REFUSES rather than
clobbering it: a second run must name a second file, which shows up in the diff and cannot
hide. Overwriting a receipt destroys the artifact a committed number traces to.

**The catalog-wide census lives in `spikes/external_census.py`, not here** — two different
experiments, run against one loaded catalog state. This runner's `catalog` block used to
carry `datasets_readable_by_attest: 52` / `datasets_refused: 15` as LITERALS, never measured
by the run that reported them; they are `null` here and the census receipt holds the
measurement. None-is-not-zero, and a hardcoded figure reported as a measurement is the
fabrication this project exists to catch.

**This is NOT a second benchmark, and the distinction is load-bearing.** The golden
benchmark (benchmark/README.md) is a conformance gate over a catalog `seed/generate_seed.py`
wrote to be a witness: 100% is the EXPECTED result there, because the checkers implement
exactly the rules the labels encode. Re-using that frame here — accuracy, macro-F1, a
confusion matrix over 15 claims — would import a scoring apparatus this run has not earned
and would undercut the benchmark's own methodology argument. So nothing here is scored.

The question is different, and narrower:

    Does Attest produce DEFENSIBLE verdicts against metadata it did not design,
    and WHERE does it hit its own documented limits?

A mixed result is the goal. An Insufficient-Coverage that a human would argue with is a
finding, not a failure — and a trial where nothing surprised us is a trial that dodged.

The catalog is DataHub's own `showcase-ecommerce` datapack (67 datasets over 7 platforms),
ingested with original timestamps preserved. docs/external-trial/ingest.md pins the URLs and
checksums; docs/external-trial.md is the write-up.

Every expected verdict below was determined by READING the catalog through Attest's own
client before the run, plus the DECLARED policy in checkers/policy.py — never by running the
checker and writing down what came out. `human_reading` is separate on purpose: it is what a
person reading the same catalog page would say, and where it disagrees with `expected` the
disagreement is the finding.

Publishes 2 verdicts to the external catalog (`--no-publish` to skip). Uses a TEMPORARY
store, so `attest.db` is untouched.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from attest.api.service import AuditService
from attest.config import settings
from attest.datahub.client import DataHubClient, DataHubError
from attest.graph import Pipeline
from attest.record import AuditRecord
from attest.report import Decision
from attest.retrieval import ClaimQuery, ClaimReader
from attest.store import AuditStore
from evidence import ReceiptExists, provenance, write_receipt

REPO = Path(__file__).resolve().parent.parent

# The BASELINE: the pre-CorpGroup run, committed 2026-08-04. It is the historical "before"
# artifact and it is never written to again -- `write_receipt` refuses any path that exists,
# so this constant is now the DEFAULT that makes a careless run fail loudly rather than the
# destination it used to be.
RECEIPT = REPO / "docs" / "external-trial" / "results.json"

# The one-line identity of the change this trial is measuring. Recorded in provenance so a
# receipt says what it was a receipt OF. `2d7eaf9` is the commit reachable from `main`; an
# earlier amend of the same tree (`7b06288`) is dangling and is deliberately not cited.
FIX_UNDER_TEST = "2d7eaf9 fix(datahub): read CorpGroup owners, not only CorpUser"

# The pack's URN prefix. Every target below is a showcase dataset; none is Attest's own seed.
PREFIX = "b2fd91."
SENTINEL = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,"
    "b2fd91.order_entry_db.order_entry.customers,PROD)"
)


def urn(platform: str, name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{PREFIX}{name},PROD)"


SNOW_CUSTOMERS = urn("snowflake", "order_entry_db.order_entry.customers")
SNOW_ORDERS = urn("snowflake", "order_entry_db.order_entry.orders")
SNOW_ORDER_HISTORY = urn("snowflake", "order_entry_db.analytics.order_history")
DBT_ORDER_HISTORY = urn("dbt", "ORDER_ENTRY_DB.analytics.order_history")
DBT_ORDERS = urn("dbt", "order_entry_db.order_entry.orders")
POWERBI_ORDER_DETAILS = urn("powerbi", "datahub_order_entries.ORDER_DETAILS")
TABLEAU_PROMOTIONS = urn("tableau", "b980a8c5-28eb-119e-f6ca-4da32732e5be")

EMAIL_TERM = "urn:li:glossaryTerm:b2fd91.Email_Address"
PII_TERM = "urn:li:glossaryTerm:b2fd91.1598cf93-c199-43a1-8833-fce96faa9a1a"


@dataclass(frozen=True)
class TrialClaim:
    """One claim, with its expectation formed BEFORE the run."""

    id: str
    family: str
    prose: str
    target_urn: str
    # What the catalog + the declared policy say this must be. Not what the checker returned.
    expected: str
    # What a competent person reading the same catalog page would say. Where this differs
    # from `expected`, THAT is the finding — not a bug to be fixed by moving `expected`.
    human_reading: str
    # Why `expected` is what it is, in one line, read off the catalog.
    #
    # WHERE AN EXPECTATION HAS MOVED (`expected_before` below is set), this field is left
    # BYTE-IDENTICAL to what it said when it was written, and it documents `expected_before`.
    # `expectation_moved_because` documents the new `expected`. Editing this to match the
    # new expectation would rewrite the record of what was believed before the fix, which is
    # the one thing a before/after pair exists to preserve.
    basis: str
    # The documented Attest limit this claim probes, if any.
    probes: str = ""
    # Publish this verdict to the external catalog?
    publish: bool = False
    # --- a PREDECLARED change of expectation, committed BEFORE the catalog is loaded -----
    #
    # Empty on every case whose expectation has not moved, which must stay the overwhelming
    # majority: a trial whose labels move with its results measures nothing. The pair is
    # additive so that the baseline receipt stays readable beside this one -- the before
    # value travels IN the after-receipt, rather than being recoverable only by checking the
    # source out at an older commit.
    expected_before: str = ""
    expectation_moved_because: str = ""


CLAIMS: tuple[TrialClaim, ...] = (
    # --- freshness ----------------------------------------------------------
    TrialClaim(
        id="ext-fresh-01",
        family="freshness",
        prose=(
            f"The dataset {SNOW_ORDER_HISTORY} is updated within the last 24 hours."
        ),
        target_urn=SNOW_ORDER_HISTORY,
        expected="Contradicted",
        human_reading="Contradicted — agrees.",
        basis="lastModified 2026-01-28T00:09:27Z, ~4530h before the run; the window is 24h.",
        publish=True,
    ),
    TrialClaim(
        id="ext-fresh-02",
        family="freshness",
        prose=(
            f"The dataset {SNOW_ORDER_HISTORY} has been refreshed at least once "
            f"in the past two years."
        ),
        target_urn=SNOW_ORDER_HISTORY,
        expected="Supported",
        human_reading="Supported — agrees.",
        basis="Same timestamp, ~4530h, inside a 17520h window. Same table as ext-fresh-01, "
        "opposite verdict: freshness is arithmetic against a stated window, not a label.",
        publish=True,
    ),
    TrialClaim(
        id="ext-fresh-03",
        family="freshness",
        prose=f"The dataset {DBT_ORDER_HISTORY} is refreshed daily.",
        target_urn=DBT_ORDER_HISTORY,
        expected="Insufficient-Coverage",
        human_reading="Insufficient-Coverage — agrees, but the evidence will be imprecise.",
        basis="GMS returns lastModified {time: 0}. Attest reads a falsy epoch as absent, "
        "which is the right verdict from a wrong description: the aspect is PRESENT and "
        "holds a meaningless zero.",
        probes="absent-vs-empty, one notch finer than snapshot.py preserves",
    ),
    TrialClaim(
        id="ext-fresh-04",
        family="freshness",
        prose=f"The dataset {POWERBI_ORDER_DETAILS} is refreshed weekly.",
        target_urn=POWERBI_ORDER_DETAILS,
        expected="Insufficient-Coverage",
        human_reading="Insufficient-Coverage — agrees.",
        basis="No lastModified at all. A GENUINELY absent aspect, distinct from ext-fresh-03.",
    ),
    # --- ownership ----------------------------------------------------------
    TrialClaim(
        id="ext-own-01",
        family="ownership",
        prose=(
            f"The dataset {TABLEAU_PROMOTIONS} is owned by "
            f"urn:li:corpuser:b2fd91.brock1@example.com."
        ),
        target_urn=TABLEAU_PROMOTIONS,
        expected="Supported",
        human_reading="Supported — agrees.",
        basis="That URN is the sole listed owner.",
    ),
    TrialClaim(
        id="ext-own-02",
        family="ownership",
        prose=(
            f"The dataset {POWERBI_ORDER_DETAILS} is owned by "
            f"urn:li:corpuser:b2fd91.brock1@example.com."
        ),
        target_urn=POWERBI_ORDER_DETAILS,
        expected="Contradicted",
        human_reading="Contradicted — agrees.",
        basis="The owners list is exhaustive and holds only kirk@example.com.",
    ),
    TrialClaim(
        id="ext-own-03",
        family="ownership",
        prose=(
            f"The dataset {SNOW_CUSTOMERS} is owned by "
            f"urn:li:corpuser:b2fd91.brock1@example.com."
        ),
        target_urn=SNOW_CUSTOMERS,
        expected="Insufficient-Coverage",
        human_reading="Insufficient-Coverage — agrees.",
        basis="Ownership aspect absent. 47 of 52 readable datasets are unowned.",
    ),
    TrialClaim(
        id="ext-own-04",
        family="ownership",
        prose=(
            f"The dataset {DBT_ORDERS} is owned by "
            f"urn:li:corpGroup:b2fd91.ORG_DATA_PLATFORM."
        ),
        target_urn=DBT_ORDERS,
        expected="Supported",
        human_reading=(
            "Supported. The catalog plainly lists ORG_DATA_PLATFORM as the technical "
            "owner; a person reading the DataHub page sees it. Attest cannot."
        ),
        basis="THE HEADLINE. Attest's DATASET_QUERY asks for `owner { ... on CorpUser }` "
        "and has no CorpGroup arm, so a group owner comes back `{}` and the Session 23 "
        "guard refuses to normalize it. Fails CLOSED (correct) with a wrong diagnosis.",
        probes="CorpGroup ownership — 15 of 67 datasets unauditable",
        expected_before="ClaimError",
        expectation_moved_because=(
            "2d7eaf9 added the `... on CorpGroup { urn }` arm to client.DATASET_QUERY. The "
            "catalog lists urn:li:corpGroup:b2fd91.ORG_DATA_PLATFORM as the technical owner "
            "and this claim names that URN, so the owners list is now readable, the claim is "
            "decidable, and set membership holds. THE HYPOTHESIS IS THAT `expected` "
            "CONVERGES ON `human_reading` — the disagreement that WAS the finding closing. "
            "A verdict of Supported is not on its own sufficient evidence: the claim's "
            "evidence must NAME the corpGroup URN, or something other than the CorpGroup "
            "arm produced it."
        ),
    ),
    # --- classification -----------------------------------------------------
    TrialClaim(
        id="ext-class-01",
        family="classification",
        prose=(
            f"The cust_email column of {SNOW_CUSTOMERS} is labelled {EMAIL_TERM}."
        ),
        target_urn=SNOW_CUSTOMERS,
        expected="Supported",
        human_reading="Supported — agrees.",
        basis="Set membership over a vocabulary Attest has never seen. The GENERIC label "
        "path is portable; only the PII policy is vocabulary-bound.",
    ),
    TrialClaim(
        id="ext-class-02",
        family="classification",
        prose=f"The cust_email column of {SNOW_CUSTOMERS} contains PII.",
        target_urn=SNOW_CUSTOMERS,
        expected="Insufficient-Coverage",
        human_reading=(
            "PII. The column is named cust_email, holds email addresses, and carries a "
            "term whose own description reads 'Subject to PII handling requirements'."
        ),
        basis="THE SHARPEST CASE. None of policy.PII_SIGNALS fires: no urn:li:tag:PII, no "
        "term under urn:li:glossaryNode:PII (Email_Address has NO parent node at all), no "
        "hasPII property. No Verified marker, so no closed-world reasoning. Attest infers "
        "nothing from a name — by design, and here that costs it the answer.",
        probes="semantic glossary-term matching (a documented deferred item)",
        publish=True,
    ),
    TrialClaim(
        id="ext-class-03",
        family="classification",
        prose=f"The dataset {SNOW_CUSTOMERS} contains no PII.",
        target_urn=SNOW_CUSTOMERS,
        expected="Insufficient-Coverage",
        human_reading=(
            "Contradicted. The table has cust_email, phone_number and dob columns. But "
            "Insufficient-Coverage is the CONSERVATIVE error here, not the dangerous one: "
            "Attest refuses to certify, which is the point."
        ),
        basis="No PII signal at either grain and no completeness marker anywhere in this "
        "catalog, so 'PII-free' can never be Supported here. The false-assurance guard.",
    ),
    TrialClaim(
        id="ext-class-04",
        family="classification",
        prose=f"The dataset {SNOW_ORDERS} is not labelled {PII_TERM}.",
        target_urn=SNOW_ORDERS,
        expected="Contradicted",
        human_reading="Contradicted — agrees.",
        basis="That term IS attached. It is the term literally named 'PII', filed under a "
        "node named 'Classification' — legible to Attest as an identifier, invisible to it "
        "as a PII signal.",
    ),
    # --- schema -------------------------------------------------------------
    TrialClaim(
        id="ext-schema-01",
        family="schema",
        prose=f"The dataset {SNOW_CUSTOMERS} has a cust_email column of type VARCHAR.",
        target_urn=SNOW_CUSTOMERS,
        expected="Supported",
        human_reading="Supported — agrees.",
        basis="VARCHAR(16777216); precision is ignored by design.",
    ),
    TrialClaim(
        id="ext-schema-02",
        family="schema",
        prose=f"The dataset {SNOW_CUSTOMERS} has an ssn column.",
        target_urn=SNOW_CUSTOMERS,
        expected="Contradicted",
        human_reading="Contradicted — agrees.",
        basis="The 22-column schema is exhaustive and lists no ssn.",
    ),
    TrialClaim(
        id="ext-schema-03",
        family="schema",
        prose=f"The zipcode column of {SNOW_CUSTOMERS} is of type VARCHAR.",
        target_urn=SNOW_CUSTOMERS,
        expected="Contradicted",
        human_reading=(
            "Contradicted — agrees. The catalog records NUMBER(38,0), which is a "
            "questionable way to model a zipcode; Attest reports what the catalog says "
            "and does not opine on the modelling, which is correct."
        ),
        basis="native NUMBER(38,0), abstract NUMBER. Neither matches varchar.",
    ),
)


# A claim drafted and DISCARDED during selection, kept because dropping it is the
# methodology being honest about itself rather than shipping a label that was wrong.
DISCARDED = {
    "prose": (
        "The customer_id column of the S3 customers table is of type NUMBER(38,0)."
    ),
    "predicted": "Contradicted",
    "actual_on_tracing": "Supported",
    "why": (
        "Predicted Contradicted because S3 records customer_id as `int64` while Snowflake "
        "records `NUMBER(38,0)`. Tracing checkers/schema.py::_type_matches before writing "
        "the label: the matcher accepts EITHER of DataHub's two type vocabularies and strips "
        "precision, so the asserted `NUMBER(38,0)` base-matches the abstract type `NUMBER`, "
        "which is what S3 carries too. Supported — and correct. The claim was dropped rather "
        "than shipped with a wrong expectation."
    ),
}


# Predictions recorded BEFORE the run, so the write-up can say which held.
PREDICTIONS = (
    {
        "id": "pred-1",
        "prediction": "At least one long UUID-bearing URN is garbled by the decomposer, "
        "producing an EntityNotFoundError ClaimError. Seeded URNs are short and word-like; "
        "these carry hex UUIDs.",
    },
    {
        "id": "pred-2",
        "prediction": "The guard's template-fallback rate rises above the seeded catalog's "
        "0-of-40, because faithfulness.py treats capitalized words as a factual class and "
        "this catalog's evidence is UUIDs, NUMBER(38,0), VARCHAR(16777216).",
    },
    {
        "id": "pred-3",
        "prediction": "ext-fresh-03's evidence says the aspect is ABSENT when it is present "
        "holding {time: 0}.",
    },
    {
        "id": "pred-4",
        "prediction": "contains_pii='False' moves nothing (correct), and no output mentions "
        "that the catalog said something PII-adjacent Attest could not read.",
    },
)


# The predeclaration, DERIVED from CLAIMS rather than restated beside it. A hand-written
# list would be a second place to keep the truth and would drift from the cases it describes
# -- the trajectory.py rule (declare the expected path, but never twice).
HYPOTHESIS = {
    "what": (
        "Does the CorpGroup fix (2d7eaf9) close the gap this trial measured on 2026-08-04, "
        "against the same foreign catalog, the same 15 cases and the same methodology?"
    ),
    "fix_under_test": FIX_UNDER_TEST,
    "declared_before_the_catalog_was_loaded": True,
    "predeclared_changes": [
        {
            "id": c.id,
            "before": c.expected_before,
            "after": c.expected,
            "reason": c.expectation_moved_because,
        }
        for c in CLAIMS
        if c.expected_before
    ],
    "predeclared_frozen": (
        "every other case: id, family, prose, target_urn, expected, human_reading, basis, "
        "probes, publish. Also frozen: PREDICTIONS, DISCARDED, and the definitions of "
        "matched_expectation / answered_the_intended_question / right_by_luck."
    ),
    "census_hypothesis": (
        "The 15 refused datasets were refused SOLELY because of the missing CorpGroup union "
        "arm, so the count goes to 0. STATED AS A HYPOTHESIS AND NOT AS AN ACCEPTANCE "
        "REQUIREMENT: a surviving refusal is a new finding to be classified, not a failed "
        "run. Measured by spikes/external_census.py, both ways, on one catalog state."
    ),
    "not_sufficient_evidence": (
        "A Supported verdict on ext-own-04 alone does not establish the fix: the claim's "
        "evidence must name urn:li:corpGroup:b2fd91.ORG_DATA_PLATFORM."
    ),
}


@dataclass
class Outcome:
    id: str
    family: str
    prose: str
    target_urn: str
    expected: str
    human_reading: str
    basis: str
    probes: str
    # Carried into the receipt so the before value travels WITH the after result, and the
    # two receipts line up without anyone checking the source out at two commits.
    expected_before: str = ""
    expectation_moved_because: str = ""
    # --- what actually happened -------------------------------------------
    verdict: str = ""
    matched_expectation: bool = False
    reason: str = ""
    evidence: list[dict] = field(default_factory=list)
    explanation: str = ""
    explanation_source: str = ""
    extracted_claim: dict | None = None
    extracted_claim_type: str = ""
    extracted_urn: str = ""
    urn_transcribed_verbatim: bool = False
    # Did the decomposer produce a claim of the family the sentence was written to make?
    # A verdict that matches the expectation while answering a DIFFERENT question is right
    # by luck, and banking it flatters a broken decomposer — benchmark/README.md's rule.
    answered_the_intended_question: bool = False
    right_by_luck: bool = False
    error: str = ""
    run_id: str = ""
    published: bool = False
    # Did the catalog take it on the FIRST attempt, or only after the repair path waited out
    # the index? On a freshly bulk-loaded catalog this is False and that is the finding.
    published_on_first_attempt: bool = False
    claim_urn: str = ""


def preflight(client: DataHubClient) -> None:
    """Refuse to run against a catalog that is not the one this trial is about."""
    try:
        snap = client.fetch_dataset(SENTINEL)
    except DataHubError as exc:
        raise SystemExit(
            f"the showcase catalog is not loaded ({type(exc).__name__}: {exc}).\n"
            f"See docs/external-trial/ingest.md — it pins the pack URLs and the recipe."
        ) from exc
    if not snap.fields:
        raise SystemExit(f"{SENTINEL} resolved but carries no schema; is the ingest complete?")


def dry_run(client: DataHubClient) -> int:
    """Resolve every target and print what the catalog holds. No model, no writes, free."""
    print("\n=== DRY RUN: what the external catalog holds at each target\n")
    seen: set[str] = set()
    failures = 0
    for c in CLAIMS:
        if c.target_urn in seen:
            continue
        seen.add(c.target_urn)
        try:
            s = client.fetch_dataset(c.target_urn)
        except DataHubError as exc:
            failures += 1
            print(f"  {c.target_urn.split(',')[1]:52} !! {type(exc).__name__}")
            continue
        lm = s.last_modified.isoformat() if s.last_modified else "ABSENT"
        print(
            f"  {c.target_urn.split(',')[1]:52} lm={lm:32} "
            f"owners={len(s.owners or ()) if s.owners is not None else 'ABSENT'} "
            f"tags={len(s.tags or ())} terms={len(s.terms or ())} cols={len(s.fields or ())}"
        )
    print(f"\n  {len(seen) - failures}/{len(seen)} targets readable, {failures} refused")
    return 0


def audit_one(service: AuditService, c: TrialClaim) -> Outcome:
    out = Outcome(
        id=c.id,
        family=c.family,
        prose=c.prose,
        target_urn=c.target_urn,
        expected=c.expected,
        human_reading=c.human_reading,
        basis=c.basis,
        probes=c.probes,
        expected_before=c.expected_before,
        expectation_moved_because=c.expectation_moved_because,
    )
    try:
        record: AuditRecord = service.audit(c.prose, source_agent="external-trial")
    except Exception as exc:  # a provider outage, most likely — see llm.ProviderError
        out.error = f"{type(exc).__name__}: {exc}"
        out.verdict = "RUN-FAILED"
        return out

    out.run_id = record.run_id

    if record.claims:
        claim = record.claims[0]
        out.verdict = claim.verdict
        out.reason = claim.reason
        out.evidence = [
            {"field": e.field, "value": e.value, "note": e.note} for e in claim.evidence
        ]
        out.explanation = claim.explanation
        out.explanation_source = claim.explanation_source
        out.extracted_claim = claim.claim
        out.extracted_urn = claim.target_urn
    elif record.errors:
        err = record.errors[0]
        out.verdict = "ClaimError"
        out.reason = err.error
        out.extracted_urn = err.target_urn
    else:
        # The decomposer produced nothing checkable. A visible GAP, not a verdict.
        out.verdict = "No-Claim"
        out.reason = (
            "; ".join(f"{d.reason}: {d.payload}" for d in record.dropped)
            if record.dropped
            else "no claim extracted"
        )

    out.urn_transcribed_verbatim = out.extracted_urn == c.target_urn
    out.matched_expectation = out.verdict == c.expected
    out.extracted_claim_type = (out.extracted_claim or {}).get("claim_type", "")
    # A ClaimError never got as far as a typed claim, so there is no family to compare and
    # it is not counted either way — the same reason a ClaimError is not a verdict.
    out.answered_the_intended_question = (
        out.extracted_claim_type == c.family if out.extracted_claim else out.verdict == c.expected
    )
    out.right_by_luck = out.matched_expectation and not out.answered_the_intended_question
    return out


# A freshly BULK-LOADED catalog does not index an assertion within writeback.py's 30s
# retry. Measured on this trial's first run: right after ingesting 3563 MCPs, all three
# publishes failed at `report` with "Assertion ... does not exist or is not associated with
# any entity", a repair attempt ~2 minutes later failed identically, and the same repair
# succeeded unchanged at ~12 minutes. It is the eventual-consistency landmine (CLAUDE.md
# §10) at a scale the seeded catalog never produces, because seeding writes ~90 entities and
# this wrote 3563.
#
# So the trial waits it out through the PRODUCT's own repair path rather than pretending the
# write succeeded — and gives up loudly rather than hanging, because a publish that never
# lands is a result too.
REPAIR_ATTEMPTS = 12
REPAIR_WAIT_SECONDS = 60


def publish(service: AuditService, outcomes: list[Outcome], by_id: dict[str, TrialClaim]) -> None:
    """Publish the nominated verdicts to the EXTERNAL catalog, through the real flow.

    Nothing calls write_claim_artifact directly: a human's Decision goes through
    service.approve exactly as the API does, so what reaches the catalog reaches it the
    only way anything ever does. A failed write is then retried through service.retry_writeback
    — the same call `POST /audit/{run_id}/writeback` makes, which approves nothing and can
    only re-run a decision a human already took.
    """
    print("\n=== PUBLISHING to the external catalog (the real approval path)\n")
    nominated = [
        o
        for o in outcomes
        if by_id[o.id].publish and o.verdict not in ("ClaimError", "No-Claim", "RUN-FAILED")
    ]
    for out in nominated:
        try:
            _record, writes = service.approve(
                out.run_id,
                [Decision(claim_index=0, publish=True, reviewer="external-trial")],
            )
        except Exception as exc:
            print(f"  {out.id}: approve failed — {type(exc).__name__}: {exc}")
            continue
        for w in writes:
            out.published = bool(w.ok)
            out.published_on_first_attempt = bool(w.ok)
            out.claim_urn = w.claim_urn or ""
            status = "written" if w.ok else f"FAILED at {w.failed_step}"
            print(f"  {out.id}  {out.verdict:22} -> {status}")
            print(f"      {out.claim_urn}")

    repair(service, [o for o in nominated if not o.published])


def repair(service: AuditService, pending: list[Outcome]) -> None:
    """Re-run the catalog write for verdicts a human accepted and the index would not take."""
    if not pending:
        return
    print(
        f"\n=== {len(pending)} write(s) failed. Retrying through the repair path "
        f"(up to {REPAIR_ATTEMPTS}x{REPAIR_WAIT_SECONDS}s).\n"
        "    This is index lag on a freshly bulk-loaded catalog, not a lost decision:\n"
        "    the approval stands and the store records that the catalog does not know it.\n"
    )
    for attempt in range(1, REPAIR_ATTEMPTS + 1):
        time.sleep(REPAIR_WAIT_SECONDS)
        still: list[Outcome] = []
        for out in pending:
            try:
                writes = service.retry_writeback(out.run_id)
            except Exception as exc:
                print(f"    {out.id}: repair raised {type(exc).__name__}: {exc}")
                still.append(out)
                continue
            ok = all(w.ok for w in writes) and bool(writes)
            out.published = ok
            if not ok:
                still.append(out)
        print(f"    attempt {attempt}: {len(pending) - len(still)} landed, {len(still)} pending")
        pending = still
        if not pending:
            print("    all published.")
            return
    for out in pending:
        print(
            f"    !! {out.id} NEVER PUBLISHED. The approval stands and the catalog does not "
            f"know it; POST /audit/{out.run_id}/writeback repairs it."
        )


def verify_published(client: DataHubClient, outcomes: list[Outcome]) -> list[dict]:
    """Read the published verdicts back out of DataHub with NO store.

    `store=None` is the second agent: it inherits whatever the catalog alone can tell it.
    If this comes back empty the write-back did not land, whatever the receipts said.
    """
    published = [o for o in outcomes if o.published]
    if not published:
        return []
    print("\n=== READING THEM BACK (ClaimReader with store=None — the second agent)\n")
    reader = ClaimReader(client, store=None)
    seen: list[dict] = []
    published_urns = {o.claim_urn for o in published if o.claim_urn}
    for target in sorted({o.target_urn for o in published}):
        page = reader.list(ClaimQuery(target_urn=target))
        for retrieved in page.claims:
            art = retrieved.artifact
            if art.claim_urn not in published_urns:
                continue  # another claim on the same dataset; not this trial's
            latest = art.history[-1] if art.history else None
            row = {
                "claim_urn": art.claim_urn,
                "target_urn": art.target_urn,
                "claim_type": art.claim_type,
                "description": art.description,
                "verdict": latest.verdict if latest else None,
                "reviewer": latest.reviewer if latest else None,
                "snapshot_id": latest.snapshot_id if latest else "",
                "evidence": [
                    {"field": e.field, "value": e.value, "note": e.note}
                    for e in (latest.evidence if latest else ())
                ],
                "state": retrieved.state.value,
                "stale_tag": retrieved.stale_tag,
                "history_length": len(art.history),
            }
            seen.append(row)
            print(
                f"  {row['verdict'] or 'no verdict':24} {row['state']:12} "
                f"history={row['history_length']}  {art.claim_urn}"
            )
    return seen


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve every target and print what the catalog holds; audit nothing",
    )
    ap.add_argument(
        "--no-publish", action="store_true", help="audit only; write nothing to the catalog"
    )
    ap.add_argument(
        "--receipt",
        type=Path,
        default=RECEIPT,
        help=(
            "where to write the receipt. An EXISTING path is refused and there is no "
            "--force; the default is the committed baseline, so a careless run fails "
            "loudly instead of destroying it."
        ),
    )
    args = ap.parse_args()

    receipt_path: Path = args.receipt if args.receipt.is_absolute() else REPO / args.receipt
    # Refused BEFORE the model is called and before anything is written to the catalog: a
    # run that cannot record its result must not spend money or touch someone's catalog.
    if not args.dry_run and receipt_path.exists():
        raise SystemExit(
            f"{receipt_path} already exists and this will not overwrite a receipt.\n"
            f"Pass --receipt with a NEW path. Nothing has been audited or published."
        )

    client = DataHubClient()
    preflight(client)
    if args.dry_run:
        return dry_run(client)

    # settings, not os.environ: the key legitimately lives in the repo's .env, and reading
    # the environment alone would refuse a correctly configured machine.
    if not settings.openai_api_key:
        raise SystemExit(
            "OPENAI_API_KEY is not set (checked the environment and the repo's .env); "
            "the trial runs the real pipeline."
        )

    started = datetime.now(tz=UTC)
    print(f"\n=== EXTERNAL TRIAL — {len(CLAIMS)} claims, real pipeline, {started.isoformat()}")
    print("    catalog: DataHub showcase-ecommerce (see docs/external-trial/ingest.md)")
    print(f"    receipt: {receipt_path.relative_to(REPO)}")
    # Printed BEFORE any result appears, so the operator reads the predeclaration first.
    for change in HYPOTHESIS["predeclared_changes"]:
        print(
            f"    PREDECLARED: {change['id']} expected "
            f"{change['before']} -> {change['after']}"
        )

    # A TEMPORARY store and checkpointer: the trial must not touch the demo's attest.db.
    # Both are passed explicitly rather than configured, so no ambient setting can point
    # this run at the store a judge is about to look at.
    tmp = Path(tempfile.mkdtemp(prefix="attest-external-trial-"))
    print(f"    store:   {tmp} (temporary; attest.db is untouched)")
    store = AuditStore(tmp / "trial.db")
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(tmp / "checkpoints.db")) as saver:
        pipeline = Pipeline(client=client, saver=saver)
        service = AuditService(pipeline, store, client=client)

        outcomes: list[Outcome] = []
        for c in CLAIMS:
            out = audit_one(service, c)
            outcomes.append(out)
            mark = "ok " if out.matched_expectation else "DIFF"
            print(f"  [{mark}] {out.id:16} {out.verdict:24} (expected {out.expected})")
            if out.error:
                print(f"         !! {out.error}")

        by_id = {c.id: c for c in CLAIMS}
        if not args.no_publish:
            publish(service, outcomes, by_id)
        readback = verify_published(client, outcomes)

    finished = datetime.now(tz=UTC)
    receipt = build_receipt(started, finished, outcomes, readback, store, client.gms_url)
    try:
        write_receipt(receipt_path, receipt)
    except ReceiptExists as exc:
        # Only reachable if the file appeared during the run. The audit is done and paid
        # for, so the result is printed rather than lost.
        print(f"\n!! {exc}\n\n{json.dumps(receipt, indent=2)}")
        return 1
    summarize(receipt)
    print(f"\nreceipt -> {receipt_path.relative_to(REPO)}")
    return 0


def build_receipt(
    started: datetime,
    finished: datetime,
    outcomes: list[Outcome],
    readback: list[dict],
    store: AuditStore,
    gms_url: str,
) -> dict:
    usd = 0.0
    priced = True
    for out in outcomes:
        if not out.run_id:
            continue
        rec = store.load(out.run_id)
        if rec is None:
            continue
        if rec.receipts.usd is None:
            priced = False
        else:
            usd += rec.receipts.usd

    verdicts: dict[str, int] = {}
    for out in outcomes:
        verdicts[out.verdict] = verdicts.get(out.verdict, 0) + 1

    return {
        "what_this_is": (
            "A trial of Attest against a DataHub catalog it did not author. NOT a benchmark: "
            "nothing here is scored, and no accuracy, macro-F1 or confusion matrix is "
            "computed over it. See docs/external-trial.md."
        ),
        "reproduce": "just external-trial --receipt <path>",
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "provenance": provenance(
            fix_under_test=FIX_UNDER_TEST, gms_url=gms_url, baseline_receipt=RECEIPT
        ),
        "hypothesis": HYPOTHESIS,
        "catalog": {
            "source": "DataHub showcase-ecommerce datapack",
            "ingest": "docs/external-trial/ingest.md",
            # A property of the checksum-pinned pack, not a measurement of Attest.
            "datasets_in_pack": 67,
            # NULL, NOT ZERO, and not the 52/15 that used to sit here. Those were hardcoded
            # literals in this function -- never measured by the run that reported them --
            # so an after-run would have emitted "15 refused" whatever the fix did. This
            # trial does not measure the catalog; the census does, and says which.
            "datasets_readable_by_attest": None,
            "datasets_refused": None,
            "census": (
                "measured separately, on the same loaded catalog state, by "
                "spikes/external_census.py. The baseline receipt's 52/15 were HARDCODED, "
                "never measured."
            ),
        },
        "totals": {
            "claims": len(outcomes),
            "matched_expectation": sum(1 for o in outcomes if o.matched_expectation),
            "verdicts": verdicts,
            "urns_transcribed_verbatim": sum(
                1 for o in outcomes if o.urn_transcribed_verbatim
            ),
            # Extraction fidelity, reported SEPARATELY from the verdict match and never
            # netted against it. A claim decided correctly on the wrong question is named
            # here rather than banked above.
            "answered_the_intended_question": sum(
                1 for o in outcomes if o.answered_the_intended_question
            ),
            "right_by_luck": sum(1 for o in outcomes if o.right_by_luck),
            "right_by_luck_ids": [o.id for o in outcomes if o.right_by_luck],
            "model_authored_explanations": sum(
                1 for o in outcomes if o.explanation_source == "model"
            ),
            "template_fallbacks": sum(
                1 for o in outcomes if o.explanation_source == "template"
            ),
            "usd": round(usd, 5) if priced else None,
            "published": sum(1 for o in outcomes if o.published),
            "published_on_first_attempt": sum(
                1 for o in outcomes if o.published_on_first_attempt
            ),
        },
        "predictions_made_before_the_run": list(PREDICTIONS),
        "discarded_claim": DISCARDED,
        "claims": [asdict(o) for o in outcomes],
        "published_read_back_from_datahub_with_no_store": readback,
    }


def summarize(receipt: dict) -> None:
    t = receipt["totals"]
    print("\n" + "=" * 70)
    print(f"  claims                        {t['claims']}")
    print(f"  matched the pre-run reading   {t['matched_expectation']}/{t['claims']}")
    print(f"  verdicts                      {t['verdicts']}")
    print(f"  URNs transcribed verbatim     {t['urns_transcribed_verbatim']}/{t['claims']}")
    print(
        f"  answered the intended q'n     {t['answered_the_intended_question']}/{t['claims']}"
        + (f"   RIGHT BY LUCK: {t['right_by_luck_ids']}" if t["right_by_luck"] else "")
    )
    print(
        f"  explanations model/template   {t['model_authored_explanations']}/"
        f"{t['template_fallbacks']}"
    )
    print(
        f"  published                     {t['published']} "
        f"({t['published_on_first_attempt']} on the first attempt)"
    )
    print(f"  cost                          {t['usd']}")
    print("=" * 70)
    print("  NOT A SCORE. See docs/external-trial.md for what this does and does not prove.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
