"""Attest's own audit history. SQLite today, Postgres-shaped throughout.

--------------------------------------------------------------------------------
Why the history lives HERE and not in DataHub
--------------------------------------------------------------------------------

This is the Session 0 decision, and it is worth restating because the opposite looks
tidier: put the verdicts on the dataset in DataHub, and there is no second database.

DataHub's `structuredProperties` are **last-write-wins and unversioned**. Write a verdict
onto `customer_profile` today and it replaces the one from yesterday, which is gone — not
superseded, not archived, gone. That makes the catalog a fine place to answer *what is
true of this dataset right now* and a hopeless place to answer any of the questions an
auditor is actually for:

  - has this agent's ownership accuracy improved since March?
  - was this claim ever contradicted before someone fixed the tag?
  - who approved this correction, and what evidence were they shown?
  - what did the audit that produced this verdict cost, and did it keep to its trajectory?

Every one of those is a question about *events*, and a last-write-wins field has no events
in it. So: **DataHub is the catalog, not the event store.** The catalog holds the current
verdict, written back on approval and queryable there (see writeback.py). The history —
every run, every claim, every piece of evidence, every human decision — lives in this
database, and the catalog carries a run id pointing back into it.

--------------------------------------------------------------------------------
The shape, and what SQLite is and is not doing here
--------------------------------------------------------------------------------

Plain SQL over `sqlite3`, no ORM. The schema uses only TEXT / INTEGER / REAL, ISO-8601
strings for timestamps, integer 0/1 for booleans, and no SQLite-only syntax, so the DDL
moves to Postgres by changing the connection and nothing else. Structured payloads that
are read whole and never filtered on (a claim's body, a correction's attempts) are stored
as JSON in a TEXT column; everything anybody would ever want to filter BY — verdict,
claim type, target URN, outcome, review status, timestamp — is a real column with an index
on it. That is what makes "every contradicted ownership claim this week" a query rather
than a scan-and-parse.

**`approvals` is append-only, and that is the point.** A decision is an event: it happened,
at a time, by a person. Re-deciding a claim writes a second row rather than overwriting the
first, so the record of who signed off on what survives someone changing their mind. The
review status shown on a claim is the LATEST decision; the earlier ones are still there.
Overwriting them would reproduce, in Attest's own store, the exact property that disqualified
DataHub from holding the history.

--------------------------------------------------------------------------------
Session 5 changed the schema, and an old database is REFUSED rather than half-read
--------------------------------------------------------------------------------

Durable resume rebuilds a parked run's typed ledger out of this store (replay.py), which
made every lossy column a correctness bug rather than a cosmetic one. Four columns were
added (`claims.rejected`, `claims.violations`, `steps.models`, `steps.error`) and three
changed from rendered strings to structured JSON (`claims.conflicts`, `runs.dropped`,
`runs.injection_findings`) — a `str()` cannot be parsed back, and a resumed run has to
re-render them exactly.

A pre-Session-5 database physically cannot hold a Session-5 record, and its rows cannot be
migrated: the structure was never written down, only its rendering. So `AuditStore` detects
one and REFUSES to open it, by name, with what to do about it. The alternative — inferring
the missing structure from the rendered string — would mean Attest fabricating fields to
fill its own audit trail, which is the one thing it is least entitled to do. The alternative
after that, letting the INSERT fail at run time inside `just serve`, is a demo-breaker
disguised as a migration policy.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from attest import writeback
from attest.record import (
    AttemptView,
    AuditRecord,
    ClaimErrorRecord,
    ClaimRecord,
    ConflictView,
    CorrectionView,
    DroppedView,
    EvidenceView,
    FindingView,
    PublicationView,
    Receipts,
    StepView,
    ViolationView,
)
from attest.report import (
    CorrectionOutcome,
    Decision,
    PublicationStatus,
    ReviewStatus,
    RunStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id              TEXT PRIMARY KEY,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL,
    source_agent        TEXT NOT NULL DEFAULT '',
    source_text         TEXT NOT NULL DEFAULT '',
    latency_ms          REAL NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    -- NULL means the cost is UNKNOWN (an unpriced model), never that it was free.
    usd                 REAL,
    n_steps             INTEGER NOT NULL DEFAULT 0,
    trajectory_ok       INTEGER NOT NULL DEFAULT 1,
    trajectory_summary  TEXT NOT NULL DEFAULT '',
    rules_checked       TEXT NOT NULL DEFAULT '[]',
    catalog_lookups     INTEGER NOT NULL DEFAULT 0,
    catalog_fetches     INTEGER NOT NULL DEFAULT 0,
    catalog_entities    INTEGER NOT NULL DEFAULT 0,
    -- JSON objects, not rendered strings: [{reason, payload}] and [{pattern, matched}].
    -- A resumed run rebuilds these, and a string cannot be parsed back into a pair.
    dropped             TEXT NOT NULL DEFAULT '[]',
    injection_findings  TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS claims (
    run_id              TEXT NOT NULL,
    claim_index         INTEGER NOT NULL,
    claim_type          TEXT NOT NULL,
    target_urn          TEXT NOT NULL,
    raw_text            TEXT NOT NULL DEFAULT '',
    claim_json          TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    reason              TEXT NOT NULL DEFAULT '',
    -- The identity of the catalog snapshot this verdict was decided against (Session 21).
    -- A real column because the write-back reads it at approval time from THIS record: it is
    -- captured at audit time and must not be re-derived by re-fetching a catalog that may
    -- have moved. See writeback.py and the snapshot-cache consistency boundary.
    snapshot_id         TEXT NOT NULL DEFAULT '',
    explanation         TEXT NOT NULL DEFAULT '',
    explanation_source  TEXT NOT NULL DEFAULT 'template',
    faithful            INTEGER NOT NULL DEFAULT 1,
    -- WHICH tokens the guard rejected. `faithful = 0` with nothing here would be a finding
    -- with no evidence, which is not a shape this system is entitled to store.
    violations          TEXT NOT NULL DEFAULT '[]',
    conflicts           TEXT NOT NULL DEFAULT '[]',
    -- The model drafts that were thrown away, and why. An auditor that quietly retries
    -- until something passes is hiding its own failure rate.
    rejected            TEXT NOT NULL DEFAULT '[]',
    -- Whether a human cleared this VERDICT for the catalog: pending | published | withheld.
    -- A real column rather than a blob, because "what have we published, and who signed it
    -- off" is a question you filter by. Kept SEPARATE from corrections.review on purpose:
    -- publishing a verdict is not accepting a correction, and fusing them is what kept every
    -- Supported, Insufficient-Coverage and STOOD_FIRM verdict out of the catalog entirely.
    -- See report.PublicationStatus.
    publication         TEXT NOT NULL DEFAULT 'pending',
    published_by        TEXT NOT NULL DEFAULT '',
    -- The claim artifact's URN in DataHub: the JOIN KEY between Attest's record of what it
    -- did and the catalog's record of what it holds (Session 16). DERIVED from claim_json,
    -- never minted — writeback.claim_urn, content-addressed — and stored because the
    -- retrieval path filters BY it, which the schema's own rule says makes it a column and
    -- not a blob. Not unique: the SAME claim audited twice lands on ONE artifact from two
    -- runs, which is exactly what the content-addressing is for.
    claim_urn           TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, claim_index)
);

CREATE TABLE IF NOT EXISTS evidence (
    run_id        TEXT NOT NULL,
    claim_index   INTEGER NOT NULL,
    seq           INTEGER NOT NULL,
    field         TEXT NOT NULL,
    -- JSON, because a catalog value may be a string, a number, or a list. JSON 'null'
    -- is a real value here: it is the catalog being silent, which is evidence.
    value_json    TEXT NOT NULL DEFAULT 'null',
    note          TEXT,
    PRIMARY KEY (run_id, claim_index, seq)
);

CREATE TABLE IF NOT EXISTS corrections (
    run_id        TEXT NOT NULL,
    claim_index   INTEGER NOT NULL,
    outcome       TEXT NOT NULL,
    review        TEXT NOT NULL,
    proposal_json TEXT,
    attempts      TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, claim_index)
);

CREATE TABLE IF NOT EXISTS claim_errors (
    run_id        TEXT NOT NULL,
    claim_index   INTEGER NOT NULL,
    target_urn    TEXT NOT NULL,
    claim_json    TEXT NOT NULL,
    error         TEXT NOT NULL,
    PRIMARY KEY (run_id, claim_index)
);

CREATE TABLE IF NOT EXISTS steps (
    run_id        TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    claim_index   INTEGER,
    latency_ms    REAL NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    -- NULL means UNPRICED, never free — and `models` is how a reader (and a resumed run)
    -- knows WHICH model could not be priced. Drop the names and the run's total silently
    -- becomes a sum where it should have been None. See cost.py and replay.py.
    cost_usd      REAL,
    models        TEXT NOT NULL DEFAULT '[]',
    -- A step that raised still ran, and the trace says so.
    error         TEXT,
    PRIMARY KEY (run_id, seq)
);

-- APPEND-ONLY. A decision is an event; re-deciding writes a new row. See the module docstring.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    claim_index   INTEGER NOT NULL,
    -- TWO acts, never one flag. NULL means the decision did not settle that question --
    -- "no opinion", which is not "no". Folding them back into one boolean would re-create
    -- the coupling that kept every Supported, Insufficient-Coverage and STOOD_FIRM verdict
    -- out of the catalog. See report.Decision.
    publish           INTEGER,
    accept_correction INTEGER,
    reviewer      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL,
    -- What happened when the accepted verdict was written back to DataHub, RENDERED for a
    -- human reading the log: 'written to <urn>', 'skipped', or 'failed at <step>: <why>'.
    -- An approval whose write-back failed is not a silent success, and the row says so.
    writeback     TEXT NOT NULL DEFAULT 'skipped',
    -- ...and the same fact as STRUCTURE, because `writeback` above is a RENDERING and a
    -- rendering cannot be parsed back (Session 5's lesson, at a new column). The retrieval
    -- path has to distinguish "DataHub's index has not caught up with a write that landed"
    -- from "the write never landed", and those differ ONLY by what Attest's own record says
    -- it did. Recovering that from a substring match on the sentence above would be Attest
    -- inferring its own audit trail. See retrieval.ReadState.
    --   writeback_ok = NULL  no write was attempted (the decision published nothing)
    --   writeback_ok = 1     all three writes landed
    --   writeback_ok = 0     writeback_step names which one did not
    writeback_ok   INTEGER,
    writeback_step TEXT
);

-- The write-ahead settlement intent (Session 22). This is what makes an approval's
-- three-step catalog write crash-recoverable.
--
-- One row is written HERE, with settled=0, BEFORE the first DataHub mutation of an
-- approval. The settled record, the decision rows, and this row's flip to settled=1 all
-- commit in ONE transaction (`settle`), AFTER the idempotent catalog writes. So a process
-- death anywhere in between leaves settled=0, and a startup scan (`unsettled_intents`)
-- replays the whole thing: the catalog writes are idempotent, the store transaction is
-- atomic, and recovery is therefore re-entrant. It carries HUMAN AUTHORIZATION forward — a
-- decision a person already made and the checkpoint node already consumed — never one it
-- manufactured. See api/service.py.
--
-- `payload` is opaque JSON owned by the service (the settled AuditRecord, the decisions,
-- and the exact claim indices this approval published). The store persists it and does not
-- interpret it — a run's projection is the record's business, not this table's.
CREATE TABLE IF NOT EXISTS settlement_intents (
    intent_id   TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    settled     INTEGER NOT NULL DEFAULT 0,
    settled_at  TEXT,
    payload     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_verdict ON claims (verdict, claim_type);
CREATE INDEX IF NOT EXISTS idx_claims_publication ON claims (publication);
CREATE INDEX IF NOT EXISTS idx_claims_urn ON claims (target_urn);
CREATE INDEX IF NOT EXISTS idx_claims_claim_urn ON claims (claim_urn);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals (run_id, claim_index);
CREATE INDEX IF NOT EXISTS idx_intents_unsettled ON settlement_intents (settled);
"""


