"""SQLite storage for approval requests and break-the-glass grants (JSON per row)."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from hacknc.approval_service.models import ApprovalRequest, BreakGlassGrant

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, created_at REAL, data TEXT)",
    "CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, created_at REAL, data TEXT)",
)


class ApprovalStore:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        for statement in SCHEMA:
            self._db.execute(statement)
        self._lock = threading.Lock()

    def save(self, request: ApprovalRequest) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO requests VALUES (?, ?, ?)",
                             (request.id, request.created_at, request.model_dump_json()))
            self._db.commit()

    def get(self, request_id: str) -> ApprovalRequest | None:
        row = self._db.execute("SELECT data FROM requests WHERE id = ?", (request_id,)).fetchone()
        return ApprovalRequest.model_validate_json(row[0]) if row else None

    def recent(self, limit: int = 50) -> list[ApprovalRequest]:
        rows = self._db.execute("SELECT data FROM requests ORDER BY created_at DESC LIMIT ?",
                                (limit,))
        return [ApprovalRequest.model_validate_json(r[0]) for r in rows]

    def add_grant(self, grant: BreakGlassGrant) -> None:
        with self._lock:
            self._db.execute("INSERT INTO grants VALUES (?, ?, ?)",
                             (grant.id, grant.created_at, grant.model_dump_json()))
            self._db.commit()

    def clear(self) -> None:
        """Demo reset: no requests, no grants."""
        with self._lock:
            self._db.execute("DELETE FROM requests")
            self._db.execute("DELETE FROM grants")
            self._db.commit()

    def revoke_all_grants(self) -> int:
        """End every emergency grant now (used when break-the-glass is switched off)."""
        with self._lock:
            count = self._db.execute("DELETE FROM grants").rowcount
            self._db.commit()
        return count

    def active_grants(self, now: float, patient_id: str | None = None,
                      user: str | None = None) -> list[BreakGlassGrant]:
        rows = self._db.execute("SELECT data FROM grants ORDER BY created_at DESC")
        grants = [BreakGlassGrant.model_validate_json(r[0]) for r in rows]
        return [g for g in grants if g.expires_at > now
                and (patient_id is None or g.patient_id == patient_id)
                and (user is None or g.user == user)]
