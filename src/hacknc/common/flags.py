"""Demo feature flags, owned by the admin console and read by every service.

Flat keys, all booleans::

    agent.<name>          an agent takes part in discharges            (default on)
    partner.<name>        an outside organization is reachable         (default on)
    scenario.<name>       an attack / failure is staged                (default off)
    break_glass_enabled   emergency access may be requested            (default off)
    ai.model              agents may use the language model            (default on; off: rules)

They are stored in the approval service (``/admin/flags``) and read through ``FlagReader``,
which caches for a second so a busy run doesn't hammer the store.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

AGENTS = ("medrec-agent", "summary-agent", "prescription-agent", "benefits-agent",
          "scheduling-agent", "transitions-agent", "instructions-agent", "chart-agent",
          "portal-agent")
PARTNERS = ("payer", "retail-pharmacy", "pcp-clinic", "nc-csrs")
SCENARIOS = {
    "prompt_injection": "A portal message, written like the patient, asks for the full record "
                        "to go to another clinic. No trigger words, no warning: only the "
                        "boundaries stop it (find out afterwards in the incident report)",
    "oversharing": "The benefits agent tries to attach psychiatry records to the prior "
                   "authorization",
    "tampered_prescription": "After the physician signs, the prescription agent raises the "
                             "oxycodone quantity",
    "rogue_agent": "The scheduling agent tries to read clinical notes it has no need for",
    "wrong_patient": "The benefits agent also tries to read another patient's insurance",
}

# The attack style behind each scenario, for the audit trail's 🎭 note (a workshop label: the
# boundaries that stop the attack never see it).
ATTACK_STYLES: dict[str, tuple[str, str]] = {
    "prompt_injection": ("Prompt injection", "instructions hidden in data the agent reads (a "
                         "portal message in the patient's voice) steer it to leak the record"),
    "oversharing": ("Over-disclosure", "the agent sends more than the purpose needs "
                    "(psychiatry notes on a prior authorization)"),
    "tampered_prescription": ("Tampering after approval", "the agent changes what the "
                              "physician signed before sending it (approve-then-swap)"),
    "rogue_agent": ("Privilege misuse", "a compromised agent reaches for data outside its job"),
    "wrong_patient": ("Cross-patient access", "the agent uses a valid token for its own task "
                      "to read another patient"),
}

DEFAULT_FLAGS: dict[str, bool] = {
    **{f"agent.{a}": True for a in AGENTS},
    **{f"partner.{p}": True for p in PARTNERS},
    **{f"scenario.{s}": False for s in SCENARIOS},
    "break_glass_enabled": False,
    "ai.model": True,
}


class FlagReader:
    """Reads flags through ``fetch`` (returns a dict) with a short cache; defaults on failure."""

    def __init__(self, fetch: Callable[[], dict[str, bool] | None], ttl: float = 1.0) -> None:
        self._fetch = fetch
        self._ttl = ttl
        self._cache: dict[str, bool] = dict(DEFAULT_FLAGS)
        self._at = 0.0
        self._lock = threading.Lock()

    def all(self) -> dict[str, bool]:
        with self._lock:
            if time.monotonic() - self._at > self._ttl:
                fetched = self._fetch()
                if fetched is not None:
                    self._cache = {**DEFAULT_FLAGS, **fetched}
                self._at = time.monotonic()
            return dict(self._cache)

    def on(self, key: str) -> bool:
        return self.all().get(key, DEFAULT_FLAGS.get(key, False))


def remote_flags(approvals, admin_key: str) -> FlagReader:
    """A ``FlagReader`` backed by the approval service's ``/admin/flags``."""

    def fetch() -> dict[str, bool] | None:
        result = approvals.get_flags(admin_key)
        return result.body if result.ok else None

    return FlagReader(fetch)
