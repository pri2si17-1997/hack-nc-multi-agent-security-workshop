"""Runs: each clinician action (start discharge, sign, ask) is one trace the UI follows live.

    GET  /api/runs/{id}/events    Server-Sent Events: steps as they happen, then the result
    POST /otlp/v1/traces          the OpenTelemetry Collector forwards spans here (OTLP/JSON)

``start_run`` opens the run's root span, hands back its id at once, and does the work in the
background inside that span, so every service involved joins the same trace.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from opentelemetry import trace

from hacknc.common.telemetry import format_trace_id
from hacknc.ui import presenters
from hacknc.ui.context import UiContext
from hacknc.ui.otlp import parse_export
from hacknc.ui.sessions import UserSession

tracer = trace.get_tracer(__name__)
Work = Callable[[], Awaitable[dict[str, Any]]]


def start_run(ctx: UiContext, session: UserSession, action: str, label: str,
              patient_id: str | None, work: Work) -> dict[str, Any]:
    claims = session.access_claims
    span = tracer.start_span("ui.action", attributes={
        "user.name": claims.username, "user.role": presenters.primary_role(claims),
        "ui.action": action, "question": label, "patient.id": patient_id or "",
        "token.scope": " ".join(sorted(claims.scopes))})
    trace_id = format_trace_id(span.get_span_context().trace_id)
    ctx.deps.runs.start(trace_id, action=action, user=claims.username)

    async def runner() -> None:
        with trace.use_span(span, end_on_exit=True):
            try:
                result = await work()
            except Exception as exc:  # report, don't hang the browser
                result = {"ok": False, "reason": f"{exc.__class__.__name__}: {exc}"}
        ctx.deps.runs.finish(trace_id, result)

    ctx.deps.runs.background(runner())
    return {"run_id": trace_id,
            "trace_url": presenters.grafana_trace_url(ctx.deps.settings.grafana_url, trace_id)}


def register(app: FastAPI, ctx: UiContext) -> None:
    deps = ctx.deps

    @app.get("/api/runs/{run_id}/events")
    async def run_events(request: Request, run_id: str) -> StreamingResponse:
        session = ctx.api_session(request)
        run = deps.runs.get(run_id)
        # A run carries its answer (chart content): only the person who started it may follow.
        if run is None or run.meta.get("user") != session.access_claims.username:
            raise HTTPException(404, detail="no such run")

        async def sse():
            async for event in deps.runs.stream(run_id):
                if event.get("type") == "step" and event.get("span"):
                    event = {**event, "trace_url": presenters.grafana_trace_url(
                        deps.settings.grafana_url, run_id, event["span"])}
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post("/otlp/v1/traces")
    async def receive_spans(request: Request) -> dict[str, Any]:
        """OTLP/HTTP JSON from the Collector (already PHI-redacted). No session: internal."""
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="expected OTLP/JSON") from None
        deps.runs.add_spans(parse_export(body))
        return {"partialSuccess": {}}
