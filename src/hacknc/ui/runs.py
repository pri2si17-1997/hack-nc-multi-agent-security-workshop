"""Live runs: one per question asked in the assistant.

A run is identified by its trace id. Steps arrive from the Collector (spans -> ``xray``
steps) and the final answer arrives from the orchestrator call; both are fanned out to every
browser watching that run (Server-Sent Events). Spans for unknown traces are ignored.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from hacknc.ui.otlp import SpanRecord
from hacknc.ui.xray import steps_for

SPAN_GRACE_SECONDS = 4.0   # keep streaming late spans this long after the answer
MAX_RUNS = 50


@dataclass
class Run:
    trace_id: str
    meta: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    done: bool = False
    listeners: list[asyncio.Queue] = field(default_factory=list)


class RunHub:
    def __init__(self, grace_seconds: float = SPAN_GRACE_SECONDS) -> None:
        self._runs: OrderedDict[str, Run] = OrderedDict()
        self._grace = grace_seconds
        self._tasks: set[asyncio.Task] = set()

    def background(self, coroutine: Any) -> asyncio.Task:
        """Run ``coroutine`` in the background. The event loop only keeps weak references to
        tasks, so the hub holds them until they finish (or they may vanish mid-run)."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start(self, trace_id: str, **meta: Any) -> Run:
        run = Run(trace_id, meta)
        self._runs[trace_id] = run
        while len(self._runs) > MAX_RUNS:
            self._runs.popitem(last=False)
        return run

    def get(self, trace_id: str) -> Run | None:
        return self._runs.get(trace_id)

    def add_spans(self, spans: list[SpanRecord]) -> int:
        """Add steps for spans of known runs. Returns how many steps were added."""
        added = 0
        for span in spans:
            run = self._runs.get(span.trace_id)
            if run is None or run.done:
                continue
            for step in steps_for(span):
                if step.id not in run.seen:
                    run.seen.add(step.id)
                    self._publish(run, {"type": "step", **step.to_dict()})
                    added += 1
        return added

    def finish(self, trace_id: str, answer: dict[str, Any]) -> None:
        """Publish the answer now; close the stream after a grace period for late spans."""
        run = self._runs.get(trace_id)
        if run is None:
            return
        self._publish(run, {"type": "answer", **answer})

        async def close_later() -> None:
            await asyncio.sleep(self._grace)
            run.done = True
            self._publish(run, {"type": "done"})

        self.background(close_later())

    async def stream(self, trace_id: str) -> AsyncIterator[dict[str, Any]]:
        """Every event so far, then new ones as they come, until the run is done."""
        run = self._runs.get(trace_id)
        if run is None:
            yield {"type": "error", "message": "unknown run"}
            return
        queue: asyncio.Queue = asyncio.Queue()
        run.listeners.append(queue)
        try:
            for event in list(run.events):
                yield event
            if run.done:
                return
            while True:
                event = await queue.get()
                yield event
                if event["type"] == "done":
                    return
        finally:
            run.listeners.remove(queue)

    def _publish(self, run: Run, event: dict[str, Any]) -> None:
        run.events.append(event)
        for queue in run.listeners:
            queue.put_nowait(event)
