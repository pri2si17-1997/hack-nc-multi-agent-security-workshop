"""The hash chain: how entries are fingerprinted and how tampering is detected.

    hash(entry N) = SHA-256( canonical JSON of entry N, including hash(entry N-1) )

Change one character in entry 5 and its recomputed hash no longer matches the stored one.
Delete entry 5 and entry 6's ``prev_hash`` points at nothing. Either way ``verify`` says where.

Limitation (worth saying out loud): deleting the *newest* entries leaves a shorter but valid
chain. Real systems periodically publish the latest hash somewhere the admin can't edit
(another system, a ticket, a blockchain-style witness) to catch that too.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

GENESIS_HASH = "0" * 64

# Everything that makes up an entry, except its own hash.
HASHED_FIELDS = (
    "seq", "event_id", "recorded_at", "writer", "action", "decision", "severity",
    "actor_user", "actor_role", "agent_chain", "resource", "patient_id", "reason",
    "policy", "details", "trace_id", "prev_hash",
)


def canonical_bytes(entry: dict[str, Any]) -> bytes:
    """Stable byte representation: same content always gives the same bytes."""
    content = {field: entry.get(field) for field in HASHED_FIELDS}
    return json.dumps(content, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(entry)).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    broken_at: int | None = None
    problem: str = ""
    head_hash: str = GENESIS_HASH


def verify(entries: Iterable[dict[str, Any]]) -> VerifyResult:
    """Walk the chain from the first entry; stop at the first broken link."""
    expected_prev, expected_seq, checked = GENESIS_HASH, 1, 0
    for entry in entries:
        seq = entry["seq"]
        if seq != expected_seq:
            return VerifyResult(False, checked, expected_seq,
                                f"entry #{expected_seq} is missing (deleted?)")
        if entry["prev_hash"] != expected_prev:
            return VerifyResult(False, checked, seq,
                                f"entry #{seq} does not link to entry #{seq - 1}")
        if compute_hash(entry) != entry["hash"]:
            return VerifyResult(False, checked, seq,
                                f"entry #{seq} was modified after it was written")
        expected_prev, expected_seq, checked = entry["hash"], seq + 1, checked + 1
    return VerifyResult(True, checked, head_hash=expected_prev)
