"""Discharge coordinator HTTP API (called by the hospital app with the clinician's own token).

    GET    /discharges                       status of every discharge
    GET    /discharges/{patient}             the checklist
    POST   /discharges/{patient}/start       run the agents      (needs discharge:order: physicians)
                                             409 if already under way or completed
    POST   /discharges/{patient}/reopen      start over (not once completed)   (discharge:order)
    POST   /discharges/{patient}/signed      continue after a physician signed or rejected a draft;
                                             when everything is out, record the discharge in the EHR
    POST   /discharges/{patient}/alerts/{id}/acknowledge
                                             a physician reviewed a security alert: the suspended
                                             task is retried (discharge:order)
    POST   /discharges/{patient}/alerts/{id}/manual
                                             while IT fixes the agent, a physician takes the task
                                             over by hand, outside the agents (discharge:order)
    POST   /chat                             the coordinator: a chat message in, a reply out
                                             (answers, or orchestrates a discharge; see
                                             coordinator.py). Rules when the model is off.
    POST   /portal/messages                  a patient's portal message -> the portal agent
                                             (portal:self: patients only, their own record)
    POST   /portal/requests/{patient}/{id}/decide
                                             a physician accepts or declines a patient's request
                                             (discharge:order); accepted medicine requests join
                                             the discharge plan
    DELETE /admin/discharges                 demo reset (X-Demo-Admin-Key)
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from opentelemetry import trace
from pydantic import BaseModel

from hacknc.agents.runtime import AgentError, call_tool, delegate, mcp_session
from hacknc.common.audit import AuditClient, AuditEvent, actor_fields
from hacknc.common.auth import KeycloakClient, TokenClaims, peek_claims
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.flags import FlagReader
from hacknc.common.telemetry import FASTAPI_TELEMETRY
from hacknc.llm import LlmRunner
from hacknc.orchestrator import coordinator
from hacknc.orchestrator.discharge import AlreadyStarted, Discharge, DischargeBoard, Task
from hacknc.orchestrator.watch import AlertWatch, AssignmentError, TaskAssigner

NAME = "orchestrator"
PLANNER = "fixed discharge checklist (a workflow, not a model: same steps every time)"
COORDINATOR_TIMEOUT = 300.0   # seconds for a whole discharge: its tools are other agents
# Seconds for one specialist's tool call. Its model alone may take twice LLM_TIMEOUT_SECONDS
# (60 s by default), plus its gateway calls and the rules: the HTTP call must outlast that.
AGENT_CALL_TIMEOUT = 150.0
MODEL_PLANNER = "Strands agent + LLM: chooses the order; code enforces prerequisites"
tracer = trace.get_tracer(__name__)


class Signed(BaseModel):
    signature_id: str
    decision: Literal["approved", "rejected"] = "approved"


class Reopen(BaseModel):
    reason: str = ""


class Manual(BaseModel):
    note: str = ""


class ChatMessage(BaseModel):
    patient_id: str
    message: str


class Decision(BaseModel):
    accept: bool
    note: str = ""


@dataclass
class OrchestratorDeps:
    keycloak: KeycloakClient
    secrets: Callable[[str], str]
    agent_urls: dict[str, str]
    ehr_url: str = "http://ehr-mcp:8010/mcp"
    audit: AuditClient | None = None
    flags: FlagReader | None = None
    admin_key: str = ""
    llm: LlmRunner | None = None
    assigner: TaskAssigner | None = None   # binds each agent to its task's patient
    watch: AlertWatch | None = None        # reads the gateways' denials of each agent

    def model_on(self) -> bool:
        return self.llm is not None and self.llm.enabled()

    def agent_enabled(self, agent: str) -> bool:
        return self.flags.on(f"agent.{agent}") if self.flags else True


def create_app(deps: OrchestratorDeps, auth: BearerAuth,
               board: DischargeBoard | None = None) -> FastAPI:
    app = FastAPI(title="Discharge coordinator", docs_url=None, redoc_url=None,
                  telemetry=FASTAPI_TELEMETRY)
    board = board or DischargeBoard()

    def require(claims: TokenClaims, scope: str, what: str) -> None:
        if scope not in claims.scopes:
            raise HTTPException(403, detail=f"{claims.username} may not {what}: the token has no "
                                            f"{scope} ({SCOPE_HOLDERS.get(scope, '')})")

    def bearer(request: Request) -> str:
        return request.headers["authorization"].split(" ", 1)[1]

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/discharges")
    def statuses(claims: TokenClaims = Depends(auth)) -> dict[str, str]:
        return board.statuses()

    @app.get("/discharges/{patient_id}")
    def checklist(patient_id: str, claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        return board.get(patient_id).view()

    @app.post("/discharges/{patient_id}/start")
    async def start(patient_id: str, request: Request,
                    claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "discharge:order", "order a discharge")
        try:
            discharge = board.start(patient_id, claims.username)
        except AlreadyStarted as exc:
            raise HTTPException(409, detail=_already(exc.discharge)) from None
        enabled = [t for t in discharge.tasks if deps.agent_enabled(t.agent)]
        with _plan_span(claims, patient_id, PLANNER,
                        f"discharge checklist: {len(discharge.tasks)} tasks") as span:
            span.set_attribute("plan.agents", ",".join(dict.fromkeys(t.agent for t in enabled)))
        _audit(deps, claims, patient_id, "discharge.started",
               f"discharge ordered; {len(enabled)} agent tasks")
        await _run_checklist(deps, discharge, bearer(request))
        discharge.refresh_status()
        await _record_if_complete(deps, discharge, bearer(request))
        return discharge.view()

    @app.post("/discharges/{patient_id}/reopen")
    def reopen(patient_id: str, body: Reopen,
               claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "discharge:order", "reopen a discharge")
        try:
            cancelled = board.reopen(patient_id)
        except AlreadyStarted as exc:
            raise HTTPException(409, detail=_already(exc.discharge)) from None
        _audit(deps, claims, patient_id, "discharge.reopened",
               f"discharge reopened; {len(cancelled)} unsigned draft(s) cancelled"
               + (f"; reason: {body.reason}" if body.reason else ""))
        return {**board.get(patient_id).view(), "cancelled_signatures": cancelled}

    @app.post("/discharges/{patient_id}/signed")
    async def signed(patient_id: str, body: Signed, request: Request,
                     claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        discharge = board.get(patient_id)
        task = discharge.task_for_signature(body.signature_id)
        if task is None:
            raise HTTPException(404, detail="no draft with that signature id")
        send = await _after_signature(deps, discharge, task, body, bearer(request))
        # Did the send change what the physician signed? The signature service says so in
        # its own audit entry: alert, suspend sending, hold the discharge.
        await _contain(deps, discharge, send)
        discharge.refresh_status()
        await _record_if_complete(deps, discharge, bearer(request))
        return discharge.view()

    @app.post("/discharges/{patient_id}/alerts/{alert_id}/acknowledge")
    async def acknowledge(patient_id: str, alert_id: str, request: Request,
                          claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "discharge:order", "acknowledge a security alert")
        discharge, alert = open_alert(patient_id, alert_id)
        alert.update(status="acknowledged", by=claims.username, at=time.time())
        _audit(deps, claims, patient_id, "security.alert_acknowledged",
               f"{claims.username} reviewed: {alert['agent']} {alert['reason']}; task retried",
               details={"alert_id": alert_id, "gateway_audit_seq": alert["seq"]})
        task = discharge.task(alert["task_id"])
        token = bearer(request)
        # No status refresh before the retry: with every draft already signed, it would mark
        # the discharge completed (final) before the suspended task has run again.
        with _plan_span(claims, patient_id, PLANNER, f"retry {task.title.lower()} after review"):
            if not any(a["task_id"] == task.id for a in discharge.open_alerts()):
                if task.id in SENDS:   # it changed signed content: draft again what didn't go
                    await _redraft(deps, discharge, task, token)
                else:
                    task.status, task.summary, task.detail, task.data = "waiting", "", "", {}
                    task.signatures = [s for s in task.signatures if s["status"] != "withdrawn"]
            await _continue(deps, discharge, token)
        await _record_if_complete(deps, discharge, token)
        return discharge.view()

    @app.post("/discharges/{patient_id}/alerts/{alert_id}/manual")
    async def handle_manually(patient_id: str, alert_id: str, body: Manual, request: Request,
                              claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        """The agent stays suspended until IT fixes it; meanwhile the physician does its task
        through the hospital's usual channels (e-prescribing, phone), and says so here."""
        require(claims, "discharge:order", "take a task over by hand")
        discharge, alert = open_alert(patient_id, alert_id)
        note = body.note.strip()[:200] or "done outside the agents"
        alert.update(status="handled_manually", by=claims.username, at=time.time(), note=note)
        task = discharge.task(alert["task_id"])
        _audit(deps, claims, patient_id, "security.alert_handled_manually",
               f"{claims.username} took over {task.title.lower()} by hand ({note}); "
               f"{alert['agent']} stays suspended",
               details={"alert_id": alert_id, "gateway_audit_seq": alert["seq"], "note": note})
        if not any(a["task_id"] == task.id for a in discharge.open_alerts()):
            _take_over(discharge, task, f"Handled manually by {claims.username}: {note}")
        token = bearer(request)
        with _plan_span(claims, patient_id, PLANNER,
                        f"{task.title.lower()} handled by hand; carry on"):
            await _continue(deps, discharge, token)
        await _record_if_complete(deps, discharge, token)
        return discharge.view()

    def open_alert(patient_id: str, alert_id: str) -> tuple[Discharge, dict[str, Any]]:
        discharge = board.get(patient_id)
        alert = next((a for a in discharge.alerts if a["id"] == alert_id), None)
        if alert is None:
            raise HTTPException(404, detail="no such alert")
        if alert["status"] != "open":
            raise HTTPException(409, detail=f"already {alert['status'].replace('_', ' ')} "
                                            f"by {alert.get('by')}")
        return discharge, alert

    @app.post("/chat")
    async def chat(body: ChatMessage, request: Request,
                   claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "agent:delegate", "use the assistant")
        token, pid = bearer(request), body.patient_id
        ordered = False   # did this message order (or continue) the discharge?

        def start() -> str | None:
            nonlocal ordered
            before = board.get(pid).status
            refusal = coordinator.start_refusal(board, pid, claims, _already)
            ordered = ordered or refusal is None
            if refusal is None and before == "not_started":
                _audit(deps, claims, pid, "discharge.started", "discharge ordered in chat")
            if refusal:
                _audit(deps, claims, pid, "discharge.refused", refusal, decision="deny")
            return refusal

        async def ask(question: str) -> str:
            task = Task("chat", "Question", "chart-agent", ("patient:read",))
            if not deps.agent_enabled("chart-agent"):
                return "The chart assistant is switched off by the admin."
            result = await _call(deps, task, token, "answer_question",
                                 {"patient_id": pid, "question": question})
            return result.get("text", "") if result else f"🛑 {task.detail}"

        async def task_runner(d: Discharge, task_id: str, user_token: str,
                              notes: str = "") -> Task:
            task = await run_task(deps, d, task_id, user_token, notes)
            settle_sends(deps, d)
            d.refresh_status()
            return task

        turn = coordinator.Turn(board, pid, claims, token, task_runner, ask, start)
        reply, by = None, "⚙ rules (keywords → fixed checklist)"
        if deps.model_on():
            with _plan_span(claims, pid, MODEL_PLANNER, "the coordinator model decides"):
                role = "physician" if "physician" in claims.realm_roles else (
                    "nurse" if "nurse" in claims.realm_roles else "staff")
                outcome = await deps.llm.run(
                    agent=NAME, task="chat",
                    system=coordinator.SYSTEM.format(user=claims.username, role=role,
                                                     patient_id=pid),
                    prompt=body.message, tools=coordinator.tools_for(turn),
                    timeout=COORDINATOR_TIMEOUT)
            if outcome.used_model and outcome.text:
                reply, by = coordinator.tidy_reply(outcome.text), outcome.byline()
                if ordered:   # the model may stop early (e.g. after a suspension): finish
                    reply += await _finish_what_the_model_left(deps, board.get(pid), token)
            else:
                by = f"⚙ rules ({'; '.join(outcome.skipped) or 'no model'})"
        if reply is None:
            with _plan_span(claims, pid, PLANNER, "keywords: discharge / status / question"):
                reply = await coordinator.rules_reply(
                    turn, body.message, lambda d: _run_checklist(deps, d, token))
        discharge = board.get(pid)
        discharge.refresh_status()
        await _record_if_complete(deps, discharge, token)
        return {"reply": reply, "by": by, "discharge": discharge.view()}

    @app.post("/portal/messages")
    async def portal_message(body: ChatMessage, request: Request,
                             claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "portal:self", "use the patient portal")
        task = Task("portal", "Patient message", "portal-agent", ("portal:self",))
        if not deps.agent_enabled("portal-agent"):
            return {"ok": False, "reply": "Messaging is unavailable right now. For anything "
                                          "urgent, call the ward."}
        result = await _call(deps, task, bearer(request), "triage_message",
                             {"patient_id": body.patient_id, "message": body.message})
        if result is None:
            return {"ok": False, "reply": f"Your message could not be sent: {task.detail}"}
        return {"ok": True, **result}

    @app.post("/portal/requests/{patient_id}/{request_id}/decide")
    async def decide_request(patient_id: str, request_id: str, body: Decision,
                             request: Request,
                             claims: TokenClaims = Depends(auth)) -> dict[str, Any]:
        require(claims, "discharge:order", "decide a patient's request")
        try:
            delegation = await delegate(deps.keycloak, deps.secrets, NAME, bearer(request),
                                        "ehr-mcp", ["discharge:order"])
            async with mcp_session(deps.ehr_url, delegation.token) as ehr:
                result = await call_tool(ehr, "decide_patient_request", {
                    "patient_id": patient_id, "request_id": request_id,
                    "accept": body.accept, "note": body.note})
        except AgentError as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except BaseExceptionGroup as group:
            inner = _first_agent_error(group)
            if inner is None:
                raise
            raise HTTPException(409, detail=str(inner)) from None
        decided = result["request"]
        _audit(deps, claims, patient_id, "patient_request.decided",
               f"{request_id} ({decided['kind']}) {decided['status']}"
               + (f": {body.note}" if body.note else ""))   # a clinical decision, not a denial
        discharge = board.get(patient_id)
        hint = ""
        if decided.get("plan_item") and discharge.task("medrec").status == "done":
            hint = ("The discharge plan is updated; reopen the discharge so the agents use it "
                    "(medication reconciliation already ran).")
        elif decided.get("plan_item"):
            hint = "Added to the discharge plan: reconciliation and your signature still apply."
        elif decided["status"] == "accepted" and decided["kind"] in ("medication_change",
                                                                     "refill"):
            hint = ("Accepted, but neither the request nor the record gives a dose, so the "
                    "discharge plan is unchanged: set the dose in your own order.")
        return {"request": decided, "hint": hint}

    @app.delete("/admin/discharges", status_code=204)
    def reset(x_demo_admin_key: str | None = Header(default=None)) -> None:
        if not deps.admin_key or not secrets.compare_digest(x_demo_admin_key or "",
                                                            deps.admin_key):
            raise HTTPException(401, detail="demo admin key required")
        board.clear()

    return app


