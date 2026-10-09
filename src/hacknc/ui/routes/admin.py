"""The admin console (admin role only; no clinical data).

    GET  /admin                          feature switches: agents, partners, scenarios
    GET  /admin/audit                    audit trail, chain verification, disclosures
    GET  /admin/incident                 incident report for one patient (?patient=&period=)
    GET  /api/admin/flags                current switches
    POST /api/admin/flags                change switches ({"agent.benefits-agent": false, ...})
    POST /api/admin/reset                reset the demo (queue, checklists, partner logs)
    POST /api/admin/prepare-finale       reset + the finale's switches, in one audited step
    GET  /api/admin/audit/events         audit entries (filters)
    POST /api/admin/audit/verify         walk the hash chain
    GET  /api/admin/disclosures          everything that left the hospital, all patients
    GET  /api/admin/received             what each partner says it received
    GET  /api/admin/incident/{patient}   the incident report as JSON (?period=reset|all)
"""

from __future__ import annotations

from typing import Any, Literal

import anyio
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from hacknc.common.auth import AuthError
from hacknc.partners.client import PartnerApi
from hacknc.ui import incident, presenters
from hacknc.ui.audit_views import attach_payloads, attack_notes, disclosure_rows
from hacknc.ui.context import UiContext
from hacknc.ui.routes.backends import audit_events, exchanged
from hacknc.ui.sessions import UserSession
from hacknc.ui.settings import UiSettings

PAGE_SIZE = 500          # the audit service's maximum page
MAX_PAGES = 20           # 10 000 entries is plenty for a demo period
Period = Literal["reset", "all"]


async def reset_services(settings: UiSettings, key: str) -> dict[str, bool]:
    """Services that keep demo state in memory: the coordinator's checklists and the EHR
    gateway's portal state (requests, consent, task assignments)."""
    results = {}
    headers = {"X-Demo-Admin-Key": key}
    async with httpx.AsyncClient(timeout=10) as http:
        for name, method, url in (
                ("checklists", "DELETE",
                 f"{settings.orchestrator_url.rstrip('/')}/admin/discharges"),
                ("patient portal", "POST",
                 settings.ehr_mcp_url.removesuffix("/mcp") + "/admin/reset")):
            try:
                response = await http.request(method, url, headers=headers)
                results[name] = response.status_code < 300
            except httpx.HTTPError:
                results[name] = False
    return results