@dataclass(frozen=True)
class Approval:
    """One human decision on one proposed correction. An event, and it is kept as one."""

    approval_id: str
    run_id: str
    claim_index: int
    # None means this decision took no view on that question. See report.Decision.
    publish: bool | None
    accept_correction: bool | None
    reviewer: str
    note: str
    decided_at: datetime
    # The write-back, RENDERED — for a human reading the log.
    writeback: str = "skipped"
    # ...and the same write-back as STRUCTURE, for code. None means nothing was written.
    # Kept beside the rendering rather than parsed out of it: see the schema's comment.
    writeback_ok: bool | None = None
    writeback_step: str | None = None


@dataclass(frozen=True)
class SettlementIntent:
    """A durable record that a human authorized an approval, written BEFORE the catalog.

    The write-ahead half of crash-recoverable settlement (Session 22). `payload` is opaque
    JSON the service owns; the store keeps `settled` and the ids so a startup scan can find
    the ones a process death left unfinished. `settled=0` means "authorized, not yet fully
    applied"; `settled=1` is set in the SAME transaction that persists the settled record
    and its decision rows. Nothing here decides anything — it carries a decision already
    made forward across a restart. See api/service.py and store.settle.
    """

    intent_id: str
    run_id: str
    created_at: datetime
    payload: str
    settled: bool = False
    settled_at: datetime | None = None