# --------------------------------------------------------------------------- running tasks


SCOPE_HOLDERS = {
    "discharge:order": "Keycloak only grants it to physicians",
    "agent:delegate": "Keycloak grants it to clinicians",
    "portal:self": "Keycloak only grants it to patients",
}


@contextmanager
def _plan_span(claims: TokenClaims, patient_id: str, reasoning: str,
               reason: str) -> Iterator[trace.Span]:
    """The coordinator's planning step, as the activity panel shows it."""
    with tracer.start_as_current_span("orchestrator plan") as span:
        span.set_attributes({"agent.id": NAME, "user.name": claims.username,
                             "patient.id": patient_id, "agent.reasoning": reasoning,
                             "plan.reason": reason})
        yield span


async def _call(deps: OrchestratorDeps, task: Task, user_token: str, tool: str,
                arguments: dict[str, Any]) -> dict[str, Any] | None:
    """Delegate one call: narrower token for the agent, then its MCP tool. None on failure."""
    task.status, task.updated_at = "running", time.time()
    assigner, patient_id = deps.assigner, arguments.get("patient_id")
    bound = assigner is not None and bool(patient_id)
    user = peek_claims(user_token).username if bound else ""
    try:
        if bound:
            # Bind the agent to this task's patient at the EHR gateway, before it gets a token.
            await anyio.to_thread.run_sync(lambda: assigner.assign(task.agent, user, patient_id))
        delegation = await delegate(deps.keycloak, deps.secrets, NAME, user_token, task.agent,
                                    task.scopes)
        async with mcp_session(deps.agent_urls[task.agent], delegation.token,
                               AGENT_CALL_TIMEOUT) as agent:
            return await call_tool(agent, tool, arguments)
    except (AgentError, AssignmentError) as exc:
        task.status, task.summary, task.detail = "failed", "Could not complete", f"🛑 {exc}"
        return None
    except BaseExceptionGroup as group:   # errors inside the MCP session arrive wrapped
        inner = _first_agent_error(group) or _first_error(group)
        task.status, task.summary, task.detail = "failed", "Could not complete", f"🛑 {inner}"
        return None
    except Exception as exc:  # noqa: BLE001 - the agent timed out or is unreachable
        # Never leave the task "running": nothing would ever run it again, and a running task
        # keeps the discharge from completing. Failed can be retried (Reopen).
        task.status, task.summary = "failed", "Could not complete"
        task.detail = f"🛑 {task.agent} did not answer: {type(exc).__name__} {exc}".strip()
        return None
    finally:
        if bound:   # the task is over: the agent no longer has any business with this patient
            await anyio.to_thread.run_sync(lambda: assigner.release(task.agent, user, patient_id))


