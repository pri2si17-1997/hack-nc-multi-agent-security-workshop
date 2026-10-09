"""SQLite storage for the audit chain. Appends are serialized so the chain never forks."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hacknc.audit_service.chain import GENESIS_HASH, VerifyResult, compute_hash, verify
from hacknc.common.audit import AuditEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq          INTEGER PRIMARY KEY,
    event_id     TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    writer       TEXT NOT NULL,
    action       TEXT NOT NULL,
    decision     TEXT NOT NULL,
    severity     TEXT NOT NULL,
    actor_user   TEXT,
    actor_role   TEXT,
    agent_chain  TEXT NOT NULL,
    resource     TEXT,
    patient_id   TEXT,
    reason       TEXT NOT NULL,
    policy       TEXT,
    details      TEXT NOT NULL,
    trace_id     TEXT,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
)
"""
JSON_COLUMNS = ("agent_chain", "details")


class AuditStore:
    """Append-only audit log in a single SQLite file (``:memory:`` for tests)."""

    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(SCHEMA)
        self._lock = threading.Lock()

    def append(self, event: AuditEvent, writer: str) -> dict[str, Any]:
        """Add an event as the next link in the chain and return the stored entry."""
        with self._lock:
            last = self._db.execute(
                "SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            entry: dict[str, Any] = {
                **event.model_dump(),
                "seq": (last["seq"] + 1) if last else 1,
                "event_id": str(uuid.uuid4()),
                "recorded_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "writer": writer,
                "prev_hash": last["hash"] if last else GENESIS_HASH,
            }
            entry["hash"] = compute_hash(entry)
            row = {k: json.dumps(v) if k in JSON_COLUMNS else v for k, v in entry.items()}
            columns = ", ".join(row)
            self._db.execute(f"INSERT INTO entries ({columns}) VALUES "
                             f"({', '.join('?' for _ in row)})", list(row.values()))
            self._db.commit()
            return entry

    def recent(self, limit: int = 100, after: int | None = None, before: int | None = None,
               **filters: str | None) -> list[dict[str, Any]]:
        """Newest first. Filters (exact match): decision, severity, writer, trace_id, actor_user,
        action, patient_id. ``after`` / ``before``: only entries with a sequence number above /
        below it (to read a period, or page backwards through a long chain)."""
        allowed = ("decision", "severity", "writer", "trace_id", "actor_user", "action",
                   "patient_id")
        active = {k: v for k, v in filters.items() if k in allowed and v}
        clauses = [f"{k} = ?" for k in active]
        values: list[Any] = list(active.values())
        for clause, bound in (("seq > ?", after), ("seq < ?", before)):
            if bound is not None:
                clauses.append(clause)
                values.append(bound)
        where = " AND ".join(clauses) or "1=1"
        rows = self._db.execute(
            f"SELECT * FROM entries WHERE {where} ORDER BY seq DESC LIMIT ?", [*values, limit])
        return [_decode(r) for r in rows]

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def iter_in_order(self) -> Iterator[dict[str, Any]]:
        for row in self._db.execute("SELECT * FROM entries ORDER BY seq ASC"):
            yield _decode(row)

    def verify(self) -> VerifyResult:
        with self._lock:
            return verify(self.iter_in_order())

    def raw_connection(self) -> sqlite3.Connection:
        """For the tamper demo only: direct access that bypasses the chain."""
        return self._db


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    entry = dict(row)
    for column in JSON_COLUMNS:
        entry[column] = json.loads(entry[column])
    return entry
