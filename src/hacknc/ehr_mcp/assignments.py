"""Which patient each agent's current task is about, as the discharge coordinator says.

The coordinator delegates a task ("reconcile Jane Doe's medications for dr.morgana") and, just
before, tells the EHR gateway: medrec-agent, acting for dr.morgana, may read jane-doe. OPA then
denies an agent any call about a patient it was not given (``hospital/ehr`` tool_decision),
whatever the agent's token would otherwise allow. Agents never write here; only the
coordinator can (Keycloak scope ``task:assign``, checked by ``POST /tasks/assign``).

The coordinator releases an assignment when the task ends, so a denial names only the
patient of the task in hand. Assignments also expire (default 30 minutes, in case a release
is lost) and are forgotten on the demo reset.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

TTL_SECONDS = 30 * 60


class Assignments:
    def __init__(self, ttl: float = TTL_SECONDS, clock: Callable[[], float] = time.time) -> None:
        self._ttl, self._clock = ttl, clock
        self._items: dict[tuple[str, str], dict[str, float]] = {}   # (agent, user) -> {pid: exp}
        self._lock = threading.Lock()

    def assign(self, agent: str, user: str, patient_id: str) -> None:
        with self._lock:
            self._items.setdefault((agent, user), {})[patient_id] = self._clock() + self._ttl

    def release(self, agent: str, user: str, patient_id: str) -> None:
        with self._lock:
            self._items.get((agent, user), {}).pop(patient_id, None)

    def patients_for(self, agent: str, user: str) -> list[str]:
        now = self._clock()
        with self._lock:
            current = {p: exp for p, exp in self._items.get((agent, user), {}).items()
                       if exp > now}
            self._items[(agent, user)] = current
            return sorted(current)

    def reset(self) -> None:
        with self._lock:
            self._items.clear()


def is_bound(client: str) -> bool:
    """Agents are bound to their task's patient; people (the hospital app) are not."""
    return client.endswith("-agent")