def _first_agent_error(group: BaseExceptionGroup) -> AgentError | None:
    for exc in group.exceptions:
        if isinstance(exc, AgentError):
            return exc
        if isinstance(exc, BaseExceptionGroup) and (inner := _first_agent_error(exc)):
            return inner
    return None


def _first_error(group: BaseExceptionGroup) -> str:
    """The first leaf error, for a task failure that isn't an agent's own refusal."""
    first = group.exceptions[0]
    if isinstance(first, BaseExceptionGroup):
        return _first_error(first)
    return f"{type(first).__name__} {first}".strip()


def _finish(task: Task, status: str, summary: str, result: dict[str, Any]) -> None:
    task.status, task.summary, task.updated_at = status, summary, time.time()
    task.detail = result.get("text", "")
    task.data = {k: v for k, v in result.items() if k != "text"}


CHECKLIST_ORDER = ("medrec", "benefits", "scheduling", "prescriptions", "summary",
                   "instructions")


def _ready(deps: OrchestratorDeps, d: Discharge, task: Task) -> bool:
    if not deps.agent_enabled(task.agent):
        task.status, task.summary = "disabled", "Turned off by the admin"
        return False
    missing = [dep for dep in task.depends_on if d.task(dep).status != "done"]
    if missing:
        task.status = "blocked"
        task.summary = f"Needs {', '.join(d.task(m).title.lower() for m in missing)}"
        return False
    return True