def register(app: FastAPI, ctx: UiContext) -> None:
    deps = ctx.deps
    settings = deps.settings
    key = settings.demo_admin_key
    partners = deps.partners or PartnerApi(settings.partners_url)

    async def flags() -> dict[str, bool]:
        result = await anyio.to_thread.run_sync(lambda: deps.approvals.get_flags(key))
        return result.body if result.ok else {}

    async def reset_demo() -> dict[str, bool]:
        """Fresh demo: empty signature queue and checklists, partners forget, no posted
        instructions. The audit chain is never reset from the app (`make reset-audit`)."""
        results = {
            "signature queue": (await anyio.to_thread.run_sync(
                lambda: deps.approvals.reset(key))).ok,
            "partners": (await anyio.to_thread.run_sync(lambda: partners.reset(key))).ok,
        }
        return {**results, **await reset_services(settings, key)}

    # ------------------------------------------------------------------ pages

    @app.get("/admin")
    async def admin_page(request: Request) -> Response:
        session = ctx.page_for_role(request, "admin")
        if not isinstance(session, UserSession):
            return session
        current = await flags()
        return ctx.render(request, "admin.html", session,
                          groups=presenters.flag_groups(current), store_ok=bool(current))

    @app.get("/admin/audit")
    def audit_page(request: Request) -> Response:
        session = ctx.page_for_role(request, "admin")
        if not isinstance(session, UserSession):
            return session
        return ctx.render(request, "admin_audit.html", session)

    @app.get("/admin/incident")
    async def incident_page(request: Request, patient: str = "jane-doe",
                            period: Period = "reset") -> Response:
        session = ctx.page_for_role(request, "admin")
        if not isinstance(session, UserSession):
            return session
        data = await incident_report(session, patient, period)
        return ctx.render(request, "admin_incident.html", session, **data)

    # ------------------------------------------------------------------ switches

    @app.get("/api/admin/flags")
    async def get_flags(request: Request) -> dict[str, bool]:
        ctx.api_for_role(request, "admin")
        return await flags()

    @app.post("/api/admin/flags")
    async def set_flags(request: Request, change: dict[str, bool]) -> dict[str, bool]:
        session = ctx.api_for_role(request, "admin")
        result = await anyio.to_thread.run_sync(lambda: deps.approvals.set_flags(key, change))
        ctx.audit(session.access_claims, "demo.control_changed", severity="warning",
                  reason=", ".join(f"{k}={'on' if v else 'off'}" for k, v in change.items()))
        return result.body if result.ok else {}

    @app.post("/api/admin/reset")
    async def reset(request: Request) -> dict[str, Any]:
        session = ctx.api_for_role(request, "admin")
        results = await reset_demo()
        ctx.audit(session.access_claims, "demo.reset", severity="warning",
                  reason="demo reset: " + ", ".join(k for k, ok in results.items() if ok))
        return {"ok": all(results.values()), "results": results}

    @app.post("/api/admin/prepare-finale")
    async def prepare_finale(request: Request) -> dict[str, Any]:
        """One click before the finale, so the setup can't be fumbled on stage: reset, then
        every agent and partner on, the model on, only the Wrong patient scenario staged."""
        session = ctx.api_for_role(request, "admin")
        results = await reset_demo()
        wanted = presenters.finale_flags()
        stored = await anyio.to_thread.run_sync(lambda: deps.approvals.set_flags(key, wanted))
        results["switches"] = stored.ok
        ctx.audit(session.access_claims, "demo.finale_prepared", severity="warning",
                  reason="finale prepared: demo reset, model on, scenario Wrong patient on")
        return {"ok": all(results.values()), "results": results,
                "flags": stored.body if stored.ok else {}}

    # ------------------------------------------------------------------ audit

    @app.get("/api/admin/audit/events")
    async def events(request: Request, decision: str | None = None,
                     severity: str | None = None, include_opa: bool = False,
                     limit: int = 150) -> dict[str, Any]:
        session = ctx.api_for_role(request, "admin")
        try:
            result = await audit_events(ctx, session, limit=limit, decision=decision,
                                        severity=severity)
        except AuthError as exc:
            return {"ok": False, "reason": str(exc), "entries": []}
        if not result["ok"]:
            return {"ok": False, "reason": result["reason"], "entries": []}
        entries = [e for e in result.get("entries", [])
                   if include_opa or e.get("writer") != "opa"]
        # Markers are fetched on their own: a "deny only" filter would leave them out.
        markers = await audit_events(ctx, session, limit=200, action="scenario.staged")
        attack_notes(entries, markers.get("entries", []) if markers["ok"] else [])
        for entry in entries:
            if entry.get("trace_id"):
                entry["trace_url"] = presenters.grafana_trace_url(settings.grafana_url,
                                                                  entry["trace_id"])
        return {"ok": True, "entries": entries, "total": result.get("total", 0)}

    @app.post("/api/admin/audit/verify")
    async def verify(request: Request) -> dict[str, Any]:
        session = ctx.api_for_role(request, "admin")
        try:
            token = await exchanged(ctx, session, "audit-service", ["audit:read"])
        except AuthError as exc:
            return {"ok": False, "reason": str(exc)}
        result = await anyio.to_thread.run_sync(lambda: deps.audit_reader.verify(token))
        return {"ok": True, "chain": result.body} if result.ok else \
            {"ok": False, "reason": result.reason}

    @app.get("/api/admin/disclosures")
    async def disclosures(request: Request) -> dict[str, Any]:
        session = ctx.api_for_role(request, "admin")
        rows = []
        for action in ("disclosure.sent", "disclosure.blocked"):
            result = await audit_events(ctx, session, limit=200, action=action)
            rows += disclosure_rows(result.get("entries", []))
        receipts = await anyio.to_thread.run_sync(lambda: partners.received(key))
        attach_payloads(rows, receipts.body.get("received", []) if receipts.ok else [])
        return {"disclosures": sorted(rows, key=lambda r: -r["seq"])}

    @app.get("/api/admin/received")
    async def received(request: Request) -> dict[str, Any]:
        ctx.api_for_role(request, "admin")
        result = await anyio.to_thread.run_sync(lambda: partners.received(key))
        return {"ok": result.ok, "received": result.body.get("received", [])}

    # ------------------------------------------------------------------ incident report

    @app.get("/api/admin/incident/{patient_id}")
    async def incident_json(request: Request, patient_id: str,
                            period: Period = "reset") -> dict[str, Any]:
        session = ctx.api_for_role(request, "admin")
        data = await incident_report(session, patient_id, period)
        return {"ok": data["error"] is None, "reason": data["error"], **data}

    async def incident_report(session: UserSession, patient_id: str,
                              period: Period) -> dict[str, Any]:
        """Read the period from the audit trail, the partners' logs and the chain check, then
        let ``hacknc.ui.incident`` answer the five questions."""
        base = {"patient_id": patient_id, "period_choice": period, "patients": [],
                "report": None, "error": None}
        try:
            token = await exchanged(ctx, session, "audit-service", ["audit:read"])
        except AuthError as exc:
            return {**base, "error": str(exc)}

        def read(**filters: Any) -> list[dict[str, Any]]:
            result = deps.audit_reader.events(token, **filters)
            if not result.ok:
                raise RuntimeError(result.reason)
            return result.body.get("entries", [])

        def period_entries(after: int | None) -> list[dict[str, Any]]:
            entries: list[dict[str, Any]] = []
            before = None
            for _ in range(MAX_PAGES):
                page = read(limit=PAGE_SIZE, after=after, before=before)
                entries += page
                if len(page) < PAGE_SIZE:
                    break
                before = page[-1]["seq"]
            return entries

        def gather() -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any]]:
            marks = [m for action in incident.PERIOD_MARKS
                     for m in read(limit=1, action=action)]
            since = incident.period_start(marks) if period == "reset" else None
            entries = period_entries(since["seq"] if since else None)
            chain = deps.audit_reader.verify(token)
            return since, entries, ({"chain": chain.body} if chain.ok
                                    else {"problem": chain.reason})

        try:
            since, entries, verified = await anyio.to_thread.run_sync(gather)
        except RuntimeError as exc:
            return {**base, "error": f"The audit trail could not be read: {exc}"}
        receipts = await anyio.to_thread.run_sync(lambda: partners.received(key))
        report = incident.build_report(
            patient_id, entries, receipts.body.get("received", []) if receipts.ok else [],
            verified.get("chain"), since=since, chain_problem=verified.get("problem", ""))
        report["partners_ok"] = receipts.ok
        return {**base, "patients": incident.patients_in(entries), "report": report}