@dataclass(frozen=True)
class WriteState:
    """What ATTEST'S OWN RECORD says happened when a claim's verdict went to the catalog.

    Half of the three-state read (retrieval.py), and the half DataHub cannot supply. The
    catalog answers *what does the catalog hold*; this answers *what did Attest do* — and
    an absent verdict means completely different things depending on this answer:

        ok=True   the write landed, so an absent verdict is the INDEX still catching up
        ok=False  the write failed at `failed_step`, so the verdict is never coming
        ok=None   nothing was written, and this record cannot say why one is absent

    Reading DataHub's silence as `ok=False` — the shape a reader falls into when it has
    only the catalog — is Attest committing its own cardinal sin in its own read path.
    """

    claim_urn: str
    run_id: str
    claim_index: int
    target_urn: str
    verdict: str
    published: bool
    reviewer: str
    ok: bool | None = None
    failed_step: str | None = None
    # When the latest write-back attempt was recorded. None if there has never been one.
    # A "lag" that is days old is not lag, and a reader is entitled to work that out.
    at: datetime | None = None


@dataclass(frozen=True)
class ClaimHit:
    """A row from a cross-run query. What "every contradicted ownership claim" returns."""

    run_id: str
    created_at: datetime
    claim_index: int
    claim_type: str
    target_urn: str
    verdict: str
    reason: str
    source_agent: str


