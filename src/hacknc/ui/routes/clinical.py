"""The clinical app (physician and nurse): one ward screen.

    GET  /ward                               ward list · chat with the coordinator · activity
    GET  /ward/{id}/chart                    the chart drawer (HTML fragment), as the EHR
                                             gateway shows it to *this* user
    GET  /api/ward                           census + discharge status (refresh)
    GET  /api/discharges/{id}                discharge state (tasks, signature cards)
    POST /api/chat                           run: a chat message to the coordinator
    POST /api/discharges/{id}/reopen         run: start over; unsigned drafts are cancelled
    POST /api/signatures/{sid}               run: sign / reject a draft, then continue
    POST /api/discharges/{id}/alerts/{aid}/acknowledge
                                             run: review a security alert; the task is retried
    POST /api/discharges/{id}/alerts/{aid}/manual
                                             run: while IT fixes the agent, do its task by hand
    GET  /api/disclosures/{id}               what left the hospital (from the audit trail)
    GET  /api/access-log/{id}                every access to this patient (audit trail)
    POST /api/break-glass/{id}               emergency access to withheld records (if the admin
                                             enabled it; justification required; loud)
    GET  /api/requests/{id}                  what the patient asked for in the portal
    POST /api/requests/{id}/{rid}            run: accept into the discharge plan, or decline

Nurses get the same screen. Asking for a discharge or signing is refused by the server
(Keycloak scopes + OPA), and the refusal shows up in the chat and on the map.
"""

from __future__ import annotations

import html
from typing import Any, Literal

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from hacknc.agents.runtime import ToolDenied
from hacknc.common.auth import AuthError
from hacknc.ui import presenters
from hacknc.ui.audit_views import access_rows, disclosure_rows
from hacknc.ui.context import UiContext
from hacknc.ui.routes.backends import (
    GatewayUnavailable,
    audit_events,
    coordinator,
    ehr,
    exchanged,
)
from hacknc.ui.routes.runs import start_run
from hacknc.ui.sessions import UserSession

CLINICAL = presenters.CLINICAL


class SignBody(BaseModel):
    patient_id: str
    decision: Literal["approve", "reject"] = "approve"


class ReopenBody(BaseModel):
    reason: str = ""


class ChatBody(BaseModel):
    patient_id: str
    message: str


class ManualBody(BaseModel):
    note: str = ""


class BreakGlassBody(BaseModel):
    justification: str


class RequestDecision(BaseModel):
    accept: bool
    note: str = ""