def follow_up_of(d: Discharge) -> str | None:
    appt = d.task("scheduling").data.get("appointment") or {}
    return f"{appt.get('clinician')}, {appt.get('when')}" if appt else None


async def run_task(deps: OrchestratorDeps, d: Discharge, task_id: str, user_token: str,
                   notes: str = "") -> Task:
    """Delegate one checklist task to its agent and record the outcome on the checklist.

    Used by the rules checklist and, as tools, by the coordinator model. Dependencies are
    enforced here, in code: the model can choose the order, not skip a prerequisite."""
    d_task, pid = d.task(task_id), d.patient_id
    if d_task.status in ("done", "needs_signature", "running", "suspended"):
        return d_task
    if not _ready(deps, d, d_task):
        return d_task
    plan = d.task("medrec").data.get("discharge_medications", [])
    if task_id == "medrec":
        if r := await _call(deps, d_task, user_token, "reconcile_medications",
                            {"patient_id": pid}):
            n = len(r["discrepancies"])
            _finish(d_task, "done", f"{n} issue(s) flagged for your review" if n
                    else "No issues", r)
    elif task_id == "benefits":
        if r := await _call(deps, d_task, user_token, "arrange_equipment",
                            {"patient_id": pid}):
            pa = r.get("authorization") or {}
            if pa.get("status") == "not needed":
                _finish(d_task, "done", "No prior authorization needed", r)
            else:
                _finish(d_task, "done" if pa.get("status") == "approved" else "failed",
                        f"Prior authorization {pa.get('status', 'not obtained')} "
                        f"{pa.get('authorization', '')}".strip(), r)
    elif task_id == "scheduling":
        if r := await _call(deps, d_task, user_token, "book_follow_up", {"patient_id": pid}):
            appt = r.get("appointment") or {}
            booked = f"{appt.get('clinician')}, {appt.get('when')}" if appt else None
            _finish(d_task, "done" if appt else "failed",
                    f"Booked: {booked}" if appt else "Not booked", r)
    elif task_id == "prescriptions":
        if r := await _call(deps, d_task, user_token, "prepare_prescriptions",
                            {"patient_id": pid, "medication_plan": plan, "notes": notes}):
            d_task.signatures = [{**x, "status": "awaiting_signature"}
                                 for x in r["prescriptions"]]
            _finish(d_task, "needs_signature" if d_task.signatures else "done",
                    f"{len(d_task.signatures)} prescription(s) need your signature"
                    if d_task.signatures else "None needed", r)
    elif task_id == "summary":
        if r := await _call(deps, d_task, user_token, "draft_discharge_summary",
                            {"patient_id": pid, "medication_plan": plan,
                             "follow_up": follow_up_of(d)}):
            d_task.signatures = [{"signature_id": r["signature_id"],
                                  "title": "Discharge summary", "content": r["summary"],
                                  "status": "awaiting_signature"}]
            _finish(d_task, "needs_signature", "Drafted; needs your signature", r)
    elif task_id == "instructions":
        if r := await _call(deps, d_task, user_token, "write_patient_instructions",
                            {"patient_id": pid, "medication_plan": plan,
                             "follow_up": follow_up_of(d)}):
            _finish(d_task, "done", "Posted to the patient portal"
                    + (" · ⚠ injection attempt reported" if r.get("injection_warnings")
                       else ""), r)
    await _contain(deps, d, d_task)
    _audit_decision(deps, d, d_task)
    return d_task


