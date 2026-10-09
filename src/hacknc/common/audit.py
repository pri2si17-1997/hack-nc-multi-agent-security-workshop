"""Write events to the audit service.

Every security-relevant action is recorded as an ``AuditEvent``: who (the human), through
which agents, did what, to which resource/patient, and whether it was allowed and why.
The audit service stores events in a hash chain, so later edits are detectable.

Writing is **best effort**: if the audit service is down, the action still proceeds and a
warning is logged and attached to the trace. A stricter hospital might refuse to act without
an audit record; that trade-off is a good discussion point for the workshop.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import httpx
from opentelemetry import trace
from pydantic import BaseModel, Field

from hacknc.common.auth.errors import AuthError
from hacknc.common.auth.models import TokenClaims
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.telemetry import format_trace_id

log = logging.getLogger(__name__)

Decision = Literal["allow", "deny", "info"]
Severity = Literal["info", "warning", "high"]
HOSPITAL_ROLES = ("physician", "nurse", "admin", "patient")


class AuditEvent(BaseModel):
    """What a service reports. The audit service adds sequence number, time, writer and hashes."""

    action: str = Field(description="dot.separated verb, e.g. 'approval.approve'")
    decision: Decision = "info"
    severity: Severity = "info"
    actor_user: str | None = None
    actor_role: str | None = None
    agent_chain: list[str] = Field(default_factory=list)
    resource: str | None = None
    patient_id: str | None = None
    reason: str = ""
    policy: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None


def actor_fields(claims: TokenClaims | None) -> dict[str, str | None]:
    """``actor_user`` / ``actor_role`` taken from a validated token."""
    if claims is None:
        return {"actor_user": None, "actor_role": None}
    role = next((r for r in HOSPITAL_ROLES if r in claims.realm_roles), None)
    return {"actor_user": claims.username or None, "actor_role": role}


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format_trace_id(context.trace_id) if context.is_valid else None


class AuditClient:
    """Posts events to ``<audit-service>/events`` using this service's own token."""

    def __init__(
        self,
        base_url: str,
        tokens: ServiceTokenProvider,
        http: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._tokens = tokens
        self._http = http or httpx.Client(timeout=5.0)

    def record(self, event: AuditEvent) -> bool:
        """Send one event. Returns False (and logs) instead of raising if it could not be stored."""
        if event.trace_id is None:
            event = event.model_copy(update={"trace_id": current_trace_id()})
        try:
            response = self._http.post(
                f"{self.base_url}/events",
                json=event.model_dump(),
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
            )
            response.raise_for_status()
            return True
        except (httpx.HTTPError, AuthError) as exc:
            log.warning("audit event %s not recorded: %s", event.action, exc)
            trace.get_current_span().add_event("audit.write_failed",
                                               {"audit.action": event.action, "error": str(exc)})
            return False
