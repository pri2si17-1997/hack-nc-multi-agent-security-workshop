"""HTTP API of the audit service.

    POST /events      append one event          (scope audit:write; writer = token's client)
    POST /opa/logs    OPA decision-log upload   (static shared token, see infra/opa/config.yaml)
    GET  /events      newest entries            (scope audit:read; filters, after/before seq)
    GET  /verify      walk the hash chain       (scope audit:read)
    GET  /healthz

The writer of an entry is taken from the *token* (``azp``), never from the request body,
so a service cannot write events in another service's name.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from opentelemetry import trace

from hacknc.audit_service import opa_logs
from hacknc.audit_service.store import AuditStore
from hacknc.common.audit import AuditEvent
from hacknc.common.auth import TokenClaims
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.telemetry import FASTAPI_TELEMETRY

# Records on this logger are exported to Loki (see __main__), feeding the Grafana dashboard.
audit_log = logging.getLogger("hacknc.audit")


def create_app(store: AuditStore, auth: BearerAuth, opa_log_token: str = "") -> FastAPI:
    """``opa_log_token`` is the shared secret OPA presents; empty disables log upload."""
    app = FastAPI(title="Audit service", docs_url=None, redoc_url=None,
                  telemetry=FASTAPI_TELEMETRY)
    app.state.store = store

    def require_scope(claims: TokenClaims, scope: str) -> None:
        if scope not in claims.scopes:
            raise HTTPException(403, detail=f"token lacks the {scope} scope")

    def store_and_publish(event: AuditEvent, writer: str) -> dict[str, Any]:
        entry = store.append(event, writer)
        if writer != "opa":  # OPA uploads batches later; they would mislabel the span
            trace.get_current_span().set_attributes({
                "audit.seq": entry["seq"], "audit.action": entry["action"],
                "audit.decision": entry["decision"], "audit.writer": writer,
                "audit.hash": entry["hash"]})
        audit_log.info(
            "%s %s %s", entry["action"], entry["decision"], entry["reason"],
            extra={
                "audit_seq": entry["seq"],
                "action": entry["action"],
                "decision": entry["decision"],
                "severity": entry["severity"],
                "writer": entry["writer"],
                "actor_user": entry["actor_user"] or "",
                "actor_role": entry["actor_role"] or "",
                "agent_chain": " > ".join(entry["agent_chain"]),
                "patient_id": entry["patient_id"] or "",
                "policy": entry["policy"] or "",
                "audit_trace_id": entry["trace_id"] or "",
            },
        )
        return entry

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", status_code=201)
    def append_event(event: AuditEvent, claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require_scope(claims, "audit:write")
        entry = store_and_publish(event, claims.authorized_party)
        return {"seq": entry["seq"], "hash": entry["hash"]}

    @app.post("/opa/logs", status_code=204)
    async def append_opa_logs(request: Request) -> None:
        presented = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not opa_log_token or not secrets.compare_digest(presented, opa_log_token):
            raise HTTPException(401, detail="OPA log token required")
        logs = opa_logs.parse_body(await request.body(), request.headers.get("content-encoding"))
        for log in logs:
            event = opa_logs.to_event(log)
            if event is not None:
                store_and_publish(event, "opa")

    @app.get("/events")
    def list_events(
        claims: TokenClaims = Depends(auth),
        limit: int = Query(100, ge=1, le=500),
        decision: str | None = None,
        severity: str | None = None,
        writer: str | None = None,
        trace_id: str | None = None,
        action: str | None = None,
        patient_id: str | None = None,
        after: int | None = Query(None, ge=0),
        before: int | None = Query(None, ge=1),
    ) -> dict[str, Any]:
        require_scope(claims, "audit:read")
        entries = store.recent(limit, after=after, before=before, decision=decision,
                               severity=severity, writer=writer, trace_id=trace_id,
                               action=action, patient_id=patient_id)
        return {"entries": entries, "total": store.count()}

    @app.get("/verify")
    def verify_chain(claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require_scope(claims, "audit:read")
        result = store.verify()
        audit_log.log(
            logging.INFO if result.ok else logging.ERROR,
            "audit chain %s", "intact" if result.ok else f"BROKEN: {result.problem}",
            extra={"action": "audit.verify", "chain_ok": result.ok,
                   "severity": "info" if result.ok else "high"},
        )
        return asdict(result)

    return app
