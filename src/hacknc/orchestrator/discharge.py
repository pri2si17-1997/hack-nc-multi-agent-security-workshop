"""The discharge checklist: which agent does what, in which order, and what waits for whom.

    medrec ─┬─▶ prescriptions ─(physician signs)─▶ send prescriptions
            ├─▶ summary ───────(physician signs)─▶ send summary to family doctor
            └─▶ instructions (also needs the follow-up date)
    benefits (home equipment)   scheduling (follow-up)     ... independent

The coordinator holds no patient data and reads nothing. It delegates each task to one agent
with a token narrowed to that agent, and records the outcome here. Its one write: when every
signed item has gone out, it records the discharge in the EHR (``record_discharge``, with the
physician's ``discharge:order``).

Lifecycle: not_started ─start─▶ in_progress ⇄ awaiting_signatures ─▶ completed (final)
A discharge under way can be reopened (back to not_started); a completed one cannot be re-run.

Containment: when a gateway blocks an agent from something outside its job, the coordinator
(reading the gateway's own audit entries) raises a security alert, suspends that agent's task
and puts the discharge on hold. Only a physician can acknowledge the alert; the task is then
retried. A discharge on hold never completes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

TaskStatus = Literal["waiting", "running", "done", "needs_signature", "sent", "blocked",
                     "disabled", "failed", "suspended"]


@dataclass
class Task:
    id: str
    title: str
    agent: str
    scopes: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    status: TaskStatus = "waiting"
    summary: str = ""
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    signatures: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0


def new_checklist() -> list[Task]:
    return [
        Task("medrec", "Medication reconciliation", "medrec-agent", ("patient:read",)),
        Task("benefits", "Home equipment: coverage and prior authorization", "benefits-agent",
             ("patient:read", "disclose:prior-auth")),
        Task("scheduling", "Follow-up appointment", "scheduling-agent",
             ("patient:read", "disclose:scheduling")),
        Task("prescriptions", "Discharge prescriptions", "prescription-agent",
             ("patient:read", "sign:request", "disclose:registry"), ("medrec",)),
        Task("summary", "Discharge summary", "summary-agent", ("patient:read", "sign:request"),
             ("medrec",)),
        Task("instructions", "Patient instructions", "instructions-agent",
             ("patient:read", "portal:write"), ("medrec", "scheduling")),
        Task("send_prescriptions", "Send prescriptions to pharmacy", "prescription-agent",
             ("disclose:prescription",), ("prescriptions",)),
        Task("send_summary", "Send summary to family doctor", "transitions-agent",
             ("disclose:care-transition",), ("summary",)),
    ]


@dataclass
class Discharge:
    patient_id: str
    status: Literal["not_started", "in_progress", "awaiting_signatures", "on_hold",
                    "completed"] = "not_started"
    started_by: str | None = None
    started_at: float | None = None
    tasks: list[Task] = field(default_factory=new_checklist)
    ehr_record: dict[str, Any] | None = None   # set once the EHR has the discharge
    alerts: list[dict[str, Any]] = field(default_factory=list)   # security alerts

    def open_alerts(self) -> list[dict[str, Any]]:
        return [a for a in self.alerts if a["status"] == "open"]

    def task(self, task_id: str) -> Task:
        return next(t for t in self.tasks if t.id == task_id)

    def task_for_signature(self, signature_id: str) -> Task | None:
        return next((t for t in self.tasks
                     if any(s["signature_id"] == signature_id for s in t.signatures)), None)

    def refresh_status(self) -> None:
        statuses = {t.id: t.status for t in self.tasks}
        if self.status == "completed":
            return
        if self.open_alerts():
            self.status = "on_hold"
        elif any(s == "needs_signature" for s in statuses.values()):
            # Checked before completion: a draft nobody signed keeps the discharge open, even
            # when the agent that would send it is switched off.
            self.status = "awaiting_signatures"
        elif any(s in ("suspended", "running") for s in statuses.values()):
            # A suspended agent's work was not used, and a running one isn't finished:
            # completion is final, so it must wait for them.
            self.status = "in_progress" if self.started_at else self.status
        elif statuses["send_prescriptions"] in ("sent", "disabled") and \
                statuses["send_summary"] in ("sent", "disabled"):
            self.status = "completed"
        elif self.started_at:
            self.status = "in_progress"

    def view(self) -> dict[str, Any]:
        return {"patient_id": self.patient_id, "status": self.status,
                "started_by": self.started_by, "started_at": self.started_at,
                "ehr_record": self.ehr_record, "alerts": self.alerts,
                "tasks": [{k: v for k, v in asdict(t).items() if k != "scopes"}
                          for t in self.tasks]}


class AlreadyStarted(Exception):
    def __init__(self, discharge: Discharge) -> None:
        super().__init__(discharge.status)
        self.discharge = discharge


class DischargeBoard:
    """All discharges in progress (in memory; the admin's reset clears it)."""

    def __init__(self) -> None:
        self._items: dict[str, Discharge] = {}
        self._lock = threading.Lock()

    def get(self, patient_id: str) -> Discharge:
        with self._lock:
            return self._items.setdefault(patient_id, Discharge(patient_id))

    def start(self, patient_id: str, started_by: str) -> Discharge:
        """Begin a discharge. Raises ``AlreadyStarted`` unless it is ``not_started``."""
        with self._lock:
            current = self._items.get(patient_id)
            if current is not None and current.status != "not_started":
                raise AlreadyStarted(current)
            item = Discharge(patient_id, status="in_progress", started_by=started_by,
                             started_at=time.time())
            self._items[patient_id] = item
            return item

    def reopen(self, patient_id: str) -> list[str]:
        """Back to not_started. Returns the signature ids still waiting (to be cancelled).
        Raises ``AlreadyStarted`` for a completed discharge: that one is final."""
        with self._lock:
            current = self._items.get(patient_id)
            if current is None or current.status == "not_started":
                return []
            if current.status == "completed":
                raise AlreadyStarted(current)
            waiting = [s["signature_id"] for t in current.tasks for s in t.signatures
                       if s["status"] in ("awaiting_signature", "withdrawn")]
            self._items[patient_id] = Discharge(patient_id)
            return waiting

    def statuses(self) -> dict[str, str]:
        with self._lock:
            return {pid: d.status for pid, d in self._items.items()}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