async def _contain(deps: OrchestratorDeps, d: Discharge, task: Task) -> None:
    """Did the gateway block this agent from something outside its job? Then: alert, suspend
    the agent's task (its result is not used) and hold the discharge for a physician."""
    if deps.watch is None:
        return
    seen = {a["seq"] for a in d.alerts}
    try:
        found = [e for e in await deps.watch.denials(task.agent) if e["seq"] not in seen]
    except Exception:  # noqa: BLE001 - the audit service is down: nothing to act on
        return
    if not found:
        return
    for entry in found:
        tampered = entry.get("action") == "approval.tamper_detected"
        alert = {"id": f"alert-{entry['seq']}", "seq": entry["seq"], "task_id": task.id,
                 "agent": task.agent, "patient_id": entry.get("patient_id"),
                 "attempted": ("changing signed content" if tampered
                               else (entry.get("details") or {}).get("tool")),
                 "stopped_by": "the signature check" if tampered else "the EHR gateway",
                 "reason": entry["reason"], "status": "open", "raised_at": time.time()}
        d.alerts.append(alert)
        with tracer.start_as_current_span("orchestrator security alert") as span:
            span.set_attributes({"agent.id": NAME, "alert.agent": task.agent,
                                 "alert.tool": alert["attempted"] or "",
                                 "alert.reason": alert["reason"], "alert.audit_seq": entry["seq"],
                                 "patient.id": d.patient_id})
        if deps.audit is not None:
            deps.audit.record(AuditEvent(
                action="security.alert", decision="deny", severity="high",
                actor_user=d.started_by, agent_chain=[NAME, task.agent],
                resource=f"Patient/{d.patient_id}", patient_id=d.patient_id,
                reason=f"{task.agent} suspended: {alert['stopped_by']} blocked "
                       f"{alert['attempted']} "
                       f"({alert['reason']}); discharge on hold for physician review",
                details={"task": task.id, "gateway_audit_seq": entry["seq"]}))
    task.status, task.summary = "suspended", "⚠ Agent suspended: security review"
    task.detail = ("🚨 Stopped by a boundary, not reported by the agent: "
                   + "; ".join(f"{a['stopped_by']} caught it {a['attempted']} ({a['reason']})"
                               for a in d.alerts
                               if a["task_id"] == task.id and a["status"] == "open")
                   + ". Its result is not used. A physician must review, or take the task over "
                     "by hand, before the discharge can finish.")
    if task.id not in SENDS:   # a send keeps its record of what went out and what didn't
        task.data = {}
    for signature in task.signatures:
        if signature["status"] == "awaiting_signature":
            signature["status"] = "withdrawn"
    d.refresh_status()


