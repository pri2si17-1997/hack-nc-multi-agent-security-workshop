"""OpenTelemetry setup: traces and logs, one call per process.

Everything is sent over OTLP/HTTP to the OpenTelemetry Collector, which forwards traces to
Tempo and logs to Loki (and redacts PHI on the way). Find them in Grafana -> Explore.

Outgoing ``httpx`` calls are instrumented so the trace id travels to the next service in the
W3C ``traceparent`` header: one user request = one trace, across every service.

Traces are meant to be read by people, so we keep them lean at the source: no spans for
health checks or for the UI receiving telemetry, no FastAPI-internal spans, and every outgoing
call is named after where it goes (``→ opa POST /v1/data/hospital/ehr``, not just ``POST``).
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

# Requests that are plumbing, not story: Docker health checks (every few seconds, from every
# container) and the UI receiving spans from the Collector (which would trace itself forever).
UNTRACED_PATHS = frozenset({"/healthz", "/otlp/v1/traces"})


def untraced(scope: MutableMapping[str, Any]) -> bool:
    """FastAPI ``exclude`` hook: True for requests that should not produce a trace."""
    return scope.get("path") in UNTRACED_PATHS


# FastAPI instruments requests out of the box. We keep one span per request but:
#   * switch off its own exporter setup: ``setup_tracing`` already exports, and a second
#     exporter would duplicate every span (and push metrics the Collector has no pipeline for)
#   * switch off "operation spans" (fastapi.dependencies / .endpoint / .serialization): three
#     extra spans per request that say nothing about agents, tokens or policy
#   * skip the plumbing requests above
FASTAPI_TELEMETRY = {"auto_configure": False, "operation_spans": False, "exclude": untraced}


def setup_tracing(
    service_name: str,
    otlp_endpoint: str,
    exporter: SpanExporter | None = None,
    set_global: bool = True,
) -> TracerProvider:
    """Create a tracer provider that labels every span with ``service_name``.

    Args:
        service_name: shows up as the service in Grafana/Tempo.
        otlp_endpoint: Collector base URL, e.g. ``http://localhost:4318``.
        exporter: override the OTLP exporter (tests pass an in-memory one).
        set_global: register as the process-wide provider and instrument httpx
            (normal for services; tests switch it off).

    Call ``provider.shutdown()`` before a short-lived script exits so spans are flushed.
    """
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    exporter = exporter or OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    if set_global:
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument(request_hook=name_client_span,
                                             async_request_hook=name_client_span_async)
    return provider


def client_span_name(method: str | bytes, url: Any) -> str:
    """``→ <host> <METHOD> <path>``: who is being called, so a trace reads as a conversation.

    The query string is left out (it can carry identifiers); the full URL stays in the span's
    ``url.full`` attribute for whoever needs it.
    """
    if isinstance(method, bytes):
        method = method.decode()
    return f"→ {url.host} {method} {url.path}"


def name_client_span(span: trace.Span, request: Any) -> None:
    if span.is_recording():
        span.update_name(client_span_name(request.method, request.url))


async def name_client_span_async(span: trace.Span, request: Any) -> None:
    name_client_span(span, request)


def setup_log_export(service_name: str, otlp_endpoint: str, logger_name: str) -> LoggerProvider:
    """Ship records from ``logger_name`` to Loki (via the Collector) as structured logs.

    ``extra={...}`` fields on a log call become attributes you can filter on in Grafana,
    and the active trace id is attached automatically.
    """
    provider = LoggerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/logs"))
    )
    set_logger_provider(provider)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    return provider


def format_trace_id(trace_id: int) -> str:
    """Render a trace id the way Grafana/Tempo display it (32 hex chars)."""
    return f"{trace_id:032x}"
