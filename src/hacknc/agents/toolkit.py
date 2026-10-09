"""What every model-driven agent gets on top of the gateway tools.

* ``DecisionLog``  the local Strands tool ``record_decision(decision, rationale, evidence)``.
                   Every specialist must call it last: it is how the agent's choice comes back
                   to the coordinator as structured data (not prose to be parsed), and it is
                   what the audit trail records as ``agent.decision``.
* ``ToolLog``      a Strands hook that writes down every tool call the model made: what it
                   asked for, whether the gateway answered or refused, and the result. Agent
                   code reads *results* from here, never the model's claims about them.
                   A call to a tool the agent was never given ("not in its toolbox") is
                   recorded as blocked.
* ``VerifyCalls``  a Strands hook that checks a call's arguments *before* it reaches the
                   gateway (e.g. a drafted prescription must match the verified plan).

Hooks never raise: Strands would turn the exception into the tool result, and the model would
only see an error (see ``llm/guards.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from strands import tool
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider, HookRegistry

LOCAL_TOOLS = {"record_decision"}


class DecisionLog:
    """Holds what the agent decided. ``tool`` is handed to the model."""

    def __init__(self) -> None:
        self.decision: dict[str, Any] | None = None
        self.rationale = ""
        self.evidence: list[str] = []

        @tool
        def record_decision(decision: dict[str, Any], rationale: str,
                            evidence: list[str] | None = None) -> str:
            """Record your final decision. Call this exactly once, as your last step.

            Args:
                decision: the structured result your task asks for.
                rationale: one or two sentences: why, in plain words.
                evidence: record ids or short facts the decision is based on.
            """
            self.decision = decision if isinstance(decision, dict) else {"value": decision}
            self.rationale = str(rationale)[:600]
            self.evidence = [str(e)[:160] for e in (evidence or [])][:12]
            return "Decision recorded."

        self.tool = record_decision

    @property
    def made(self) -> bool:
        return self.decision is not None


@dataclass
class Call:
    tool: str
    args: dict[str, Any]
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class ToolLog(HookProvider):
    """Every tool call the model made, with the gateway's actual answer."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.blocked: list[str] = []          # not in the toolbox, cancelled by a check

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def before(self, event: BeforeToolCallEvent) -> None:
        try:
            if event.selected_tool is None:
                name = event.tool_use.get("name", "?")
                self.blocked.append(f"tried {name}: not in its toolbox")
        except Exception:  # noqa: BLE001 - see module docstring
            pass

    def after(self, event: AfterToolCallEvent) -> None:
        try:
            name = event.tool_use.get("name", "?")
            if name in LOCAL_TOOLS or event.selected_tool is None:
                return
            result = event.result if isinstance(event.result, dict) else {}
            if result.get("status") == "error" or event.exception is not None:
                text = " ".join(c.get("text", "") for c in result.get("content", []))
                text = text.strip() or str(event.exception or "failed")
                self.calls.append(Call(name, dict(event.tool_use.get("input") or {}), False,
                                       error=text))
            else:
                self.calls.append(Call(name, dict(event.tool_use.get("input") or {}), True,
                                       result=result.get("structuredContent") or {}))
        except Exception:  # noqa: BLE001
            pass

    def results(self, tool_name: str) -> list[Call]:
        return [c for c in self.calls if c.tool == tool_name and c.ok]

    @property
    def refused(self) -> list[str]:
        return [f"{c.tool}: {c.error}" for c in self.calls if not c.ok]


Check = Callable[[str, dict[str, Any]], str | None]   # (tool, args) -> reason to refuse


class VerifyCalls(HookProvider):
    """Runs ``check`` before every gateway call; a reason cancels the call."""

    def __init__(self, check: Check, log: ToolLog | None = None) -> None:
        self.check, self.log = check, log
        self.cancelled: list[str] = []

    def register_hooks(self, registry: HookRegistry, **_: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.before)

    def before(self, event: BeforeToolCallEvent) -> None:
        try:
            name = event.tool_use.get("name", "?")
            reason = self.check(name, dict(event.tool_use.get("input") or {}))
        except Exception:  # noqa: BLE001
            return
        if reason:
            event.cancel_tool = f"{name} refused by the agent's own check: {reason}"
            self.cancelled.append(event.cancel_tool)
            if self.log is not None:
                self.log.blocked.append(event.cancel_tool)