def settle_sends(deps: OrchestratorDeps, d: Discharge) -> None:
    """The two send tasks run after signatures; say what they are waiting for."""
    for send_id in ("send_prescriptions", "send_summary"):
        send = d.task(send_id)
        if send.status in ("sent", "failed"):
            continue
        source = d.task(send.depends_on[0]).status
        if not deps.agent_enabled(send.agent):
            send.status, send.summary = "disabled", "Turned off by the admin"
        elif source == "needs_signature":
            send.status, send.summary = "waiting", "After your signature"
        elif source == "done" and not d.task(send.depends_on[0]).signatures:
            send.status, send.summary = "sent", "Nothing to send"
        else:
            send.status, send.summary = "blocked", "Nothing signed to send"


# Send task -> the task whose signed drafts it sends.
SENDS = {"send_prescriptions": "prescriptions", "send_summary": "summary"}
SENT_BY = {source: send for send, source in SENDS.items()}
# A signature that needs nothing more: sent, rejected, replaced by a new draft, or done by hand.
SETTLED = ("sent", "rejected", "replaced", "manual")


async def _continue(deps: OrchestratorDeps, d: Discharge, user_token: str) -> None:
    """After a review: run whatever is waiting or was blocked, then settle the sends."""
    for task_id in CHECKLIST_ORDER:
        if d.task(task_id).status in ("waiting", "blocked"):
            await run_task(deps, d, task_id, user_token)
    settle_sends(deps, d)
    d.refresh_status()


