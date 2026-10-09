"""Guardrails the agent code puts around the model. None of them is the security boundary
(that's Keycloak + the gateways + OPA); they make the model's mistakes less likely and visible.

* ``PinPatient``      a Strands hook: tool calls about any other patient are cancelled
* ``GatewayFacts``    a Strands hook: records what the gateway withheld or refused, so the
                      agent reports it itself instead of trusting the model to (models get
                      this wrong: they "see" a PSY label and claim a record was withheld)
* ``check_output``    text the model wrote is checked before anyone sees it
"""

from __future__ import annotations

import re
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
URL = re.compile(r"https?://|www\.", re.IGNORECASE)
LEAKY = re.compile(r"ignore (all|previous|prior)|system prompt|as an ai\b", re.IGNORECASE)


class PinPatient(HookProvider):
    """The patient is chosen by the clinician, not the model: every tool call must be about
    ``patient_id``. Anything else is cancelled before it reaches the gateway."""

    def __init__(self, patient_id: str, exempt: frozenset[str] = frozenset()) -> None:
        self.patient_id = patient_id
        self.exempt = exempt          # the agent's own local tools (e.g. record_decision)
        self.cancelled: list[str] = []

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        args = event.tool_use.get("input")
        # A model can send malformed input (not an object): that is "no patient", never a crash.
        asked = args.get("patient_id") if isinstance(args, dict) else None
        if asked == self.patient_id or event.tool_use.get("name") in self.exempt:
            return
        name = event.tool_use.get("name", "?")
        reason = (f"{name} refused by the agent: this conversation is about patient "
                  f"{self.patient_id} only (asked for {asked or 'no patient'})")
        event.cancel_tool = reason
        self.cancelled.append(reason)   # the runner reports these on its span


class GatewayFacts(HookProvider):
    """Collects, from the actual tool results: records withheld and calls refused.

    The gateway names withheld records by type and reason only (no ids). A hook must never
    raise: Strands would turn the exception into the tool's result, and the model would only
    see an error."""

    def __init__(self) -> None:
        self._withheld: dict[str, list[dict[str, Any]]] = {}   # per tool: repeat calls count once
        self.refused: list[str] = []

    @property
    def withheld(self) -> list[dict[str, Any]]:
        return [w for items in self._withheld.values() for w in items]

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self.collect)

    def collect(self, event: AfterToolCallEvent) -> None:
        try:
            self._collect(event.tool_use.get("name", "?"), event.result)
        except Exception:  # noqa: BLE001 - see class docstring
            pass

    def _collect(self, tool: str, result: Any) -> None:
        if not isinstance(result, dict):
            return
        if result.get("status") == "error":
            text = " ".join(c.get("text", "") for c in result.get("content", []))
            if "DENIED" in text and not any(r.startswith(f"{tool}:") for r in self.refused):
                self.refused.append(f"{tool}: {text.strip()}")
            return
        withheld = (result.get("structuredContent") or {}).get("withheld") or []
        if withheld:
            self._withheld[tool] = list(withheld)


def check_output(text: str) -> list[str]:
    """Problems with model-written text meant for a patient or another clinician."""
    problems = []
    if EMAIL.search(text):
        problems.append("contains an email address")
    if URL.search(text):
        problems.append("contains a link")
    if LEAKY.search(text):
        problems.append("talks about its own instructions")
    if not text.strip():
        problems.append("is empty")
    return problems
