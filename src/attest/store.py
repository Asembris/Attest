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
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from attest.record import (
    AttemptView,
    AuditRecord,
    ClaimErrorRecord,
    ClaimRecord,
    CorrectionView,
    EvidenceView,
    Receipts,
    StepView,
)
from attest.report import CorrectionOutcome, Decision, ReviewStatus, RunStatus

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
    explanation         TEXT NOT NULL DEFAULT '',
    explanation_source  TEXT NOT NULL DEFAULT 'template',
    faithful            INTEGER NOT NULL DEFAULT 1,
    conflicts           TEXT NOT NULL DEFAULT '[]',
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
    cost_usd      REAL,
    PRIMARY KEY (run_id, seq)
);

-- APPEND-ONLY. A decision is an event; re-deciding writes a new row. See the module docstring.
CREATE TABLE IF NOT EXISTS approvals (
    approval_id   TEXT PRIMARY KEY,
    run_id        TEXT NOT NULL,
    claim_index   INTEGER NOT NULL,
    accept        INTEGER NOT NULL,
    reviewer      TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    decided_at    TEXT NOT NULL,
    -- What happened when the accepted verdict was written back to DataHub: 'written',
    -- 'skipped', or the failure. An approval whose write-back failed is not a silent
    -- success, and the row says which it was.
    writeback     TEXT NOT NULL DEFAULT 'skipped'
);

CREATE INDEX IF NOT EXISTS idx_claims_verdict ON claims (verdict, claim_type);
CREATE INDEX IF NOT EXISTS idx_claims_urn ON claims (target_urn);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs (created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_run ON approvals (run_id, claim_index);
"""


@dataclass(frozen=True)
class Approval:
    """One human decision on one proposed correction. An event, and it is kept as one."""

    approval_id: str
    run_id: str
    claim_index: int
    accept: bool
    reviewer: str
    note: str
    decided_at: datetime
    writeback: str = "skipped"


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
        self._db.executescript(SCHEMA)
        self._db.commit()

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
                    json.dumps(list(record.dropped)),
                    json.dumps(list(record.injection_findings)),
                ),
            )

            for claim in record.claims:
                db.execute(
                    "INSERT INTO claims (run_id, claim_index, claim_type, target_urn,"
                    " raw_text, claim_json, verdict, reason, explanation,"
                    " explanation_source, faithful, conflicts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        record.run_id,
                        claim.index,
                        claim.claim_type,
                        claim.target_urn,
                        claim.raw_text,
                        json.dumps(claim.claim),
                        claim.verdict,
                        claim.reason,
                        claim.explanation,
                        claim.explanation_source,
                        int(claim.faithful),
                        json.dumps(list(claim.conflicts)),
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
                    " input_tokens, output_tokens, cost_usd) VALUES (?,?,?,?,?,?,?,?,?)",
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
                    ),
                )

    def record_decision(
        self,
        run_id: str,
        decision: Decision,
        writeback: str = "skipped",
        decided_at: datetime | None = None,
    ) -> Approval:
        """Append one human decision. Never overwrites an earlier one on the same claim."""
        approval = Approval(
            approval_id=str(uuid.uuid4()),
            run_id=run_id,
            claim_index=decision.claim_index,
            accept=decision.accept,
            reviewer=decision.reviewer,
            note=decision.note,
            decided_at=decided_at or datetime.now(tz=UTC),
            writeback=writeback,
        )
        with self._write() as db:
            db.execute(
                "INSERT INTO approvals (approval_id, run_id, claim_index, accept, reviewer,"
                " note, decided_at, writeback) VALUES (?,?,?,?,?,?,?,?)",
                (
                    approval.approval_id,
                    approval.run_id,
                    approval.claim_index,
                    int(approval.accept),
                    approval.reviewer,
                    approval.note,
                    approval.decided_at.isoformat(),
                    approval.writeback,
                ),
            )
        return approval

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
                    explanation=c["explanation"],
                    explanation_source=c["explanation_source"],
                    faithful=bool(c["faithful"]),
                    conflicts=tuple(json.loads(c["conflicts"])),
                    correction=_correction(corrected.get(c["claim_index"])),
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
                )
                for s in steps
            ),
            dropped=tuple(json.loads(run["dropped"])),
            injection_findings=tuple(json.loads(run["injection_findings"])),
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
                accept=bool(r["accept"]),
                reviewer=r["reviewer"],
                note=r["note"],
                decided_at=datetime.fromisoformat(r["decided_at"]),
                writeback=r["writeback"],
            )
            for r in rows
        )

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