async def _redraft(deps: OrchestratorDeps, d: Discharge, send: Task, user_token: str) -> None:
    """A send changed signed content and was reviewed: draft again, from the verified plan,
    exactly what did not go out. The physician signs the new drafts; nothing already sent is
    sent twice. If the agent is still compromised, the next send is caught again."""
    source = d.task(SENDS[send.id])
    redo = [s for s in source.signatures if s["status"] in ("refused", "signed")]
    for signature in redo:
        signature["status"] = "replaced"
        signature["result"] = "Replaced by a new draft after the security review"
    send.status, send.summary, send.detail = "waiting", "After your signature", ""
    if not redo:
        return
    plan = d.task("medrec").data.get("discharge_medications", [])
    if source.id == "prescriptions":
        wanted = [s["content"] for s in redo]
        items = [m for m in plan if m.get("prescription")
                 and {"patient_id": d.patient_id, **m["prescription"]} in wanted]
        r = await _call(deps, source, user_token, "prepare_prescriptions", {
            "patient_id": d.patient_id, "medication_plan": items,
            "notes": "Re-draft after a security review: the signed copy was altered in sending."})
        new = [{**x, "status": "awaiting_signature"} for x in (r or {}).get("prescriptions", [])]
    else:
        r = await _call(deps, source, user_token, "draft_discharge_summary", {
            "patient_id": d.patient_id, "medication_plan": plan, "follow_up": follow_up_of(d)})
        new = ([{"signature_id": r["signature_id"], "title": "Discharge summary",
                 "content": r["summary"], "status": "awaiting_signature"}] if r else [])
    if r is None:   # _call marked the task failed and says why
        return
    source.signatures += new
    source.status = "needs_signature" if new else "done"
    source.summary = f"{len(new)} new draft(s) after the security review: need your signature"
    source.detail, source.updated_at = r.get("text", ""), time.time()


def _take_over(d: Discharge, task: Task, by: str) -> None:
    """The physician does a suspended agent's task by hand (outside this system). Its drafts
    and sends are settled as manual, so the discharge can finish without the agent."""
    if task.id in SENDS or task.id in SENT_BY:
        source = d.task(SENDS.get(task.id, task.id))
        send = d.task(SENT_BY[source.id])
        for signature in source.signatures:
            if signature["status"] not in SETTLED:
                signature["status"], signature["result"] = "manual", by
        if source.status in ("suspended", "failed", "needs_signature", "waiting", "blocked"):
            source.status, source.summary = "done", by
        send.status, send.summary, send.detail = "sent", by, ""
    else:
        task.status, task.summary, task.detail, task.data = "done", by, "", {}
    task.updated_at = time.time()


async def _finish_what_the_model_left(deps: OrchestratorDeps, d: Discharge,
                                      user_token: str) -> str:
    """After a model-planned discharge: run, in the fixed order, every task the model did
    not get to. The model chooses the order; it can't leave the discharge half done (for
    example, stopping everything because one agent was suspended). ``run_task`` still
    enforces prerequisites and leaves suspended tasks alone."""
    ran = []
    for task_id in CHECKLIST_ORDER:
        if d.task(task_id).status not in ("waiting", "blocked"):
            continue
        task = await run_task(deps, d, task_id, user_token)
        if task.status not in ("waiting", "blocked"):
            ran.append(task.title.lower())
    settle_sends(deps, d)
    d.refresh_status()
    return f"\n\n_↪ rules ran what the model left: {', '.join(ran)}_" if ran else ""


async def _run_checklist(deps: OrchestratorDeps, d: Discharge, user_token: str) -> None:
    """The rules plan (AI off, or no model answering): every task, fixed order."""
    for task_id in CHECKLIST_ORDER:
        await run_task(deps, d, task_id, user_token)
    settle_sends(deps, d)


def _audit_decision(deps: OrchestratorDeps, d: Discharge, task: Task) -> None:
    """What the coordinator learned from an agent, in the audit trail (the agent records
    its own ``agent.decision`` too)."""
    if deps.audit is None or task.status in ("waiting", "running"):
        return
    deps.audit.record(AuditEvent(
        action="coordinator.step", decision="info" if task.status != "failed" else "deny",
        severity="info" if task.status != "failed" else "warning",
        actor_user=d.started_by, agent_chain=[NAME, task.agent],
        resource=f"Patient/{d.patient_id}", patient_id=d.patient_id,
        reason=f"{task.title}: {task.summary}"[:300], details={"task": task.id,
                                                              "status": task.status}))


