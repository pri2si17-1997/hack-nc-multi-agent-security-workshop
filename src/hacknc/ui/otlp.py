"""Parse OTLP/JSON trace exports (what the OpenTelemetry Collector sends us).

The Collector forwards every span to the UI as well as to Tempo (after PHI redaction), so
the activity map and feed show the same data you see in Grafana, rendered for humans.

Format reference: ExportTraceServiceRequest in OTLP/JSON encoding. Ids are hex strings,
timestamps are nanosecond strings, attribute values are typed (``stringValue``...).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    service: str
    start_ns: int
    end_ns: int
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def attr(self, key: str, default: Any = "") -> Any:
        return self.attributes.get(key, default)


def any_value(value: dict[str, Any]) -> Any:
    """Decode an OTLP AnyValue."""
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "arrayValue" in value:
        return [any_value(v) for v in value["arrayValue"].get("values", [])]
    return None


def attributes(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: any_value(a.get("value", {})) for a in items or []}


def parse_export(body: dict[str, Any]) -> list[SpanRecord]:
    spans: list[SpanRecord] = []
    for resource_spans in body.get("resourceSpans", []):
        resource = attributes(resource_spans.get("resource", {}).get("attributes"))
        service = resource.get("service.name", "unknown")
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                spans.append(SpanRecord(
                    trace_id=span.get("traceId", "").lower(),
                    span_id=span.get("spanId", "").lower(),
                    parent_id=span.get("parentSpanId", "").lower(),
                    name=span.get("name", ""),
                    service=service,
                    start_ns=int(span.get("startTimeUnixNano", 0)),
                    end_ns=int(span.get("endTimeUnixNano", 0)),
                    attributes=attributes(span.get("attributes")),
                    events=[{"name": e.get("name", ""),
                             "attributes": attributes(e.get("attributes"))}
                            for e in span.get("events", [])],
                ))
    return spans
