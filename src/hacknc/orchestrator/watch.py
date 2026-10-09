"""How the coordinator binds agents to patients and notices when one oversteps.

* ``TaskAssigner``  before delegating, tell the EHR gateway which patient the task is about
                    (``POST /tasks/assign``, scope ``task:assign``, as the coordinator itself);
                    after it, release that binding (``POST /tasks/release``)
* ``AlertWatch``    after a task, read the **gateways' own audit entries** for that agent in
                    this trace: anything the EHR gateway denied it, or a signed item it changed
                    (the signature service's tamper check), is a security alert. The agent is
                    never asked: a rogue agent would not report itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
import httpx

from hacknc.audit_service.client import AuditApi
from hacknc.common.audit import current_trace_id
from hacknc.common.auth.service_tokens import ServiceTokenProvider

# Denials that are never an honest mistake: a tool outside the agent's job or patient (EHR
# gateway), or content changed after a physician signed it (signature service). Not "a partner
# is switched off", and not a disclosure refused for lack of a signature (that one is silent).
OVERSTEP_ACTIONS = {"mcp.tool_call"}
TAMPER_ACTIONS = {"approval.tamper_detected"}


class AssignmentError(RuntimeError):
    pass


class TaskAssigner:
    def __init__(self, ehr_base_url: str, tokens: ServiceTokenProvider,
                 http: httpx.Client | None = None) -> None:
        self.base = ehr_base_url.rstrip("/").removesuffix("/mcp") + "/tasks"
        self._tokens = tokens
        self._http = http or httpx.Client(timeout=5.0)

    def assign(self, agent: str, user: str, patient_id: str) -> None:
        try:
            response = self._post("assign", agent, user, patient_id)
        except Exception as exc:  # noqa: BLE001 - network or token failure
            raise AssignmentError(f"could not tell the EHR gateway about the task: {exc}") from exc
        if response.status_code >= 300:
            raise AssignmentError(f"EHR gateway refused the task assignment: {response.text[:200]}")

    def release(self, agent: str, user: str, patient_id: str) -> None:
        """Best effort: if it is lost, the assignment still expires."""
        try:
            self._post("release", agent, user, patient_id)
        except Exception:  # noqa: BLE001 - network or token failure
            pass

    def _post(self, action: str, agent: str, user: str, patient_id: str) -> httpx.Response:
        return self._http.post(f"{self.base}/{action}",
                               json={"agent": agent, "user": user, "patient_id": patient_id},
                               headers={"Authorization": f"Bearer {self._tokens.token()}"})


class AlertWatch:
    def __init__(self, audit: AuditApi, tokens: Callable[[], str]) -> None:
        self._audit, self._tokens = audit, tokens

    async def denials(self, agent: str, after_seq: int = 0) -> list[dict[str, Any]]:
        """Gateway denials of ``agent`` in the current trace, newer than ``after_seq``."""
        trace_id = current_trace_id()
        if trace_id is None:
            return []

        def read() -> list[dict[str, Any]]:
            entries = []
            for writer in ("ehr-mcp", "approval-service"):
                result = self._audit.events(self._tokens(), limit=100, decision="deny",
                                            writer=writer, trace_id=trace_id)
                entries += result.body.get("entries", []) if result.ok else []
            return entries

        entries = await anyio.to_thread.run_sync(read)
        # The signature service records the chain that filed the draft: [the agent, ehr-mcp].
        return sorted((e for e in entries
                       if e["seq"] > after_seq
                       and e.get("action") in OVERSTEP_ACTIONS | TAMPER_ACTIONS
                       and (e.get("agent_chain") or [None])[0] == agent),
                      key=lambda e: e["seq"])