async def _after_signature(deps: OrchestratorDeps, d: Discharge, task: Task, body: Signed,
                           user_token: str) -> Task:
    """Send what the physician signed. Returns the send task (the coordinator then checks the
    gateways' records for anything the send was caught doing)."""
    signature = next(s for s in task.signatures if s["signature_id"] == body.signature_id)
    send = d.task(SENT_BY[task.id])
    if body.decision == "rejected":
        signature["status"] = "rejected"
    else:
        if send.status == "suspended":
            # Never through an agent under security review: held until IT fixes it (then
            # re-drafted on review) or the physician does it by hand.
            signature["status"] = "signed"
            signature["result"] = "Signed, held: sending is suspended for security review"
        elif not deps.agent_enabled(send.agent):
            send.status, send.summary = "disabled", "Turned off by the admin"
            signature["status"] = "signed"
        elif task.id == "prescriptions":
            r = await _call(deps, send, user_token, "send_prescription", {
                "patient_id": d.patient_id, "signature_id": body.signature_id,
                "prescription": signature["content"]})
            signature["status"] = "sent" if r and r.get("sent") else "refused"
            signature["result"] = (r or {}).get("text", send.detail)
        else:
            r = await _call(deps, send, user_token, "send_summary_to_pcp", {
                "patient_id": d.patient_id, "signature_id": body.signature_id,
                "summary": signature["content"]})
            signature["status"] = "sent" if r and r.get("sent") else "refused"
            signature["result"] = (r or {}).get("text", send.detail)
    # After a rejection too: rejecting the last open draft must settle the send task, or the
    # discharge waits forever for a signature nobody is asked for.
    outcomes = [s["status"] for s in task.signatures]
    if send.status not in ("disabled", "suspended"):
        if any(s == "refused" for s in outcomes):
            send.status, send.summary = "failed", "Refused: see details"
        elif all(s in SETTLED for s in outcomes):
            sent = outcomes.count("sent")
            send.status = "sent"
            send.summary = f"{sent} sent" if sent else "Nothing sent: you rejected the draft"
        else:
            send.status, send.summary = "waiting", "Waiting for the rest of your signatures"
        send.detail = "\n".join(f"{s['title']}: {s.get('result', s['status'])}"
                                for s in task.signatures)
    if all(s["status"] != "awaiting_signature" for s in task.signatures):
        task.status, task.summary = "done", "Signed"
    task.updated_at = time.time()
    return send


def _already(d: Discharge) -> str:
    if d.status == "completed":
        when = time.strftime("%H:%M", time.localtime(
            (d.ehr_record or {}).get("recorded_at") or d.started_at or 0))
        return (f"this discharge is complete (recorded in the EHR at {when}); a completed "
                "discharge can't be run again")
    return (f"a discharge is already under way ({d.status.replace('_', ' ')}, started by "
            f"{d.started_by}); reopen it to start over")


async def _record_if_complete(deps: OrchestratorDeps, d: Discharge, user_token: str) -> None:
    """Everything signed has gone out: record the discharge in the EHR, once.

    The coordinator's only EHR permission: ``record_discharge``, on a token exchanged from the
    physician's (``discharge:order``). It still reads nothing."""
    if d.status != "completed" or (d.ehr_record or {}).get("recorded"):
        return
    with tracer.start_as_current_span("orchestrator record discharge") as span:
        span.set_attributes({"agent.id": NAME, "patient.id": d.patient_id})
        try:
            result = await _ehr_record_discharge(deps, d.patient_id, user_token)
            d.ehr_record = {**result.get("discharge", {}), "recorded": True,
                            "recorded_at": time.time()}
        except AgentError as exc:
            d.ehr_record = {"recorded": False, "recorded_at": time.time(), "error": str(exc)}
            span.set_attribute("error.reason", str(exc))


async def _ehr_record_discharge(deps: OrchestratorDeps, patient_id: str,
                                user_token: str) -> dict[str, Any]:
    delegation = await delegate(deps.keycloak, deps.secrets, NAME, user_token, "ehr-mcp",
                                ["discharge:order"])
    async with mcp_session(deps.ehr_url, delegation.token) as ehr:
        return await call_tool(ehr, "record_discharge", {
            "patient_id": patient_id, "disposition": "home",
            "note": "Discharge coordinated by agents; physician signed every order."})


def _audit(deps: OrchestratorDeps, claims: TokenClaims, patient_id: str, action: str,
           reason: str, decision: str = "info", details: dict[str, Any] | None = None) -> None:
    if deps.audit is None:
        return
    deps.audit.record(AuditEvent(
        action=action, decision=decision,
        severity="warning" if decision == "deny" else "info", **actor_fields(claims),
        agent_chain=[claims.authorized_party, NAME], resource=f"Patient/{patient_id}",
        patient_id=patient_id, reason=reason, details={"planner": PLANNER, **(details or {})}))
