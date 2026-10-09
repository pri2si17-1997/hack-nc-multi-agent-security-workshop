import pytest

from hacknc.ui.otlp import SpanRecord
from hacknc.ui.runs import RunHub


def ask_span(trace_id: str, span_id: str = "s1") -> SpanRecord:
    return SpanRecord(trace_id, span_id, "", "ui.ask", "hospital-ui", 1, 2,
                      {"user.name": "dr.morgana", "question": "hi"})


@pytest.mark.anyio
async def test_steps_then_answer_then_done() -> None:
    hub = RunHub(grace_seconds=0.01)
    hub.start("t1", question="hi")
    assert hub.add_spans([ask_span("t1"), ask_span("t1"), ask_span("other")]) == 1
    hub.finish("t1", {"answer": "done"})
    events = [e["type"] async for e in hub.stream("t1")]
    assert events == ["step", "answer", "done"]


@pytest.mark.anyio
async def test_unknown_run() -> None:
    events = [e async for e in RunHub().stream("nope")]
    assert events[0]["type"] == "error"


def test_old_runs_are_evicted() -> None:
    hub = RunHub()
    for i in range(60):
        hub.start(f"t{i}")
    assert hub.get("t0") is None and hub.get("t59") is not None
