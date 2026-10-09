"""Turn OPA decision logs into audit events.

OPA pushes a (gzipped) JSON array of every decision it made. Recording them gives a second,
independent witness: the service says what it *did*, OPA says what it *decided*.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from hacknc.common.audit import HOSPITAL_ROLES, AuditEvent

RELEVANT_PREFIX = "hospital/"


def parse_body(body: bytes, content_encoding: str | None) -> list[dict[str, Any]]:
    if (content_encoding or "").lower() == "gzip":
        body = gzip.decompress(body)
    data = json.loads(body or b"[]")
    return data if isinstance(data, list) else [data]


def to_event(log: dict[str, Any]) -> AuditEvent | None:
    """One OPA decision -> one audit event. Ignores non-hospital policies (e.g. health checks)."""
    path = log.get("path", "")
    result = log.get("result")
    if not path.startswith(RELEVANT_PREFIX) or not isinstance(result, dict):
        return None
    input_doc = log.get("input") or {}
    subject = input_doc.get("subject") or {}
    allowed = bool(result.get("allow"))
    roles = subject.get("roles") or []
    return AuditEvent(
        action=f"policy.{input_doc.get('action', 'decision')}",
        decision="allow" if allowed else "deny",
        severity="info" if allowed else "warning",
        actor_user=subject.get("user"),
        actor_role=next((r for r in HOSPITAL_ROLES if r in roles), None),
        agent_chain=input_doc.get("agent_chain") or [c for c in [subject.get("client")] if c],
        resource=input_doc.get("resource"),
        patient_id=input_doc.get("patient_id"),
        reason=str(result.get("reason", "")),
        policy=path,
        details={"opa_decision_id": log.get("decision_id")},
        trace_id=log.get("trace_id"),
    )
