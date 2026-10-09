"""The model runner: provider order, fallback, rules as the last resort. Strands' Agent is faked."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from hacknc.llm import runner as runner_module
from hacknc.llm.providers import ModelChoice, friendly_model_name, reasoning_fields
from hacknc.llm.runner import LlmRunner
from hacknc.llm.settings import LlmSettings

pytestmark = pytest.mark.anyio


class FakeAgent:
    """Stands in for ``strands.Agent``: answers, or fails, depending on the model."""

    created: list[dict] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        FakeAgent.created.append(kwargs)

    async def invoke_async(self, prompt):
        model = self.kwargs["model"]
        if isinstance(model, Exception):
            raise model
        metrics = SimpleNamespace(accumulated_usage={"inputTokens": 120, "outputTokens": 30},
                                  tool_metrics={"get_allergies": object()})
        return _Result(model, metrics)


class _Result:
    def __init__(self, text, metrics) -> None:
        self.text, self.metrics = text, metrics

    def __str__(self) -> str:
        return self.text


def choice(provider, model):
    return lambda settings: ModelChoice(provider, f"{provider}-id", f"{provider} label",
                                        "public", model)


@pytest.fixture(autouse=True)
def fake_agent(monkeypatch):
    FakeAgent.created = []
    monkeypatch.setattr(runner_module, "Agent", FakeAgent)


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    original = runner_module.tracer
    runner_module.tracer = tracer
    yield exporter
    runner_module.tracer = original


def make(settings=None, *, bedrock="Bedrock says hi", ollama="Ollama says hi",
         ollama_ready=None, enabled=True):
    return LlmRunner(settings or LlmSettings(llm_provider="bedrock", llm_fallback="ollama"),
                     enabled=lambda: enabled,
                     builders={"bedrock": choice("bedrock", bedrock),
                               "ollama": choice("ollama", ollama)},
                     ollama_ready=lambda s: ollama_ready)


async def run(r, **kw):
    return await r.run(agent="chart-agent", task="answer", system="s", prompt="p", **kw)


async def test_first_provider_answers(spans) -> None:
    outcome = await run(make())
    assert outcome.text == "Bedrock says hi" and outcome.provider == "bedrock"
    assert outcome.tools_used == ["get_allergies"] and outcome.input_tokens == 120
    span = spans.get_finished_spans()[-1]
    assert span.name == "llm chart-agent: answer"
    assert span.attributes["llm.used_model"] is True
    assert span.attributes["gen_ai.usage.output_tokens"] == 30
    assert FakeAgent.created[0]["retry_strategy"] is None   # fail fast, then fall back


async def test_falls_back_to_ollama_when_bedrock_fails(spans) -> None:
    outcome = await run(make(bedrock=RuntimeError("ExpiredToken: session expired")))
    assert outcome.provider == "ollama" and outcome.text == "Ollama says hi"
    assert "ExpiredToken" in outcome.skipped[0]
    events = [e.name for e in spans.get_finished_spans()[-1].events]
    assert events == ["llm.fallback"]


async def test_ollama_without_the_model_is_skipped_without_trying() -> None:
    outcome = await run(make(bedrock=RuntimeError("no credentials"),
                             ollama_ready="Ollama has no qwen2.5:7b"))
    assert outcome.text is None and outcome.provider == "rules"
    assert outcome.skipped[-1] == "Ollama has no qwen2.5:7b"
    assert len(FakeAgent.created) == 1                  # only Bedrock was tried
    assert outcome.byline() == ("⚙ Written by rules (bedrock label: no credentials; "
                                "Ollama has no qwen2.5:7b)")


async def test_admin_switch_means_rules_without_calling_a_model() -> None:
    outcome = await run(make(enabled=False))
    assert outcome.text is None and FakeAgent.created == []
    assert "switched off by the admin" in outcome.byline()


async def test_rules_only_configuration() -> None:
    outcome = await run(make(LlmSettings(llm_provider="rules", llm_fallback="")))
    assert outcome.text is None and outcome.skipped == ["LLM_PROVIDER=rules"]


async def test_guardrail_cancellations_are_reported_on_the_span(spans) -> None:
    hook = SimpleNamespace(cancelled=["get_allergies refused by the agent: ..."])
    await run(make(), hooks=[hook])
    events = spans.get_finished_spans()[-1].events
    assert [e.name for e in events] == ["agent.guardrail"]


def test_provider_chain_and_names() -> None:
    assert LlmSettings(llm_provider="bedrock", llm_fallback="ollama").chain() == \
        ["bedrock", "ollama"]
    assert LlmSettings(llm_provider="ollama", llm_fallback="ollama,bedrock").chain() == \
        ["ollama", "bedrock"]
    assert LlmSettings(llm_provider="bedrock", llm_fallback="rules,ollama").chain() == \
        ["bedrock"]
    assert friendly_model_name("us.meta.llama4-scout-17b-instruct-v1:0") == "Llama 4 Scout"
    assert friendly_model_name("openai.gpt-oss-20b-1:0") == "gpt-oss-20b"
    assert friendly_model_name("us.anthropic.claude-haiku-4-5-20251001-v1:0") == "Claude Haiku"


async def test_a_failed_provider_is_rebuilt_on_the_next_run() -> None:
    built = []

    def bedrock(settings):
        built.append(1)
        model = RuntimeError("expired") if len(built) == 1 else "fresh session works"
        return ModelChoice("bedrock", "id", "label", "public", model)

    r = LlmRunner(LlmSettings(llm_provider="bedrock", llm_fallback=""),
                  builders={"bedrock": bedrock}, ollama_ready=lambda s: None)
    assert (await run(r)).text is None
    assert (await run(r)).text == "fresh session works" and len(built) == 2


def test_reasoning_effort_only_for_gpt_oss() -> None:
    assert reasoning_fields(LlmSettings(bedrock_model_id="openai.gpt-oss-20b-1:0")) == \
        {"reasoning_effort": "low"}
    assert reasoning_fields(LlmSettings(bedrock_model_id="us.meta.llama4-scout-17b")) is None
    assert reasoning_fields(LlmSettings(llm_reasoning_effort="")) is None


def test_tool_inputs_with_empty_keys_are_cleaned_for_bedrock() -> None:
    # gpt-oss calls argument-less tools with {"": ""}; Bedrock rejects it on the next turn.
    from hacknc.llm.providers import clean_tool_input

    assert clean_tool_input({"": " "}) == {}
    assert clean_tool_input({"decision": {"": "", "ok": [{" ": 1, "a": 2}]}}) == \
        {"decision": {"ok": [{"a": 2}]}}


def test_models_lists_configured_ids_in_order() -> None:
    settings = LlmSettings(llm_provider="bedrock", llm_fallback="ollama",
                           bedrock_model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
                           ollama_model="qwen2.5:7b")
    assert settings.models() == \
        "us.anthropic.claude-haiku-4-5-20251001-v1:0 (Bedrock), then qwen2.5:7b (Ollama)"
    assert LlmSettings(llm_provider="rules", llm_fallback="").models() == "none (rules only)"