class StoreError(RuntimeError):
    """This database cannot hold what Attest now records. Said plainly, at open time."""


# Every column a CURRENT record needs that an older database does not have, OLDEST FIRST,
# each with the session that introduced it and what it holds.
#
# It is a TABLE rather than one hand-written `if` because the `if` version was WRONG: it
# checked `claims.rejected` (Session 5) and nothing else, so it returned "compatible" for
# every database written between Sessions 5 and 14 — which then died on the first INSERT
# with "table claims has no column named publication", from inside a running service, on
# exactly the path the check exists to protect. CLAUDE.md said Session 15's schema change
# was refused at open; it was not, because the guard was never extended when the schema
# moved under it. A guard that only knows about the first change it was written for is a
# green light wired to nothing.
#
# ADD A ROW HERE WHENEVER THE SCHEMA GAINS A COLUMN. That is the whole maintenance rule.
_REQUIRED_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "claims",
        "rejected",
        "5",
        "the guard's rejected drafts, its faithfulness violations, the models a step "
        "called, and structured conflicts",
    ),
    (
        "claims",
        "publication",
        "15",
        "whether a human cleared each verdict for the catalog, separately from whether "
        "they accepted a correction",
    ),
    (
        "approvals",
        "publish",
        "15",
        "the two halves of a decision — publish, and accept-correction — which used to be "
        "one flag",
    ),
    (
        "claims",
        "claim_urn",
        "16",
        "the claim artifact's URN in DataHub: the join key between what Attest did and "
        "what the catalog holds",
    ),
    (
        "approvals",
        "writeback_ok",
        "16",
        "whether each catalog write LANDED, and at which step it did not, as structure "
        "rather than as a rendered sentence",
    ),
    (
        "claims",
        "snapshot_id",
        "21",
        "the identity of the catalog snapshot each verdict was decided against, so an "
        "inherited artifact can name the ground truth it was checked on",
    ),
    (
        "settlement_intents",
        "settled",
        "22",
        "the write-ahead settlement intent that makes an approval's catalog write "
        "crash-recoverable — a table an older database does not have at all",
    ),
)


