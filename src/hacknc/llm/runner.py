"""Run a Strands agent on the first provider that works; fall back to the next, then to rules.

    outcome = await runner.run(agent="chart-agent", task="answer", system=..., prompt=...,
                               tools=mcp.list_tools_sync(), hooks=[PinPatient(pid)])
    if outcome.text is None:      # every provider failed or the admin switched AI off
        ...deterministic rules...

Each run is one span, ``llm <agent>: <task>``, with the provider, model, endpoint, token usage,
the tools the model chose and why earlier providers were skipped. Prompts and answers are not
recorded (they contain PHI): Strands redacts them (OTEL_SEMCONV_STABILITY_OPT_IN) and the
Collector drops them again.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
from opentelemetry import trace
from strands import Agent

from hacknc.llm import providers
from hacknc.llm.settings import LlmSettings

tracer = trace.get_tracer(__name__)
RULES = "rules"


@dataclass
class LlmOutcome:
    text: str | None
    provider: str = RULES
    label: str = "deterministic rules"
    tools_used: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # why earlier providers weren't used
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def used_model(self) -> bool:
        return self.text is not None

    def byline(self) -> str:
        """One line for the UI: who wrote this."""
        if self.used_model:
            return f"✨ Written by {self.label}"
        reason = "; ".join(self.skipped) or "AI model not configured"
        return f"⚙ Written by rules ({reason})"


class LlmRunner:
    def __init__(self, settings: LlmSettings, enabled: Callable[[], bool] = lambda: True,
                 builders: dict[str, Callable] | None = None,
                 ollama_ready: Callable[[LlmSettings], str | None] = providers.ollama_ready
                 ) -> None:
        self.settings = settings
        self.enabled = enabled
        self.builders = builders or providers.BUILDERS
        self.ollama_ready = ollama_ready
        self._choices: dict[str, providers.ModelChoice] = {}

    def _choice(self, provider: str) -> providers.ModelChoice:
        if provider not in self._choices:
            self._choices[provider] = self.builders[provider](self.settings)
        return self._choices[provider]

    async def run(self, *, agent: str, task: str, system: str, prompt: str,
                  tools: list[Any] | None = None, hooks: list[Any] | None = None,
                  timeout: float | None = None) -> LlmOutcome:
        """``timeout``: for the whole agent loop (default: twice the per-request timeout).
        The coordinator passes a longer one: its tools are other agents."""
        with tracer.start_as_current_span(f"llm {agent}: {task}") as span:
            span.set_attributes({"agent.id": agent, "llm.task": task})
            outcome = await self._run(span, agent, system, prompt, tools or [], hooks or [],
                                      timeout or self.settings.llm_timeout_seconds * 2)
            for hook in hooks or []:
                for reason in getattr(hook, "cancelled", []):
                    span.add_event("agent.guardrail", {"detail": reason})
            span.set_attributes({
                "llm.provider": outcome.provider, "llm.label": outcome.label,
                "llm.used_model": outcome.used_model,
                "llm.tools_used": ",".join(outcome.tools_used),
                "llm.skipped": " | ".join(outcome.skipped),
                "gen_ai.usage.input_tokens": outcome.input_tokens,
                "gen_ai.usage.output_tokens": outcome.output_tokens})
            return outcome

    async def _run(self, span, agent: str, system: str, prompt: str, tools: list[Any],
                   hooks: list[Any], timeout: float) -> LlmOutcome:
        skipped: list[str] = []
        if not self.enabled():
            return LlmOutcome(None, skipped=["AI model switched off by the admin"])
        for provider in self.settings.chain():
            if provider == "ollama" and (why := self.ollama_ready(self.settings)):
                skipped.append(why)
                continue
            try:
                choice = self._choice(provider)
            except Exception as exc:  # noqa: BLE001 - misconfiguration: try the next one
                skipped.append(f"{provider}: {_short(exc)}")
                continue
            started = time.monotonic()
            try:
                with anyio.fail_after(timeout):
                    result = await Agent(
                        model=choice.model, system_prompt=system, tools=list(tools),
                        hooks=list(hooks), callback_handler=None, retry_strategy=None,
                        name=agent).invoke_async(prompt)
            except TimeoutError:
                skipped.append(f"{choice.label}: no answer within {timeout:.0f} s")
                span.add_event("llm.fallback", {"provider": provider, "reason": skipped[-1]})
                continue
            except Exception as exc:  # noqa: BLE001 - any model failure: fall back
                # Rebuild next time: a fresh AWS session picks up a new `aws login`.
                self._choices.pop(provider, None)
                skipped.append(f"{choice.label}: {_short(exc)}")
                span.add_event("llm.fallback", {"provider": provider, "reason": skipped[-1]})
                continue
            usage = result.metrics.accumulated_usage
            span.set_attributes({"llm.model": choice.model_id, "llm.endpoint": choice.endpoint,
                                 "llm.seconds": round(time.monotonic() - started, 2)})
            return LlmOutcome(str(result).strip(), provider, choice.label,
                              list(result.metrics.tool_metrics), skipped,
                              usage.get("inputTokens", 0), usage.get("outputTokens", 0))
        if not self.settings.chain():
            skipped.append("LLM_PROVIDER=rules")
        return LlmOutcome(None, skipped=skipped)


def _short(exc: Exception, limit: int = 160) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"
