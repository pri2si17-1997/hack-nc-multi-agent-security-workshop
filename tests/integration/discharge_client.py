"""Drive the hospital UI like the browser does: log in, act, read the run's event stream."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from tests.integration.test_ui_login import UI, login_as

PATIENT = "jane-doe"


@dataclass
class Run:
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)

    def has(self, text: str, status: str | None = None) -> bool:
        return any(text in s["title"] and (status is None or s["status"] == status)
                   for s in self.steps)

    @property
    def checklist(self) -> dict[str, Any]:
        return self.result.get("checklist", {})

    def task(self, task_id: str) -> dict[str, Any]:
        return next(t for t in self.checklist["tasks"] if t["id"] == task_id)


def browser_for(username: str) -> httpx.Client:
    try:
        httpx.get(f"{UI}/healthz", timeout=3).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("UI is not reachable; run `make up` first")
    client = httpx.Client(follow_redirects=False, timeout=180)
    login_as(client, username)
    return client


def follow(client: httpx.Client, started: dict[str, Any]) -> Run:
    run = Run()
    with client.stream("GET", f"{UI}/api/runs/{started['run_id']}/events",
                       timeout=180) as response:
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "step":
                run.steps.append(event)
            elif event["type"] == "answer":
                run.result = event
            elif event["type"] in ("done", "error"):
                break
    return run


def chat(client: httpx.Client, message: str, patient: str = PATIENT) -> Run:
    """A chat message to the coordinator. A refusal in the reply (🛑) counts as not ok."""
    run = follow(client, client.post(f"{UI}/api/chat",
                                     json={"patient_id": patient, "message": message}).json())
    reply = run.result.get("reply", "")
    if run.result.get("ok") and reply.startswith("🛑"):
        run.result = {**run.result, "ok": False, "reason": reply}
    return run


def start(client: httpx.Client, patient: str = PATIENT, name: str = "Jane Doe") -> Run:
    return chat(client, f"Discharge {name}", patient)


def reopen(client: httpx.Client, patient: str = PATIENT) -> Run:
    return follow(client, client.post(f"{UI}/api/discharges/{patient}/reopen",
                                      json={"reason": "integration test"}).json())


def sign(client: httpx.Client, signature_id: str, decision: str = "approve",
         patient: str = PATIENT) -> Run:
    return follow(client, client.post(f"{UI}/api/signatures/{signature_id}",
                                      json={"patient_id": patient, "decision": decision}).json())


def acknowledge(client: httpx.Client, alert_id: str, patient: str = PATIENT) -> Run:
    return follow(client, client.post(
        f"{UI}/api/discharges/{patient}/alerts/{alert_id}/acknowledge").json())


def ask(client: httpx.Client, question: str, patient: str = PATIENT) -> Run:
    return chat(client, question, patient)


def signatures(run: Run) -> dict[str, dict[str, Any]]:
    return {s["title"]: s for t in run.checklist["tasks"] for s in t["signatures"]}
