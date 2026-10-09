"""The patient portal (patient role only): the patient's own record, nothing else.

    GET  /portal                 my medicines, my requests, my sharing choice, a message box
    GET  /api/portal             the same, as JSON (refresh)
    POST /api/portal/messages    send a message: the portal agent files it for the care team
    POST /api/portal/consent     may my psychiatry records go to my family doctor?

Every call uses the patient's own token (``portal:self``, which Keycloak grants only to
patients). The EHR gateway resolves which record the portal account is linked to, and OPA
refuses any other patient.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from hacknc.agents.runtime import ToolDenied
from hacknc.common.auth import AuthError
from hacknc.ui import presenters
from hacknc.ui.context import UiContext
from hacknc.ui.routes.backends import GatewayUnavailable, coordinator, ehr
from hacknc.ui.sessions import UserSession

PORTAL = ("portal:self",)


class MessageBody(BaseModel):
    message: str


class ConsentBody(BaseModel):
    share_sensitive_with_pcp: bool


def register(app: FastAPI, ctx: UiContext) -> None:
    async def my_record(session: UserSession) -> dict[str, Any]:
        [me] = await ehr(ctx, session, [("get_my_record", {})], PORTAL)
        pid = me["patient"]["id"]
        meds, requests = await ehr(ctx, session, [("get_medications", {"patient_id": pid}),
                                                  ("get_patient_requests", {"patient_id": pid})],
                                   PORTAL)
        home = [m for m in meds["medications"] if m.get("category") != "inpatient"]
        return {"patient": me["patient"], "consent": me["consent"], "pharmacy": me["pharmacy"],
                "medications": home, "requests": list(reversed(requests["requests"])),
                "suggestions": presenters.portal_suggestions(home)}

    @app.get("/portal")
    async def portal_page(request: Request) -> Response:
        session = ctx.page_for_role(request, "patient")
        if not isinstance(session, UserSession):
            return session
        try:
            data, error = await my_record(session), None
        except (AuthError, ToolDenied, GatewayUnavailable) as exc:
            data, error = None, str(exc)
        return ctx.render(request, "portal.html", session, data=data, error=error)

    @app.get("/api/portal")
    async def portal_data(request: Request) -> dict[str, Any]:
        session = ctx.api_for_role(request, "patient")
        try:
            return {"ok": True, **await my_record(session)}
        except (AuthError, ToolDenied, GatewayUnavailable) as exc:
            return {"ok": False, "reason": str(exc)}

    @app.post("/api/portal/messages")
    async def send_message(request: Request, body: MessageBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, "patient")
        text = body.message.strip()
        if not text:
            raise HTTPException(422, detail="empty message")
        [me] = await ehr(ctx, session, [("get_my_record", {})], PORTAL)
        status, result = await coordinator(ctx, session, "POST", "/portal/messages",
                                           {"patient_id": me["patient"]["id"],
                                            "message": text[:2000]})
        if status != 200:
            return {"ok": False, "reply": result.get("detail", f"HTTP {status}")}
        return result

    @app.post("/api/portal/consent")
    async def consent(request: Request, body: ConsentBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, "patient")
        [me] = await ehr(ctx, session, [("get_my_record", {})], PORTAL)
        [saved] = await ehr(ctx, session, [("update_consent", {
            "patient_id": me["patient"]["id"],
            "share_sensitive_with_pcp": body.share_sensitive_with_pcp})], PORTAL)
        return {"ok": True, "consent": saved["consent"]}
