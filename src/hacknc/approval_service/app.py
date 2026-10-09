"""HTTP API of the approval service.

    POST /requests                 file a request          (EHR gateway, on an agent's behalf)
    POST /requests/{id}/approve    approve                 (physician, in the hospital app)
    POST /requests/{id}/reject     reject                  (physician, in the hospital app)
    POST /requests/{id}/consume    act on a signature      (disclosure gateway; content must match)
    POST /break-glass              emergency access grant  (clinician, if enabled)
    GET  /break-glass/grants       active grants
    GET/PUT /admin/flags           demo switches           (X-Demo-Admin-Key header)
                                   (see hacknc.common.flags; turning break-the-glass off
                                    revokes active grants)
    DELETE /admin/requests         reset the demo: empty queue, no grants

Every decision is made by OPA (``hospital/approval/decision``) and every outcome, allowed or
denied, is written to the audit log.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from opentelemetry import trace

from hacknc.approval_service.models import (
    ApprovalRequest,
    BreakGlassGrant,
    BreakGlassRequest,
    Decision,
    Fulfilment,
    NewRequest,
)
from hacknc.approval_service.store import ApprovalStore
from hacknc.common.audit import AuditClient, AuditEvent, actor_fields
from hacknc.common.auth import TokenClaims
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.fingerprint import fingerprint
from hacknc.common.flags import DEFAULT_FLAGS
from hacknc.common.policy import OpaClient, PolicyDecision, subject_input
from hacknc.common.telemetry import FASTAPI_TELEMETRY

POLICY = "hospital/approval/decision"
tracer = trace.get_tracer(__name__)


@dataclass
class ApprovalSettings:
    approval_ttl_seconds: int = 600
    break_glass_ttl_seconds: int = 900
    demo_admin_key: str = ""
    flags: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_FLAGS))

    @property
    def break_glass_enabled(self) -> bool:
        return self.flags.get("break_glass_enabled", False)


def create_app(
    store: ApprovalStore,
    auth: BearerAuth,
    opa: OpaClient,
    audit: AuditClient,
    settings: ApprovalSettings,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    app = FastAPI(title="Approval service", docs_url=None, redoc_url=None,
                  telemetry=FASTAPI_TELEMETRY)
    app.state.store = store
    # Handlers run on a thread pool. Read-check-write of a request's status happens under this
    # lock, so a signature is used at most once even if two submissions arrive together.
    transition = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def decide(claims: TokenClaims, action: str, **extra: Any) -> PolicyDecision:
        with tracer.start_as_current_span(f"policy {action}") as span:
            decision = opa.decide(POLICY, {"action": action, "subject": subject_input(claims),
                                           **extra})
            span.set_attribute("policy.decision", "allow" if decision.allow else "deny")
            span.set_attribute("policy.reason", decision.reason)
            return decision

    def record(claims: TokenClaims, action: str, decision: PolicyDecision | None, *,
               request: ApprovalRequest | None = None, severity: str | None = None,
               agent_chain: list[str] | None = None, patient_id: str | None = None,
               reason: str | None = None, **details: Any) -> None:
        allowed = decision is None or decision.allow
        audit.record(AuditEvent(
            action=action,
            decision="allow" if allowed else "deny",
            severity=severity or ("info" if allowed else "warning"),
            **actor_fields(claims),
            agent_chain=agent_chain if agent_chain is not None else [claims.authorized_party],
            resource=f"approval/{request.id}" if request else None,
            patient_id=patient_id or (request.patient_id if request else None),
            reason=reason if reason is not None else (decision.reason if decision else ""),
            policy=POLICY if decision else None,
            details=details,
        ))

    def denied(decision: PolicyDecision) -> JSONResponse:
        return JSONResponse({"allow": False, "reason": decision.reason}, status_code=403)

    def load(request_id: str) -> ApprovalRequest:
        request = store.get(request_id)
        if request is None:
            raise HTTPException(404, detail="no such approval request")
        return request.effective(clock())

    def request_input(request: ApprovalRequest) -> dict[str, Any]:
        return {
            "request": {"status": request.status, "expired": request.status == "expired"},
            "resource": f"approval/{request.id}",
            "patient_id": request.patient_id, "kind": request.kind,
        }

    def check_admin(key: str | None) -> None:
        if not settings.demo_admin_key or not secrets.compare_digest(
                key or "", settings.demo_admin_key):
            raise HTTPException(401, detail="demo admin key required")

    # ------------------------------------------------------------------ routes

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/requests", status_code=201, response_model=None)
    def create_request(body: NewRequest, claims: TokenClaims = Depends(auth)):
        decision = decide(claims, "create", agent_chain=body.agent_chain,
                          patient_id=body.patient_id, kind=body.kind)
        if not decision.allow:
            record(claims, "approval.request", decision, agent_chain=body.agent_chain,
                   patient_id=body.patient_id)
            return denied(decision)
        now = clock()
        request = ApprovalRequest(
            **body.model_dump(),
            id=uuid.uuid4().hex[:8],
            fingerprint=body.content_fingerprint(),
            requested_for=claims.username,
            requested_by=claims.authorized_party,
            created_at=now,
            expires_at=now + settings.approval_ttl_seconds,
        )
        store.save(request)
        trace.get_current_span().set_attribute("patient.name", body.patient_name)
        record(claims, "approval.request", decision, request=request,
               agent_chain=body.agent_chain,
               reason=f"{body.title}: awaiting physician signature",
               fingerprint=request.fingerprint, agent_chain_reported_by=claims.authorized_party)
        return request

    def decide_request(request_id: str, body: Decision, claims: TokenClaims,
                       new_status: str):
        with transition:
            return _decide_request(request_id, body, claims, new_status)

    def _decide_request(request_id: str, body: Decision, claims: TokenClaims,
                        new_status: str):
        request = load(request_id)
        action = "approval.approve" if new_status == "approved" else "approval.reject"
        decision = decide(claims, "decide", **request_input(request))
        if not decision.allow:
            record(claims, action, decision, request=request)
            return denied(decision)
        now = clock()
        request = request.model_copy(update={
            "status": new_status,
            "decided_by": claims.username,
            "decided_at": now,
            "decision_note": body.note,
            # An approval must be used soon; the clock restarts at approval time.
            "expires_at": now + settings.approval_ttl_seconds,
        })
        store.save(request)
        record(claims, action, decision, request=request,
               reason=f"{claims.username} {new_status} the request"
                      + (f": {body.note}" if body.note else ""))
        return request

    @app.post("/requests/{request_id}/approve", response_model=None)
    def approve(request_id: str, body: Decision | None = None,
                claims: TokenClaims = Depends(auth)):
        return decide_request(request_id, body or Decision(), claims, "approved")

    @app.post("/requests/{request_id}/reject", response_model=None)
    def reject(request_id: str, body: Decision | None = None,
               claims: TokenClaims = Depends(auth)):
        return decide_request(request_id, body or Decision(), claims, "rejected")

    @app.post("/requests/{request_id}/consume", response_model=None)
    def consume(request_id: str, submitted: Fulfilment, claims: TokenClaims = Depends(auth)):
        with transition:
            return _consume(request_id, submitted, claims)

    def _consume(request_id: str, submitted: Fulfilment, claims: TokenClaims):
        request = load(request_id)
        matches = fingerprint(submitted.content) == request.fingerprint
        bound = submitted.patient_id == request.patient_id and submitted.kind == request.kind
        decision = decide(claims, "consume", hash_matches=matches,
                          submitted={"patient_id": submitted.patient_id, "kind": submitted.kind},
                          **request_input(request))
        if not decision.allow:
            tampered = not (matches and bound) and request.status == "approved"
            if tampered:
                # The approval is burned: a request that was tampered with can't be retried.
                store.save(request.model_copy(update={"status": "invalidated",
                                                      "tampered_submission": submitted.content}))
            record(claims, "approval.tamper_detected" if tampered else "approval.consume",
                   decision, request=request, agent_chain=request.agent_chain,
                   severity="high" if tampered else "warning",
                   approved=request.content, submitted=submitted.content)
            return denied(decision)
        request = request.model_copy(update={"status": "fulfilled", "fulfilled_at": clock()})
        store.save(request)
        record(claims, "rx.fulfilled", decision, request=request, agent_chain=request.agent_chain,
               reason=f"signed by {request.decided_by}; content matches the signature")
        return request

    @app.post("/break-glass", status_code=201, response_model=None)
    def break_glass(body: BreakGlassRequest, claims: TokenClaims = Depends(auth)):
        decision = decide(claims, "break_glass", patient_id=body.patient_id,
                          resource=f"Patient/{body.patient_id}",
                          break_glass={"enabled": settings.break_glass_enabled,
                                       "justification": body.justification})
        if not decision.allow:
            record(claims, "break_glass.request", decision, patient_id=body.patient_id)
            return denied(decision)
        now = clock()
        actor = actor_fields(claims)
        grant = BreakGlassGrant(id=uuid.uuid4().hex[:8], user=claims.username,
                                role=actor["actor_role"], patient_id=body.patient_id,
                                justification=body.justification, created_at=now,
                                expires_at=now + settings.break_glass_ttl_seconds)
        store.add_grant(grant)
        # Allowed, but loud: every emergency access is reviewed afterwards.
        record(claims, "break_glass.granted", decision, patient_id=body.patient_id,
               severity="high", reason=f"emergency access: {body.justification}",
               grant_id=grant.id, expires_at=grant.expires_at)
        return grant

    @app.get("/break-glass/grants")
    def grants(patient_id: str | None = None, claims: TokenClaims = Depends(auth)) -> dict:
        return {"grants": store.active_grants(clock(), patient_id=patient_id)}

    @app.get("/admin/flags")
    def get_flags(x_demo_admin_key: str | None = Header(default=None)) -> dict[str, bool]:
        check_admin(x_demo_admin_key)
        return dict(settings.flags)

    @app.put("/admin/flags")
    def set_flags(change: dict[str, bool],
                  x_demo_admin_key: str | None = Header(default=None)) -> dict[str, bool]:
        """Merge known flags; unknown keys are ignored."""
        check_admin(x_demo_admin_key)
        settings.flags.update({k: v for k, v in change.items() if k in DEFAULT_FLAGS})
        if change.get("break_glass_enabled") is False:
            store.revoke_all_grants()
        return dict(settings.flags)

    @app.delete("/admin/requests", status_code=204)
    def reset(x_demo_admin_key: str | None = Header(default=None)) -> None:
        check_admin(x_demo_admin_key)
        store.clear()

    return app
