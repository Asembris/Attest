"""The golden benchmark: 40 hand-labeled claims against the seeded catalog.

This module is the SOURCE of the dataset; `python -m benchmark.cases` writes
`benchmark/cases.json`, which is the artifact anyone else can use without reading a line
of Attest's code. Same discipline as seed/generate_seed.py: the generator is the truth,
the JSON is its output — except that this output IS committed, because a benchmark you
cannot cite is not a benchmark.

--------------------------------------------------------------------------------
What a label is, and what makes one defensible
--------------------------------------------------------------------------------

Each case carries the agent's prose, the structured claim that prose means, the expected
verdict, and **one line saying why that verdict is correct**. The one-line rule is a
forcing function, not a style preference: *if a label needs a paragraph to justify, the
claim is ambiguous and does not belong in a benchmark.* Ambiguous cases were cut, and the
cuts are listed in benchmark/README.md rather than quietly dropped — a benchmark that
silently excludes its hard cases is measuring the easy ones and reporting the average.

The label is what the CATALOG says, decided by the governance policy declared in
checkers/policy.py. It is not what a reasonable person would guess about the data. Those
come apart constantly, and every place they do is a case worth having:
`recipient_email_hash` reads as personal and is tagged NonPII; `CustomerIdentifier` reads
as personal and is deliberately outside the PII node; an untagged `email` column on an
unreviewed table is *unclassified*, not *clean*, and not *dirty* either.

--------------------------------------------------------------------------------
Why both the prose AND the structured claim
--------------------------------------------------------------------------------

The prose is what the full pipeline is fed: it exercises decomposition, the checker, the
explanation and the guard, end to end, which is the number a user actually experiences.
The structured claim is what the DETERMINISTIC CORE is fed directly, with no model in the
loop at all.

Carrying both lets the harness answer a question that one alone cannot: when an end-to-end
verdict is wrong, *was the checker wrong, or did the decomposer mis-transcribe the
sentence?* Those are different bugs with different fixes, and an aggregate accuracy number
blurs them into one. It also makes the pair itself a measurement — extraction fidelity is
"how often does the decomposer produce the claim a human says the sentence means", and the
harness reports it.

--------------------------------------------------------------------------------
Freshness labels are ARITHMETIC, and the boundary cases prove it
--------------------------------------------------------------------------------

The seed writes its fresh datasets 6h before seed time and its stale one 417 days before,
so `now` is reconstructed from the catalog itself (as tests/conftest.py does) and every
fresh dataset is 1.0h old, the stale one ~10003h. `revenue_daily` therefore appears
TWICE, with opposite labels: "refreshed within the past year" is Contradicted (10003h >
8760h) and "refreshed at least once in the past two years" is Supported (10003h < 17520h).
A checker that had learned "revenue_daily is the stale one" would get one of them wrong.
Same trick on the fresh side: `customer_profile` is Supported at 24h and Contradicted at
0.5h.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

BENCHMARK_DIR = Path(__file__).parent
CASES_JSON = BENCHMARK_DIR / "cases.json"


def sf(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:snowflake,{name},PROD)"


def pg(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,{name},PROD)"


# The seeded catalog, by the role each dataset plays. Named for what it PROVES.
DOCUMENTED = sf("analytics.customers.customer_profile")  # PII + Verified + fresh + owned
PII_UNVERIFIED = sf("analytics.customers.customer_contact")  # PII tag, NOT Verified
REVIEWED_CLEAN = sf("analytics.orders.orders_fact")  # Verified, no PII, hasPII=false
NONPII_TRAP = sf("analytics.marketing.email_campaign_stats")  # NonPII col, EmailAddress tbl
STALE = sf("analytics.finance.revenue_daily")  # 417 days old
UNREVIEWED = sf("analytics.staging.raw_events")  # schema only; catalog silent
PROPERTY_ONLY_PII = sf("analytics.product.device_telemetry")  # hasPII property only
NO_TIMESTAMP = sf("analytics.staging.pipeline_scratch")  # no lastModified
USERS = pg("attest_db.public.users")
PAYMENTS = pg("attest_db.public.payment_methods")  # PII table, NonPII card_last4
OWNED_BY_CAROL = pg("attest_db.public.support_tickets")
DEPRECATED_UNOWNED = pg("attest_db.public.legacy_accounts")  # untagged email, no owner
TAG_ONLY_PII = pg("attest_db.public.hr_headcount")  # PII by tag alone
TERM_ONLY_PII = pg("attest_db.public.marketing_leads")  # PII by glossary term alone
COLUMN_ONLY_PII = pg("attest_db.public.audit_log")  # PII on a column, nothing on the table
NO_SCHEMA = pg("attest_db.public.external_report")  # no schemaMetadata

ALICE = "urn:li:corpuser:alice.chen"
BOB = "urn:li:corpuser:bob.martinez"
CAROL = "urn:li:corpuser:carol.davis"
DANA = "urn:li:corpuser:dana.wu"

PII = "urn:li:tag:PII"
CUSTOMER_ID_TERM = "urn:li:glossaryTerm:CustomerIdentifier"

SUPPORTED = "Supported"
CONTRADICTED = "Contradicted"
INSUFFICIENT = "Insufficient-Coverage"


@dataclass(frozen=True)
class Case:
    """One labeled claim. The label is ground truth; the rationale is why."""

    id: str
    # What the agent said. Fed to the FULL pipeline: the URN must appear verbatim in it,
    # because decompose.py refuses to carry a URN the agent did not quote.
    agent_text: str
    target_urn: str
    # What that sentence MEANS, as a claim. Fed to the deterministic core directly, and
    # compared against what the decomposer extracted (extraction fidelity).
    claim: dict[str, Any]
    expected_verdict: str
    # ONE line. If this needs a paragraph, the case is ambiguous — cut it instead.
    rationale: str
    # Cases the benchmark exists for: the ones a plausible system gets wrong.
    hard: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def claim_type(self) -> str:
        return self.claim["claim_type"]

    @property
    def cell(self) -> str:
        return f"{self.claim_type}/{self.expected_verdict}"


def freshness(urn: str, hours: float, text: str) -> dict[str, Any]:
    return {"claim_type": "freshness", "target_urn": urn, "raw_text": text,
            "max_age_hours": hours}


def ownership(urn: str, owner: str, text: str) -> dict[str, Any]:
    return {"claim_type": "ownership", "target_urn": urn, "raw_text": text,
            "owner_urn": owner}


def classification(
    urn: str, text: str, labels: tuple[str, ...] = (PII,), present: bool = True,
    field_path: str | None = None,
) -> dict[str, Any]:
    return {"claim_type": "classification", "target_urn": urn, "raw_text": text,
            "labels": list(labels), "present": present, "field_path": field_path}


def schema(urn: str, text: str, columns: list[dict[str, Any]]) -> dict[str, Any]:
    return {"claim_type": "schema", "target_urn": urn, "raw_text": text,
            "columns": columns}


def col(name: str, native_type: str | None = None) -> dict[str, Any]:
    return {"name": name, "native_type": native_type}


# =============================================================================
# FRESHNESS — 7 cases. Pure date arithmetic, and the boundary pairs prove it.
# =============================================================================

FRESHNESS_CASES = [
    Case(
        id="fresh-01",
        agent_text=f"The table {DOCUMENTED} is updated at least once every 24 hours.",
        target_urn=DOCUMENTED,
        claim=freshness(DOCUMENTED, 24, "updated at least once every 24 hours"),
        expected_verdict=SUPPORTED,
        rationale="lastModified is 1.0h before the reference now, inside the 24h window.",
    ),
    Case(
        id="fresh-02",
        agent_text=(
            f"The table {STALE} has been refreshed at least once in the past two years."
        ),
        target_urn=STALE,
        claim=freshness(STALE, 17520, "refreshed at least once in the past two years"),
        expected_verdict=SUPPORTED,
        rationale=(
            "Age is ~10003h, inside the asserted 17520h window — stale is not a label, it is a "
            "comparison."
        ),
        hard=True,
        tags=("boundary", "same-dataset-opposite-verdicts"),
    ),
    Case(
        id="fresh-03",
        agent_text=f"The table {STALE} is rebuilt nightly.",
        target_urn=STALE,
        claim=freshness(STALE, 24, "rebuilt nightly"),
        expected_verdict=CONTRADICTED,
        rationale="Age is ~10003h, far outside the asserted 24h window.",
    ),
    Case(
        id="fresh-04",
        agent_text=f"The table {STALE} has been refreshed within the past year.",
        target_urn=STALE,
        claim=freshness(STALE, 8760, "refreshed within the past year"),
        expected_verdict=CONTRADICTED,
        rationale=(
            "Age is ~10003h, outside the asserted 8760h window — the same dataset is Supported at "
            "two years."
        ),
        hard=True,
        tags=("boundary", "same-dataset-opposite-verdicts"),
    ),
    Case(
        id="fresh-05",
        agent_text=f"The table {DOCUMENTED} is updated every 30 minutes.",
        target_urn=DOCUMENTED,
        claim=freshness(DOCUMENTED, 0.5, "updated every 30 minutes"),
        expected_verdict=CONTRADICTED,
        rationale=(
            "Age is 1.0h, outside the asserted 0.5h window — a fresh table is not fresh for every "
            "claim."
        ),
        hard=True,
        tags=("boundary",),
    ),
    Case(
        id="fresh-06",
        agent_text=f"The table {NO_TIMESTAMP} is refreshed daily.",
        target_urn=NO_TIMESTAMP,
        claim=freshness(NO_TIMESTAMP, 24, "refreshed daily"),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "The catalog holds no lastModified for it: not knowing when it ran is not knowing it "
            "is old."
        ),
        hard=True,
        tags=("absence-vs-denial",),
    ),
    Case(
        id="fresh-07",
        agent_text=f"The table {DEPRECATED_UNOWNED} is updated daily.",
        target_urn=DEPRECATED_UNOWNED,
        claim=freshness(DEPRECATED_UNOWNED, 24, "updated daily"),
        expected_verdict=CONTRADICTED,
        rationale=(
            "Age is ~10003h; the table having no owner does not make its timestamp unreadable."
        ),
        tags=("aspect-independence",),
    ),
]

# =============================================================================
# OWNERSHIP — 7 cases. Set membership over an exhaustive owners list.
# =============================================================================

OWNERSHIP_CASES = [
    Case(
        id="own-01",
        agent_text=f"The dataset {DOCUMENTED} is owned by {ALICE}.",
        target_urn=DOCUMENTED,
        claim=ownership(DOCUMENTED, ALICE, "owned by alice.chen"),
        expected_verdict=SUPPORTED,
        rationale="alice.chen is in the catalog's owners list.",
    ),
    Case(
        id="own-02",
        agent_text=f"The dataset {OWNED_BY_CAROL} is owned by {CAROL}.",
        target_urn=OWNED_BY_CAROL,
        claim=ownership(OWNED_BY_CAROL, CAROL, "owned by carol.davis"),
        expected_verdict=SUPPORTED,
        rationale="carol.davis is in the catalog's owners list.",
    ),
    Case(
        id="own-03",
        agent_text=f"The dataset {OWNED_BY_CAROL} is owned by {DANA}.",
        target_urn=OWNED_BY_CAROL,
        claim=ownership(OWNED_BY_CAROL, DANA, "owned by dana.wu"),
        expected_verdict=CONTRADICTED,
        rationale="The owners list is exhaustive and names carol.davis, not dana.wu.",
    ),
    Case(
        id="own-04",
        agent_text=f"The dataset {REVIEWED_CLEAN} is owned by {ALICE}.",
        target_urn=REVIEWED_CLEAN,
        claim=ownership(REVIEWED_CLEAN, ALICE, "owned by alice.chen"),
        expected_verdict=CONTRADICTED,
        rationale="The owners list is exhaustive and names bob.martinez, not alice.chen.",
    ),
    Case(
        id="own-05",
        agent_text=f"The dataset {UNREVIEWED} is owned by {ALICE}.",
        target_urn=UNREVIEWED,
        claim=ownership(UNREVIEWED, ALICE, "owned by alice.chen"),
        expected_verdict=INSUFFICIENT,
        rationale="No ownership aspect exists, so nobody is confirmed and nobody is ruled out.",
        hard=True,
        tags=("absence-vs-denial",),
    ),
    Case(
        id="own-06",
        agent_text=f"The dataset {DEPRECATED_UNOWNED} is owned by {DANA}.",
        target_urn=DEPRECATED_UNOWNED,
        claim=ownership(DEPRECATED_UNOWNED, DANA, "owned by dana.wu"),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "The table carries a Deprecated tag but no owner — a tag is not an ownership record."
        ),
        hard=True,
        tags=("absence-vs-denial",),
    ),
    Case(
        id="own-07",
        agent_text=f"The dataset {NO_SCHEMA} is owned by {BOB}.",
        target_urn=NO_SCHEMA,
        claim=ownership(NO_SCHEMA, BOB, "owned by bob.martinez"),
        expected_verdict=SUPPORTED,
        rationale=(
            "bob.martinez is listed; the dataset having no schema says nothing about its "
            "ownership."
        ),
        tags=("aspect-independence",),
    ),
]

# =============================================================================
# CLASSIFICATION — 19 cases. The claim type where the verdicts are hardest to
# keep apart and where getting them wrong does the most damage.
# =============================================================================

CLASSIFICATION_CASES = [
    # --- Supported ----------------------------------------------------------
    Case(
        id="class-01",
        agent_text=f"The table {DOCUMENTED} contains PII.",
        target_urn=DOCUMENTED,
        claim=classification(DOCUMENTED, "contains PII"),
        expected_verdict=SUPPORTED,
        rationale="The table carries the PII tag — an explicit classification act.",
    ),
    Case(
        id="class-02",
        agent_text=f"The email column of {DOCUMENTED} holds PII.",
        target_urn=DOCUMENTED,
        claim=classification(DOCUMENTED, "the email column holds PII", field_path="email"),
        expected_verdict=SUPPORTED,
        rationale="The email column carries its own PII tag.",
    ),
    Case(
        id="class-03",
        agent_text=f"The table {NONPII_TRAP} contains no PII.",
        target_urn=NONPII_TRAP,
        claim=classification(NONPII_TRAP, "contains no PII", present=False),
        expected_verdict=SUPPORTED,
        rationale="The table is explicitly tagged NonPII, which affirmatively rules PII out.",
        hard=True,
        tags=("precedence-explicit-over-implied",),
    ),
    Case(
        id="class-04",
        agent_text=f"The table {REVIEWED_CLEAN} is free of PII.",
        target_urn=REVIEWED_CLEAN,
        claim=classification(REVIEWED_CLEAN, "is free of PII", present=False),
        expected_verdict=SUPPORTED,
        rationale=(
            "No PII signal fires and the table is tagged Verified, so the absence is a reviewed "
            "finding rather than silence."
        ),
        hard=True,
        tags=("completeness-marker",),
    ),
    Case(
        id="class-05",
        agent_text=(
            f"The table {DOCUMENTED} is annotated with the {CUSTOMER_ID_TERM} "
            f"glossary term."
        ),
        target_urn=DOCUMENTED,
        claim=classification(
            DOCUMENTED, "annotated with the CustomerIdentifier term",
            labels=(CUSTOMER_ID_TERM,),
        ),
        expected_verdict=SUPPORTED,
        rationale=(
            "The term is attached to the table; it is a real annotation, and deliberately not a "
            "PII one."
        ),
        hard=True,
        tags=("customer-identifier-is-not-pii",),
    ),
    Case(
        id="class-06",
        agent_text=f"The work_email column of {TERM_ONLY_PII} contains personal data.",
        target_urn=TERM_ONLY_PII,
        claim=classification(
            TERM_ONLY_PII, "the work_email column contains personal data",
            field_path="work_email",
        ),
        expected_verdict=SUPPORTED,
        rationale=(
            "The column carries the EmailAddress term, which is filed under the PII glossary "
            "node."
        ),
        hard=True,
        tags=("term-signal",),
    ),
    # --- Contradicted -------------------------------------------------------
    Case(
        id="class-07",
        agent_text=f"The recipient_email_hash column of {NONPII_TRAP} contains PII.",
        target_urn=NONPII_TRAP,
        claim=classification(
            NONPII_TRAP, "the recipient_email_hash column contains PII",
            field_path="recipient_email_hash",
        ),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The column is explicitly tagged NonPII — it is a salted hash, and the catalog says "
            "so."
        ),
        hard=True,
        tags=("the-trap", "column-over-table"),
    ),
    Case(
        id="class-08",
        agent_text=f"The table {NONPII_TRAP} contains PII.",
        target_urn=NONPII_TRAP,
        claim=classification(NONPII_TRAP, "contains PII"),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The table's explicit NonPII tag outranks the EmailAddress term it also carries — a "
            "tag is a classification act, a term is subject matter."
        ),
        hard=True,
        tags=("precedence-explicit-over-implied",),
    ),
    Case(
        id="class-09",
        agent_text=f"The table {COLUMN_ONLY_PII} is PII-free.",
        target_urn=COLUMN_ONLY_PII,
        claim=classification(COLUMN_ONLY_PII, "is PII-free", present=False),
        expected_verdict=CONTRADICTED,
        rationale=(
            "Its actor_email column is tagged PII, and a table-level PII claim is existential — "
            "one PII column settles it."
        ),
        hard=True,
        tags=("column-propagates-up",),
    ),
    Case(
        id="class-10",
        agent_text=f"The actor_email column of {COLUMN_ONLY_PII} holds no personal data.",
        target_urn=COLUMN_ONLY_PII,
        claim=classification(
            COLUMN_ONLY_PII, "the actor_email column holds no personal data",
            present=False, field_path="actor_email",
        ),
        expected_verdict=CONTRADICTED,
        rationale="The column is tagged PII, whatever the untagged table says.",
        hard=True,
        tags=("column-over-table",),
    ),
    Case(
        id="class-11",
        agent_text=f"The table {TAG_ONLY_PII} contains no PII.",
        target_urn=TAG_ONLY_PII,
        claim=classification(TAG_ONLY_PII, "contains no PII", present=False),
        expected_verdict=CONTRADICTED,
        rationale=(
            "PII is asserted here by the global tag alone — no glossary term anywhere on the "
            "dataset."
        ),
        hard=True,
        tags=("tag-signal",),
    ),
    Case(
        id="class-12",
        agent_text=f"The table {TERM_ONLY_PII} contains no PII.",
        target_urn=TERM_ONLY_PII,
        claim=classification(TERM_ONLY_PII, "contains no PII", present=False),
        expected_verdict=CONTRADICTED,
        rationale=(
            "PII is asserted here by glossary terms under the PII node alone — no PII tag "
            "anywhere."
        ),
        hard=True,
        tags=("term-signal",),
    ),
    Case(
        id="class-13",
        agent_text=f"The table {PROPERTY_ONLY_PII} contains no PII.",
        target_urn=PROPERTY_ONLY_PII,
        claim=classification(PROPERTY_ONLY_PII, "contains no PII", present=False),
        expected_verdict=CONTRADICTED,
        rationale=(
            "A classifier recorded hasPII=true on it — no tag, no term, and the finding still "
            "counts."
        ),
        hard=True,
        tags=("property-signal",),
    ),
    Case(
        id="class-14",
        agent_text=f"The card_last4 column of {PAYMENTS} is PII.",
        target_urn=PAYMENTS,
        claim=classification(PAYMENTS, "the card_last4 column is PII", field_path="card_last4"),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The column is tagged NonPII even though its table is tagged PII — table PII means "
            "'somewhere in here', not 'everywhere'."
        ),
        hard=True,
        tags=("column-over-table", "no-downward-propagation"),
    ),
    Case(
        id="class-15",
        agent_text=f"The signup_ts column of {DOCUMENTED} contains PII.",
        target_urn=DOCUMENTED,
        claim=classification(
            DOCUMENTED, "the signup_ts column contains PII", field_path="signup_ts"
        ),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The column is untagged on a Verified table, so the omission is a reviewed finding "
            "rather than an oversight."
        ),
        hard=True,
        tags=("completeness-marker", "pairs-with-class-17"),
    ),
    # --- Insufficient-Coverage ---------------------------------------------
    Case(
        id="class-16",
        agent_text=f"The table {UNREVIEWED} contains no PII.",
        target_urn=UNREVIEWED,
        claim=classification(UNREVIEWED, "contains no PII", present=False),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "Nothing is tagged and nothing declares the review complete: absence of a PII tag is "
            "not evidence of absence of PII."
        ),
        hard=True,
        tags=("the-central-case", "absence-vs-denial"),
    ),
    Case(
        id="class-17",
        agent_text=f"The verified_at column of {PII_UNVERIFIED} contains PII.",
        target_urn=PII_UNVERIFIED,
        claim=classification(
            PII_UNVERIFIED, "the verified_at column contains PII", field_path="verified_at",
        ),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "The column is untagged and its table carries no completeness marker, so a PII tag on "
            "the table settles nothing about it."
        ),
        hard=True,
        tags=("no-downward-propagation", "pairs-with-class-15"),
    ),
    Case(
        id="class-18",
        agent_text=f"The email column of {DEPRECATED_UNOWNED} contains PII.",
        target_urn=DEPRECATED_UNOWNED,
        claim=classification(
            DEPRECATED_UNOWNED, "the email column contains PII", field_path="email",
        ),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "A column literally named email, untagged, on a table nobody reviewed: unclassified "
            "is not clean and not dirty."
        ),
        hard=True,
        tags=("the-distinction-that-must-not-blur",),
    ),
    Case(
        id="class-19",
        agent_text=f"The table {UNREVIEWED} contains PII.",
        target_urn=UNREVIEWED,
        claim=classification(UNREVIEWED, "contains PII"),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "The table is unclassified, so the catalog can neither confirm nor deny it — the "
            "mirror of class-16."
        ),
        hard=True,
        tags=("absence-vs-denial",),
    ),
]

# =============================================================================
# SCHEMA — 7 cases. String and type comparison over an exhaustive column list.
# =============================================================================

SCHEMA_CASES = [
    Case(
        id="schema-01",
        agent_text=f"{DOCUMENTED} has an email column of type VARCHAR(255).",
        target_urn=DOCUMENTED,
        claim=schema(DOCUMENTED, "has an email column of type VARCHAR(255)",
                     [col("email", "VARCHAR(255)")]),
        expected_verdict=SUPPORTED,
        rationale="The schema lists email as VARCHAR(255).",
    ),
    Case(
        id="schema-02",
        agent_text=f"{REVIEWED_CLEAN} has an order_total column of type NUMBER(12,2).",
        target_urn=REVIEWED_CLEAN,
        claim=schema(REVIEWED_CLEAN, "has an order_total column of type NUMBER(12,2)",
                     [col("order_total", "NUMBER(12,2)")]),
        expected_verdict=SUPPORTED,
        rationale="The schema lists order_total as NUMBER(12,2).",
    ),
    Case(
        id="schema-03",
        agent_text=f"{UNREVIEWED} has a payload column.",
        target_urn=UNREVIEWED,
        claim=schema(UNREVIEWED, "has a payload column", [col("payload")]),
        expected_verdict=SUPPORTED,
        rationale=(
            "The schema lists payload; a dataset with no owner and no tags still has a schema."
        ),
        tags=("aspect-independence",),
    ),
    Case(
        id="schema-04",
        agent_text=f"{USERS} has an email column of type varchar(255).",
        target_urn=USERS,
        claim=schema(USERS, "has an email column of type varchar(255)",
                     [col("email", "varchar(255)")]),
        expected_verdict=SUPPORTED,
        rationale="The schema lists email as varchar(255).",
    ),
    Case(
        id="schema-05",
        agent_text=f"{DOCUMENTED} has an ssn column.",
        target_urn=DOCUMENTED,
        claim=schema(DOCUMENTED, "has an ssn column", [col("ssn")]),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The schema is exhaustive and lists no ssn column — and the claim is unrevisable, "
            "because 'it has no ssn column' is not a schema claim anyone can make."
        ),
        hard=True,
        tags=("unrevisable", "stood-firm"),
    ),
    Case(
        id="schema-06",
        agent_text=f"{REVIEWED_CLEAN} has an order_total column of type VARCHAR(32).",
        target_urn=REVIEWED_CLEAN,
        claim=schema(REVIEWED_CLEAN, "has an order_total column of type VARCHAR(32)",
                     [col("order_total", "VARCHAR(32)")]),
        expected_verdict=CONTRADICTED,
        rationale=(
            "The column exists but is NUMBER(12,2) — the right column, the wrong type, which is a "
            "revisable error."
        ),
        hard=True,
        tags=("revisable-type",),
    ),
    Case(
        id="schema-07",
        agent_text=f"{NO_SCHEMA} has a revenue_amount column.",
        target_urn=NO_SCHEMA,
        claim=schema(NO_SCHEMA, "has a revenue_amount column", [col("revenue_amount")]),
        expected_verdict=INSUFFICIENT,
        rationale=(
            "No schema has ever been ingested for it: absence of a schema is not absence of a "
            "column."
        ),
        hard=True,
        tags=("absence-vs-denial",),
    ),
]


CASES: list[Case] = [
    *FRESHNESS_CASES,
    *OWNERSHIP_CASES,
    *CLASSIFICATION_CASES,
    *SCHEMA_CASES,
]

CLAIM_TYPES = ("freshness", "ownership", "classification", "schema")
VERDICTS = (SUPPORTED, CONTRADICTED, INSUFFICIENT)


def coverage() -> dict[str, int]:
    """The 12-cell map: how many cases land in each (claim type x verdict) cell."""
    counts = {f"{t}/{v}": 0 for t in CLAIM_TYPES for v in VERDICTS}
    for case in CASES:
        counts[case.cell] += 1
    return counts


def validate() -> None:
    """Hold the dataset to its own rules. Run at import of the JSON, and in the suite."""
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"

    dark = [cell for cell, n in coverage().items() if n == 0]
    assert not dark, f"unexercised (claim type x verdict) cells: {dark}"

    for case in CASES:
        assert case.target_urn in case.agent_text, (
            f"{case.id}: the URN must appear VERBATIM in the agent's prose, or decompose.py "
            f"will refuse to carry the claim — see the URN rule."
        )
        assert case.claim["target_urn"] == case.target_urn, f"{case.id}: URN mismatch"
        assert case.expected_verdict in VERDICTS, f"{case.id}: unknown verdict"
        assert case.rationale, f"{case.id}: a label with no rationale is not ground truth"
        # The one-line rule, enforced. A label that needs a paragraph is an ambiguous
        # case, and an ambiguous case belongs in the README's cut-list, not the dataset.
        assert "\n" not in case.rationale, f"{case.id}: rationale must be one line"


def as_dict() -> dict[str, Any]:
    validate()
    return {
        "name": "attest-golden-benchmark",
        "version": 1,
        "description": (
            "Hand-labeled groundedness claims about a seeded DataHub catalog. Each case "
            "carries an agent's sentence, the claim it means, the verdict the catalog "
            "supports, and a one-line rationale."
        ),
        "verdicts": {
            "Supported": "The catalog affirms the claim.",
            "Contradicted": "The catalog positively disagrees.",
            "Insufficient-Coverage": "The catalog is silent. Absence, not disagreement.",
        },
        "catalog": "seed/generate_seed.py, ingested into DataHub Core v1.5.0.6",
        "n_cases": len(CASES),
        "n_hard": sum(1 for c in CASES if c.hard),
        "coverage": coverage(),
        "cases": [asdict(c) | {"cell": c.cell} for c in CASES],
    }


def main() -> None:
    payload = as_dict()
    CASES_JSON.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n"
    )
    print(f"wrote {payload['n_cases']} cases ({payload['n_hard']} hard) -> {CASES_JSON}")
    for cell, n in payload["coverage"].items():
        print(f"  {cell:40s} {n}")


if __name__ == "__main__":
    main()