def register(app: FastAPI, ctx: UiContext) -> None:
    deps = ctx.deps

    async def census(session: UserSession) -> list[dict[str, Any]]:
        [result] = await ehr(ctx, session, [("search_patients", {"query": ""})])
        status, statuses = await coordinator(ctx, session, "GET", "/discharges")
        statuses = statuses if status == 200 else {}
        patients = [p for p in result["patients"] if p.get("admission")]
        for patient in patients:
            state = statuses.get(patient["id"], "not_started")
            if patient.get("discharged"):
                state = "completed"
            patient["discharge"] = {"state": state,
                                    "label": presenters.STATUS_LABELS.get(state, state)}
        return patients

    # ------------------------------------------------------------------ pages

    @app.get("/ward")
    async def ward_page(request: Request, patient: str | None = None) -> Response:
        session = ctx.page_for_role(request, *CLINICAL)
        if not isinstance(session, UserSession):
            return session
        try:
            patients, error = await census(session), None
        except (AuthError, ToolDenied, GatewayUnavailable) as exc:
            patients, error = [], str(exc)
        selected = next((p for p in patients if p["id"] == patient), patients[0] if patients
                        else None)
        return ctx.render(request, "ward.html", session, patients=patients, error=error,
                          selected=selected)

    @app.get("/patients")
    def old_patients_page() -> Response:
        return RedirectResponse("/ward", status_code=303)

    @app.get("/patients/{patient_id}")
    def old_workspace_page(patient_id: str) -> Response:
        return RedirectResponse(f"/ward?patient={patient_id}", status_code=303)

    @app.get("/ward/{patient_id}/chart")
    async def chart_drawer(request: Request, patient_id: str) -> Response:
        session = ctx.page_for_role(request, *CLINICAL)
        if not isinstance(session, UserSession):
            return session
        args = {"patient_id": patient_id}
        try:
            summary, meds, allergies, observations, notes, messages = await ehr(ctx, session, [
                ("get_patient_summary", args), ("get_medications", args),
                ("get_allergies", args), ("get_observations", args),
                ("get_clinical_notes", args), ("get_portal_messages", args)])
        except (GatewayUnavailable, ToolDenied, AuthError) as exc:
            # The message can echo the patient id from the URL: escape it (no raw HTML).
            return Response(f'<p class="deny-text">✕ {html.escape(str(exc))}</p>',
                            status_code=503, media_type="text/html")
        withheld = [w for part in (summary, meds, allergies, observations, notes, messages)
                    for w in part.get("withheld", [])]
        flags = await anyio.to_thread.run_sync(
            lambda: deps.approvals.get_flags(deps.settings.demo_admin_key))
        can_break_glass = bool(withheld) and flags.ok and \
            flags.body.get("break_glass_enabled", False)
        return ctx.render(request, "chart_drawer.html", session, patient=summary["patient"],
                          can_break_glass=can_break_glass,
                          admission=summary.get("admission"), summary=summary, meds=meds,
                          allergies=allergies, observations=observations, notes=notes,
                          messages=messages, withheld=withheld, labels_of=presenters.labels_of)

    # ------------------------------------------------------------------ API

    @app.get("/api/ward")
    async def ward(request: Request) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        try:
            return {"ok": True, "patients": await census(session)}
        except (AuthError, ToolDenied, GatewayUnavailable) as exc:
            return {"ok": False, "reason": str(exc), "patients": []}

    @app.get("/api/discharges/{patient_id}")
    async def discharge(request: Request, patient_id: str) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        status, body = await coordinator(ctx, session, "GET", f"/discharges/{patient_id}")
        return body if status == 200 else {"error": body.get("detail")}

    @app.post("/api/chat")
    async def chat(request: Request, body: ChatBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)

        async def work() -> dict[str, Any]:
            status, result = await coordinator(ctx, session, "POST", "/chat",
                                               {"patient_id": body.patient_id,
                                                "message": body.message})
            if status != 200:
                return {"ok": False, "reason": result.get("detail", f"HTTP {status}")}
            return {"ok": True, "reply": result["reply"], "by": result.get("by", ""),
                    "checklist": result.get("discharge")}

        return start_run(ctx, session, "chat", body.message, body.patient_id, work)

    @app.post("/api/discharges/{patient_id}/reopen")
    async def reopen(request: Request, patient_id: str, body: ReopenBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)

        async def work() -> dict[str, Any]:
            status, checklist = await coordinator(ctx, session, "POST",
                                                  f"/discharges/{patient_id}/reopen",
                                                  {"reason": body.reason})
            if status != 200:
                return {"ok": False, "reason": checklist.get("detail", f"HTTP {status}")}
            cancelled = checklist.pop("cancelled_signatures", [])
            if cancelled:
                # The physician's own authority cancels the drafts nobody signed.
                token = await exchanged(ctx, session, "approval-service", ["rx:approve"])
                for signature_id in cancelled:
                    await anyio.to_thread.run_sync(
                        lambda sid=signature_id: deps.approvals.decide(token, sid, False))
            return {"ok": True, "checklist": checklist}

        return start_run(ctx, session, "reopen", "Reopen discharge", patient_id, work)

    @app.post("/api/discharges/{patient_id}/alerts/{alert_id}/acknowledge")
    async def acknowledge(request: Request, patient_id: str, alert_id: str) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)

        async def work() -> dict[str, Any]:
            status, body = await coordinator(
                ctx, session, "POST", f"/discharges/{patient_id}/alerts/{alert_id}/acknowledge")
            if status != 200:
                return {"ok": False, "reason": body.get("detail", f"HTTP {status}")}
            return {"ok": True, "checklist": body}

        return start_run(ctx, session, "acknowledge", "Review security alert", patient_id, work)

    @app.post("/api/discharges/{patient_id}/alerts/{alert_id}/manual")
    async def handle_manually(request: Request, patient_id: str, alert_id: str,
                              body: ManualBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)

        async def work() -> dict[str, Any]:
            status, result = await coordinator(
                ctx, session, "POST", f"/discharges/{patient_id}/alerts/{alert_id}/manual",
                {"note": body.note})
            if status != 200:
                return {"ok": False, "reason": result.get("detail", f"HTTP {status}")}
            return {"ok": True, "checklist": result}

        return start_run(ctx, session, "manual", "Take over by hand", patient_id, work)

    @app.post("/api/signatures/{signature_id}")
    async def sign(request: Request, signature_id: str, body: SignBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        verb = "Sign" if body.decision == "approve" else "Reject"

        async def work() -> dict[str, Any]:
            try:
                token = await exchanged(ctx, session, "approval-service", ["rx:approve"])
            except AuthError as exc:
                return {"ok": False, "reason": str(exc)}
            result = await anyio.to_thread.run_sync(lambda: deps.approvals.decide(
                token, signature_id, body.decision == "approve"))
            if not result.ok:
                return {"ok": False, "reason": result.reason}
            decision = "approved" if body.decision == "approve" else "rejected"
            status, checklist = await coordinator(
                ctx, session, "POST", f"/discharges/{body.patient_id}/signed",
                {"signature_id": signature_id, "decision": decision})
            if status != 200:
                return {"ok": False, "reason": checklist.get("detail", f"HTTP {status}")}
            return {"ok": True, "checklist": checklist}

        return start_run(ctx, session, "sign", f"{verb} draft", body.patient_id, work)

    @app.get("/api/disclosures/{patient_id}")
    async def disclosures(request: Request, patient_id: str) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        rows = []
        for action in ("disclosure.sent", "disclosure.blocked"):
            result = await audit_events(ctx, session, limit=100, action=action,
                                        patient_id=patient_id)
            if not result["ok"]:
                return {"ok": False, "reason": result["reason"], "disclosures": []}
            rows += disclosure_rows(result.get("entries", []))
        return {"ok": True, "disclosures": sorted(rows, key=lambda r: r["seq"])}

    @app.get("/api/requests/{patient_id}")
    async def patient_requests(request: Request, patient_id: str) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        try:
            [result] = await ehr(ctx, session, [("get_patient_requests",
                                                 {"patient_id": patient_id})])
        except (AuthError, ToolDenied, GatewayUnavailable) as exc:
            return {"ok": False, "reason": str(exc), "requests": []}
        return {"ok": True, "requests": result["requests"]}

    @app.post("/api/requests/{patient_id}/{request_id}")
    async def decide_request(request: Request, patient_id: str, request_id: str,
                             body: RequestDecision) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)

        async def work() -> dict[str, Any]:
            status, result = await coordinator(
                ctx, session, "POST", f"/portal/requests/{patient_id}/{request_id}/decide",
                {"accept": body.accept, "note": body.note})
            if status != 200:
                return {"ok": False, "reason": result.get("detail", f"HTTP {status}")}
            return {"ok": True, **result}

        verb = "Accept" if body.accept else "Decline"
        return start_run(ctx, session, "decide_request", f"{verb} patient request", patient_id,
                         work)

    @app.post("/api/break-glass/{patient_id}")
    async def break_glass(request: Request, patient_id: str,
                          body: BreakGlassBody) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        try:
            # No extra scope: the approval policy checks the human's role and that the request
            # comes from the hospital app (never from an agent).
            token = await exchanged(ctx, session, "approval-service", [])
        except AuthError as exc:
            return {"ok": False, "reason": str(exc)}
        result = await anyio.to_thread.run_sync(lambda: deps.approvals.break_glass(
            token, patient_id, body.justification.strip()))
        if not result.ok:
            return {"ok": False, "reason": result.reason}
        return {"ok": True, "grant": result.body}

    @app.get("/api/access-log/{patient_id}")
    async def access_log(request: Request, patient_id: str) -> dict[str, Any]:
        session = ctx.api_for_role(request, *CLINICAL)
        result = await audit_events(ctx, session, limit=200, patient_id=patient_id)
        if not result["ok"]:
            return {"ok": False, "reason": result["reason"], "entries": []}
        return {"ok": True, "entries": access_rows(result.get("entries", []))}