class AuditStore:
    """Attest's audit history. One connection, guarded — the API calls this from a pool."""

    def __init__(self, path: str | Path = "attest.db") -> None:
        self.path = str(path)
        # check_same_thread=False because FastAPI runs sync endpoints in a threadpool, so
        # the connection is legitimately touched from several threads. The lock is what
        # makes that safe: sqlite3 serializes writes anyway, and an audit store is not a
        # throughput problem — a run takes seconds and writes once.
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._refuse_an_incompatible_database()
        self._db.executescript(SCHEMA)
        self._db.commit()

    def _refuse_an_incompatible_database(self) -> None:
        """An older database is rejected HERE, not discovered mid-INSERT.

        `CREATE TABLE IF NOT EXISTS` is a no-op against an existing table, so an older
        database would open cleanly, serve reads, and then fail on the first write with
        "table claims has no column named ..." — from inside a running service, on the one
        path that matters. The columns cannot be back-filled either: several changed from a
        rendered string to the structure that produced it, and reconstructing the structure
        from the string means Attest inventing the contents of its own audit trail. So it
        says so instead. See the module docstring.

        Every schema change is checked, not just the first one — see `_REQUIRED_COLUMNS`,
        and the bug that table exists because of.
        """
        tables = {
            row["name"]
            for row in self._db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "claims" not in tables:
            return  # a new database. It gets the current schema below.

        columns: dict[str, set[str]] = {
            table: {
                row["name"]
                for row in self._db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for table in tables
        }

        for table, column, session, holds in _REQUIRED_COLUMNS:
            # A table the old database never had is as missing as a column it never had.
            if column in columns.get(table, set()):
                continue
            raise StoreError(
                f"{self.path} was written by a pre-Session-{session} Attest and cannot hold "
                f"what a run now records: {holds} (missing {table}.{column}).\n"
                f"\n"
                f"  WHAT TO DO:  delete {self.path} (and attest-checkpoints.db) and re-run.\n"
                f"\n"
                f"THERE IS NO MIGRATION, and that is deliberate rather than unfinished. "
                f"Columns here changed from a rendered string to the structure that produced "
                f"it, and a string cannot be parsed back into what it came from. "
                f"Reconstructing them would mean Attest inventing the contents of its own "
                f"audit trail — the one thing it exists not to do. Deleting the file costs "
                f"you the audit HISTORY of a development database; nothing in DataHub is "
                f"touched, and the next run rebuilds the schema. A production deployment "
                f"would need a real migration; this is a hackathon build and it says so "
                f"rather than pretending otherwise."
            )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> AuditStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:
            yield self._db

    # --- writing --------------------------------------------------------------

    def save(self, record: AuditRecord) -> None:
        """Persist a run, replacing any earlier version of it.

        Idempotent by design: a run is saved when it is created and saved again when its
        proposals are reviewed, and the second save must not double the claims. Deleting
        the run's rows and rewriting them inside one transaction is the portable way to say
        that — no ON CONFLICT, no upsert dialect.

        `approvals` is deliberately NOT touched. It is the append-only decision log and it
        outlives any particular projection of the run.
        """
        with self._write() as db:
            self._save_within(db, record)

    def _save_within(self, db: sqlite3.Connection, record: AuditRecord) -> None:
        """The body of `save`, on an already-open transaction.

        Extracted so `settle` (Session 22) can persist the settled record, its decision
        rows, and the intent flip in ONE transaction — the atomicity that makes settlement
        recovery re-entrant without ever double-appending a decision. `save` wraps this in
        its own `_write`; `settle` calls it alongside the other two writes in a single one.
        """
        for table in ("claims", "evidence", "corrections", "claim_errors", "steps"):
            db.execute(f"DELETE FROM {table} WHERE run_id = ?", (record.run_id,))
        db.execute("DELETE FROM runs WHERE run_id = ?", (record.run_id,))

        r = record.receipts
        db.execute(
            "INSERT INTO runs (run_id, created_at, status, source_agent, source_text,"
            " latency_ms, input_tokens, output_tokens, usd, n_steps, trajectory_ok,"
            " trajectory_summary, rules_checked, catalog_lookups, catalog_fetches,"
            " catalog_entities, dropped, injection_findings)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.run_id,
                record.created_at.isoformat(),
                record.status.value,
                record.source_agent,
                record.source_text,
                r.latency_ms,
                r.input_tokens,
                r.output_tokens,
                r.usd,  # None stays NULL: unknown, not free
                r.steps,
                int(r.trajectory_ok),
                r.trajectory_summary,
                json.dumps(list(r.rules_checked)),
                r.catalog_lookups,
                r.catalog_fetches,
                r.catalog_entities,
                json.dumps([d.model_dump(mode="json") for d in record.dropped]),
                json.dumps(
                    [f.model_dump(mode="json") for f in record.injection_findings]
                ),
            ),
        )

        for claim in record.claims:
            db.execute(
                "INSERT INTO claims (run_id, claim_index, claim_type, target_urn,"
                " raw_text, claim_json, verdict, reason, snapshot_id, explanation,"
                " explanation_source, faithful, violations, conflicts, rejected,"
                " publication, published_by, claim_urn)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    claim.index,
                    claim.claim_type,
                    claim.target_urn,
                    claim.raw_text,
                    json.dumps(claim.claim),
                    claim.verdict,
                    claim.reason,
                    claim.snapshot_id,
                    claim.explanation,
                    claim.explanation_source,
                    int(claim.faithful),
                    json.dumps(
                        [v.model_dump(mode="json") for v in claim.faithfulness_violations]
                    ),
                    json.dumps([c.model_dump(mode="json") for c in claim.conflicts]),
                    json.dumps(list(claim.rejected)),
                    claim.publication.status.value,
                    claim.publication.reviewer,
                    # DERIVED from the claim, by the same function the write-back uses,
                    # so the store and the catalog cannot disagree about where a claim
                    # lives. Computed for EVERY claim, published or not: a claim's
                    # artifact URN is a fact about the claim, not about the decision.
                    writeback.claim_urn(claim.claim),
                ),
            )
            for seq, e in enumerate(claim.evidence):
                db.execute(
                    "INSERT INTO evidence (run_id, claim_index, seq, field, value_json,"
                    " note) VALUES (?,?,?,?,?,?)",
                    (
                        record.run_id,
                        claim.index,
                        seq,
                        e.field,
                        json.dumps(e.value),
                        e.note,
                    ),
                )
            c = claim.correction
            db.execute(
                "INSERT INTO corrections (run_id, claim_index, outcome, review,"
                " proposal_json, attempts) VALUES (?,?,?,?,?,?)",
                (
                    record.run_id,
                    claim.index,
                    c.outcome.value,
                    c.review.value,
                    json.dumps(c.proposal) if c.proposal is not None else None,
                    json.dumps([a.model_dump(mode="json") for a in c.attempts]),
                ),
            )

        for err in record.errors:
            db.execute(
                "INSERT INTO claim_errors (run_id, claim_index, target_urn, claim_json,"
                " error) VALUES (?,?,?,?,?)",
                (
                    record.run_id,
                    err.index,
                    err.target_urn,
                    json.dumps(err.claim),
                    err.error,
                ),
            )

        for step in record.steps:
            db.execute(
                "INSERT INTO steps (run_id, seq, name, kind, claim_index, latency_ms,"
                " input_tokens, output_tokens, cost_usd, models, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    step.seq,
                    step.name,
                    step.kind,
                    step.claim_index,
                    step.latency_ms,
                    step.input_tokens,
                    step.output_tokens,
                    step.cost_usd,
                    json.dumps(list(step.models)),
                    step.error,
                ),
            )

    def record_decision(
        self,
        run_id: str,
        decision: Decision,
        writeback: str = "skipped",
        writeback_ok: bool | None = None,
        writeback_step: str | None = None,
        decided_at: datetime | None = None,
    ) -> Approval:
        """Append one human decision. Never overwrites an earlier one on the same claim.

        `writeback` is the RENDERING a human reads in the log; `writeback_ok` and
        `writeback_step` are the same fact as structure, for code. Both, because a rendering
        cannot be parsed back and the retrieval path needs the fact rather than the sentence
        — see the schema comment, and `WriteState`.
        """
        approval = Approval(
            approval_id=str(uuid.uuid4()),
            run_id=run_id,
            claim_index=decision.claim_index,
            publish=decision.publish,
            accept_correction=decision.accept_correction,
            reviewer=decision.reviewer,
            note=decision.note,
            decided_at=decided_at or datetime.now(tz=UTC),
            writeback=writeback,
            writeback_ok=writeback_ok,
            writeback_step=writeback_step,
        )
        with self._write() as db:
            self._insert_approval_within(db, approval)
        return approval

    @staticmethod
    def _insert_approval_within(db: sqlite3.Connection, approval: Approval) -> None:
        """Append one approval row on an already-open transaction.

        Extracted so `settle` (Session 22) can write the decision rows in the SAME
        transaction as the settled record and the intent flip. The approvals table stays
        append-only; this only ever inserts.
        """
        db.execute(
            "INSERT INTO approvals (approval_id, run_id, claim_index, publish,"
            " accept_correction, reviewer, note, decided_at, writeback, writeback_ok,"
            " writeback_step)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                approval.approval_id,
                approval.run_id,
                approval.claim_index,
                None if approval.publish is None else int(approval.publish),
                None
                if approval.accept_correction is None
                else int(approval.accept_correction),
                approval.reviewer,
                approval.note,
                approval.decided_at.isoformat(),
                approval.writeback,
                None if approval.writeback_ok is None else int(approval.writeback_ok),
                approval.writeback_step,
            ),
        )

    # --- crash-recoverable settlement (Session 22) ----------------------------

    def record_intent(self, run_id: str, payload: str) -> SettlementIntent:
        """Persist a write-ahead settlement intent, unsettled, and return it.

        Written BEFORE the first DataHub mutation of an approval (api/service.py). `payload`
        is the service's opaque JSON — the settled record, the decisions, and which claims
        this approval published — everything a replay needs and nothing the store reads.
        """
        intent = SettlementIntent(
            intent_id=str(uuid.uuid4()),
            run_id=run_id,
            created_at=datetime.now(tz=UTC),
            payload=payload,
            settled=False,
        )
        with self._write() as db:
            db.execute(
                "INSERT INTO settlement_intents (intent_id, run_id, created_at, settled,"
                " settled_at, payload) VALUES (?,?,?,?,?,?)",
                (
                    intent.intent_id,
                    intent.run_id,
                    intent.created_at.isoformat(),
                    0,
                    None,
                    intent.payload,
                ),
            )
        return intent

    def unsettled_intents(self) -> tuple[SettlementIntent, ...]:
        """Every settlement intent a process death left unfinished, OLDEST FIRST.

        These are the ones a startup scan replays. Oldest first so recovery settles them in
        the order they were authorized, which is also the order they would have committed.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM settlement_intents WHERE settled = 0"
                " ORDER BY created_at, intent_id"
            ).fetchall()
        return tuple(
            SettlementIntent(
                intent_id=r["intent_id"],
                run_id=r["run_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                payload=r["payload"],
                settled=bool(r["settled"]),
                settled_at=(
                    datetime.fromisoformat(r["settled_at"])
                    if r["settled_at"] is not None
                    else None
                ),
            )
            for r in rows
        )

    def settle(
        self,
        record: AuditRecord,
        approvals: Sequence[Approval],
        intent_id: str,
        settled_at: datetime | None = None,
    ) -> None:
        """Persist the settled record, its decision rows, and the intent flip — ATOMICALLY.

        This is the one transaction that makes settlement recovery re-entrant. The catalog
        writes happened before it and are idempotent; this commits the settled projection,
        appends the append-only decision rows, and flips the intent to settled=1, all or
        nothing. So a crash before this leaves settled=0 (the next scan replays), and a
        crash cannot leave the record saved while the intent still looks unsettled — which
        is what would make a replay double-append the decision rows.
        """
        when = (settled_at or datetime.now(tz=UTC)).isoformat()
        with self._write() as db:
            self._save_within(db, record)
            for approval in approvals:
                self._insert_approval_within(db, approval)
            db.execute(
                "UPDATE settlement_intents SET settled = 1, settled_at = ?"
                " WHERE intent_id = ?",
                (when, intent_id),
            )

    # --- reading --------------------------------------------------------------

    def load(self, run_id: str) -> AuditRecord | None:
        """The stored run, rebuilt whole. None if there is no such run."""
        with self._lock:
            run = self._db.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None

            claims = self._db.execute(
                "SELECT * FROM claims WHERE run_id = ? ORDER BY claim_index", (run_id,)
            ).fetchall()
            evidence = self._db.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY claim_index, seq",
                (run_id,),
            ).fetchall()
            corrections = self._db.execute(
                "SELECT * FROM corrections WHERE run_id = ?", (run_id,)
            ).fetchall()
            errors = self._db.execute(
                "SELECT * FROM claim_errors WHERE run_id = ? ORDER BY claim_index", (run_id,)
            ).fetchall()
            steps = self._db.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()

        by_claim: dict[int, list[sqlite3.Row]] = {}
        for row in evidence:
            by_claim.setdefault(row["claim_index"], []).append(row)
        corrected = {row["claim_index"]: row for row in corrections}

        return AuditRecord(
            run_id=run["run_id"],
            created_at=datetime.fromisoformat(run["created_at"]),
            status=RunStatus(run["status"]),
            source_agent=run["source_agent"],
            source_text=run["source_text"],
            claims=tuple(
                ClaimRecord(
                    index=c["claim_index"],
                    claim_type=c["claim_type"],
                    target_urn=c["target_urn"],
                    raw_text=c["raw_text"],
                    claim=json.loads(c["claim_json"]),
                    verdict=c["verdict"],
                    reason=c["reason"],
                    evidence=tuple(
                        EvidenceView(
                            field=e["field"],
                            value=json.loads(e["value_json"]),
                            note=e["note"],
                        )
                        for e in by_claim.get(c["claim_index"], [])
                    ),
                    snapshot_id=c["snapshot_id"],
                    explanation=c["explanation"],
                    explanation_source=c["explanation_source"],
                    faithful=bool(c["faithful"]),
                    faithfulness_violations=tuple(
                        ViolationView(**v) for v in json.loads(c["violations"])
                    ),
                    conflicts=tuple(
                        ConflictView(**k) for k in json.loads(c["conflicts"])
                    ),
                    rejected=tuple(json.loads(c["rejected"])),
                    correction=_correction(corrected.get(c["claim_index"])),
                    publication=PublicationView(
                        status=PublicationStatus(c["publication"]),
                        reviewer=c["published_by"],
                    ),
                )
                for c in claims
            ),
            errors=tuple(
                ClaimErrorRecord(
                    index=e["claim_index"],
                    target_urn=e["target_urn"],
                    claim=json.loads(e["claim_json"]),
                    error=e["error"],
                )
                for e in errors
            ),
            receipts=Receipts(
                latency_ms=run["latency_ms"],
                input_tokens=run["input_tokens"],
                output_tokens=run["output_tokens"],
                usd=run["usd"],
                steps=run["n_steps"],
                trajectory_ok=bool(run["trajectory_ok"]),
                trajectory_summary=run["trajectory_summary"],
                rules_checked=tuple(json.loads(run["rules_checked"])),
                catalog_lookups=run["catalog_lookups"],
                catalog_fetches=run["catalog_fetches"],
                catalog_entities=run["catalog_entities"],
            ),
            steps=tuple(
                StepView(
                    seq=s["seq"],
                    name=s["name"],
                    kind=s["kind"],
                    claim_index=s["claim_index"],
                    latency_ms=s["latency_ms"],
                    input_tokens=s["input_tokens"],
                    output_tokens=s["output_tokens"],
                    cost_usd=s["cost_usd"],
                    models=tuple(json.loads(s["models"])),
                    error=s["error"],
                )
                for s in steps
            ),
            dropped=tuple(DroppedView(**d) for d in json.loads(run["dropped"])),
            injection_findings=tuple(
                FindingView(**f) for f in json.loads(run["injection_findings"])
            ),
        )

    def approvals(self, run_id: str) -> tuple[Approval, ...]:
        """Every decision made on this run, oldest first. Nothing is overwritten."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM approvals WHERE run_id = ? ORDER BY decided_at, approval_id",
                (run_id,),
            ).fetchall()
        return tuple(
            Approval(
                approval_id=r["approval_id"],
                run_id=r["run_id"],
                claim_index=r["claim_index"],
                publish=None if r["publish"] is None else bool(r["publish"]),
                accept_correction=(
                    None
                    if r["accept_correction"] is None
                    else bool(r["accept_correction"])
                ),
                reviewer=r["reviewer"],
                note=r["note"],
                decided_at=datetime.fromisoformat(r["decided_at"]),
                writeback=r["writeback"],
                writeback_ok=(
                    None if r["writeback_ok"] is None else bool(r["writeback_ok"])
                ),
                writeback_step=r["writeback_step"],
            )
            for r in rows
        )

    def write_states(
        self, claim_urns: Sequence[str] | None = None
    ) -> dict[str, WriteState]:
        """What Attest did about each claim artifact, keyed by the artifact's URN.

        The store's half of the three-state read (retrieval.py). DataHub says what the
        catalog HOLDS; this says what Attest DID, and an absent verdict cannot be read
        without both.

        **Keyed by claim URN, and therefore NOT by run.** A claim artifact is
        content-addressed, so the same claim audited twice is ONE artifact written by two
        runs — that is the point of the addressing, not an edge case. The catalog holds one
        thing at that URN, so this reports the LATEST attempt against it: an older run's
        success does not describe the state of a newer run's failed rewrite.

        `claim_urns=None` returns every claim this store knows about. Passing the URNs a
        DataHub read actually returned keeps it to that page.
        """
        sql = (
            "SELECT c.claim_urn, c.run_id, c.claim_index, c.target_urn, c.verdict,"
            " c.publication, c.published_by, a.writeback_ok, a.writeback_step,"
            " a.decided_at"
            " FROM claims c"
            " LEFT JOIN approvals a"
            "   ON a.run_id = c.run_id AND a.claim_index = c.claim_index"
            "   AND a.writeback_ok IS NOT NULL"
            " WHERE c.claim_urn != ''"
        )
        args: list[Any] = []
        if claim_urns is not None:
            urns = list(claim_urns)
            if not urns:
                return {}
            sql += f" AND c.claim_urn IN ({','.join('?' * len(urns))})"
            args.extend(urns)
        # Newest attempt first, so the first row seen per URN is the one that describes the
        # artifact now. A claim with no write-back attempt at all still yields a row (LEFT
        # JOIN, decided_at NULL) and sorts last — it is a real answer: "nothing was written".
        sql += " ORDER BY a.decided_at IS NULL, a.decided_at DESC"

        with self._lock:
            rows = self._db.execute(sql, args).fetchall()

        states: dict[str, WriteState] = {}
        for r in rows:
            if r["claim_urn"] in states:
                continue
            states[r["claim_urn"]] = WriteState(
                claim_urn=r["claim_urn"],
                run_id=r["run_id"],
                claim_index=r["claim_index"],
                target_urn=r["target_urn"],
                verdict=r["verdict"],
                published=r["publication"] == PublicationStatus.PUBLISHED.value,
                reviewer=r["published_by"],
                ok=None if r["writeback_ok"] is None else bool(r["writeback_ok"]),
                failed_step=r["writeback_step"],
                at=(
                    datetime.fromisoformat(r["decided_at"])
                    if r["decided_at"] is not None
                    else None
                ),
            )
        return states

    def find_claims(
        self,
        verdict: str | None = None,
        claim_type: str | None = None,
        target_urn: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> tuple[ClaimHit, ...]:
        """The query the history exists to answer.

        "Show me every contradicted ownership claim this week" is one call, filtered in the
        index rather than parsed out of a blob. This is what a last-write-wins property on
        a dataset cannot do, and the reason Attest keeps its own store.
        """
        where: list[str] = []
        args: list[Any] = []
        if verdict:
            where.append("c.verdict = ?")
            args.append(verdict)
        if claim_type:
            where.append("c.claim_type = ?")
            args.append(claim_type)
        if target_urn:
            where.append("c.target_urn = ?")
            args.append(target_urn)
        if since:
            where.append("r.created_at >= ?")
            args.append(since.isoformat())

        sql = (
            "SELECT c.run_id, c.claim_index, c.claim_type, c.target_urn, c.verdict,"
            " c.reason, r.created_at, r.source_agent"
            " FROM claims c JOIN runs r ON r.run_id = c.run_id"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY r.created_at DESC, c.claim_index LIMIT ?"
        args.append(limit)

        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return tuple(
            ClaimHit(
                run_id=r["run_id"],
                created_at=datetime.fromisoformat(r["created_at"]),
                claim_index=r["claim_index"],
                claim_type=r["claim_type"],
                target_urn=r["target_urn"],
                verdict=r["verdict"],
                reason=r["reason"],
                source_agent=r["source_agent"],
            )
            for r in rows
        )


def _correction(row: sqlite3.Row | None) -> CorrectionView:
    """Rebuild a correction. A claim with no correction row was never in the loop."""
    if row is None:
        return CorrectionView(outcome=CorrectionOutcome.NOT_ATTEMPTED)
    return CorrectionView(
        outcome=CorrectionOutcome(row["outcome"]),
        review=ReviewStatus(row["review"]),
        proposal=json.loads(row["proposal_json"]) if row["proposal_json"] else None,
        attempts=tuple(
            AttemptView(**a) for a in json.loads(row["attempts"])
        ),
    )
