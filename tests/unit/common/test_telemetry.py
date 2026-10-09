import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hacknc.common.telemetry import (
    FASTAPI_TELEMETRY,
    client_span_name,
    format_trace_id,
    setup_tracing,
    untraced,
)


def test_spans_are_labelled_with_service_name() -> None:
    exporter = InMemorySpanExporter()
    provider = setup_tracing("unit-test-service", "http://unused", exporter=exporter,
                             set_global=False)

    with provider.get_tracer("test").start_as_current_span("hello"):
        pass
    provider.force_flush()

    [span] = exporter.get_finished_spans()
    assert span.name == "hello"
    assert span.resource.attributes["service.name"] == "unit-test-service"
    provider.shutdown()


def test_format_trace_id_is_32_hex_chars() -> None:
    assert format_trace_id(255) == "0" * 30 + "ff"


def test_client_spans_are_named_after_their_destination() -> None:
    url = httpx.URL("http://opa:8181/v1/data/hospital/ehr?pretty=true")
    assert client_span_name(b"POST", url) == "→ opa POST /v1/data/hospital/ehr"


def test_plumbing_requests_are_not_traced() -> None:
    assert untraced({"path": "/healthz"}) and untraced({"path": "/otlp/v1/traces"})
    assert not untraced({"path": "/api/chat"})


def test_fastapi_keeps_one_span_per_request() -> None:
    exporter = InMemorySpanExporter()
    provider = setup_tracing("unit-test-service", "http://unused", exporter=exporter,
                             set_global=False)
    app = FastAPI(telemetry={**FASTAPI_TELEMETRY, "tracer_provider": provider})

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/work")
    def work() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/healthz")
        client.get("/work")
    provider.force_flush()

    assert [s.name for s in exporter.get_finished_spans()] == ["GET /work"]
    provider.shutdown()
