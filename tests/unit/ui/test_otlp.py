from hacknc.ui.otlp import parse_export

EXPORT = {"resourceSpans": [{
    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "ehr-mcp"}}]},
    "scopeSpans": [{"spans": [{
        "traceId": "ABCD", "spanId": "01", "parentSpanId": "00", "name": "ehr-mcp get_claims",
        "startTimeUnixNano": "100", "endTimeUnixNano": "200",
        "attributes": [
            {"key": "policy.decision", "value": {"stringValue": "allow"}},
            {"key": "ehr.withheld", "value": {"intValue": "2"}},
            {"key": "ehr.break_glass", "value": {"boolValue": False}},
            {"key": "tags", "value": {"arrayValue": {"values": [{"stringValue": "a"}]}}},
        ],
        "events": [{"name": "e", "attributes": [{"key": "detail", "value": {"stringValue": "x"}}]}],
    }]}],
}]}


def test_parse_export() -> None:
    [span] = parse_export(EXPORT)
    assert span.trace_id == "abcd" and span.service == "ehr-mcp"
    assert (span.start_ns, span.end_ns) == (100, 200)
    assert span.attributes == {"policy.decision": "allow", "ehr.withheld": 2,
                               "ehr.break_glass": False, "tags": ["a"]}
    assert span.events == [{"name": "e", "attributes": {"detail": "x"}}]


def test_empty_export() -> None:
    assert parse_export({}) == []
