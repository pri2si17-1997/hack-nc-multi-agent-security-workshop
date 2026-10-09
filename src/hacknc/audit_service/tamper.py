"""DEMO ONLY: play a rogue admin who edits the audit database directly.

    make tamper-audit                 # flip the most recent DENY to ALLOW ("cover it up")
    make tamper-audit ARGS="--seq 3 --field reason --value 'nothing to see'"
    make tamper-audit ARGS="--delete 3"
    make cover-up                     # finale: make the gateway's latest blocked call look normal

Then press "Verify chain" in the UI (or reload the incident report): the broken link is
reported at the exact entry.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3

EDITABLE = ("decision", "reason", "actor_user", "action", "patient_id", "severity")


def cover_up(db: sqlite3.Connection) -> str:
    """Rewrite the EHR gateway's most recent refused call as a routine, allowed read: the
    insider erases the blocked attempt. Only the row is edited; its hash is left as it was."""
    row = db.execute("SELECT seq, details FROM entries WHERE writer = 'ehr-mcp' AND "
                     "decision = 'deny' ORDER BY seq DESC LIMIT 1").fetchone()
    if row is None:
        raise SystemExit("no blocked gateway call yet: run the finale's discharge first "
                         "(scenario Wrong patient)")
    seq, details = row
    tool = json.loads(details).get("tool", "a tool")
    db.execute("UPDATE entries SET decision = 'allow', severity = 'info', reason = ? "
               "WHERE seq = ?", (f"{tool}: 1 returned", seq))
    db.commit()
    return (f"entry #{seq}: the blocked {tool} call now reads as an allowed read (hash left "
            "unchanged). Now reload the incident report.")


def edit(db: sqlite3.Connection, seq: int | None, field: str, value: str) -> str:
    if field not in EDITABLE:
        raise SystemExit(f"--field must be one of {', '.join(EDITABLE)}")
    if seq is None:
        row = db.execute("SELECT seq FROM entries WHERE decision = 'deny' "
                         "ORDER BY seq DESC LIMIT 1").fetchone()
        if row is None:
            raise SystemExit("no DENY entries yet: e.g. log in as nurse.gawain and ask to "
                             "discharge a patient first")
        seq = row[0]
    db.execute(f"UPDATE entries SET {field} = ? WHERE seq = ?", (value, seq))
    db.commit()
    return (f"entry #{seq}: set {field} = {value!r} (hash left unchanged). "
            "Now press 'Verify chain' in the UI.")


def delete(db: sqlite3.Connection, seq: int) -> str:
    db.execute("DELETE FROM entries WHERE seq = ?", (seq,))
    db.commit()
    return f"deleted entry #{seq}. Now press 'Verify chain' in the UI."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("AUDIT_DB_PATH", "/data/audit.db"))
    parser.add_argument("--seq", type=int, help="entry to edit (default: latest deny)")
    parser.add_argument("--field", choices=EDITABLE, default="decision")
    parser.add_argument("--value", default="allow")
    parser.add_argument("--delete", type=int, metavar="SEQ", help="delete this entry instead")
    parser.add_argument("--cover-up", action="store_true",
                        help="make the EHR gateway's latest blocked call look allowed")
    args = parser.parse_args(argv)

    db = sqlite3.connect(args.db)
    if args.cover_up:
        print(cover_up(db))
    elif args.delete:
        print(delete(db, args.delete))
    else:
        print(edit(db, args.seq, args.field, args.value))


if __name__ == "__main__":
    main()
