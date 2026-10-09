"""Strands model objects for each provider.

Bedrock uses the AWS profile from ``~/.aws`` (``aws login`` / SSO / keys all work) and the
Converse API, non-streaming (not every Bedrock model streams tool calls). An endpoint URL
switches from the public regional endpoint to a VPC interface endpoint (PrivateLink).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from strands.models import Model

from hacknc.llm.settings import LlmSettings, Provider

DEFAULT_REGION = "us-east-1"


@dataclass(frozen=True)
class ModelChoice:
    provider: Provider
    model_id: str
    label: str          # shown to students, e.g. "gpt-oss-20b on Bedrock (us-east-1, public …)"
    endpoint: str       # "public" | "vpc" | "local"
    model: Model


def friendly_model_name(model_id: str) -> str:
    """'openai.gpt-oss-20b-1:0' -> 'gpt-oss-20b'."""
    known = {"gpt-oss-120b": "gpt-oss-120b", "gpt-oss-20b": "gpt-oss-20b",
             "llama4-scout": "Llama 4 Scout", "llama4-maverick": "Llama 4 Maverick",
             "claude-haiku": "Claude Haiku", "claude-sonnet": "Claude Sonnet",
             "claude-opus": "Claude Opus", "claude": "Claude",
             "nova": "Amazon Nova", "qwen": "Qwen", "llama3": "Llama 3"}
    lowered = model_id.lower()
    return next((name for key, name in known.items() if key in lowered), model_id)


def reasoning_fields(settings: LlmSettings) -> dict[str, str] | None:
    """gpt-oss thinks before answering; "low" effort keeps the demo fast."""
    if settings.llm_reasoning_effort and "gpt-oss" in settings.bedrock_model_id:
        return {"reasoning_effort": settings.llm_reasoning_effort}
    return None


def clean_tool_input(value: Any) -> Any:
    """Drop keys with empty names, at any depth.

    gpt-oss on Bedrock calls a tool that takes no arguments with ``{"": ""}``. The call itself
    works, but Bedrock rejects that input when the conversation is sent back on the next turn
    ("toolUse.input is invalid. Provide a json object")."""
    if isinstance(value, dict):
        return {k: clean_tool_input(v) for k, v in value.items() if str(k).strip()}
    if isinstance(value, list):
        return [clean_tool_input(v) for v in value]
    return value


def bedrock(settings: LlmSettings) -> ModelChoice:
    import boto3
    from botocore.config import Config
    from strands.models.bedrock import BedrockModel

    class CleanToolInputBedrockModel(BedrockModel):
        """Bedrock, with tool inputs cleaned before every request (see ``clean_tool_input``)."""

        def _format_request_message_content(self, content):
            formatted = super()._format_request_message_content(content)
            if formatted and "toolUse" in formatted:
                formatted["toolUse"]["input"] = clean_tool_input(formatted["toolUse"]["input"])
            return formatted

    session = boto3.Session(profile_name=settings.aws_profile or None,
                            region_name=settings.aws_region or None)
    if session.region_name is None:
        session = boto3.Session(profile_name=settings.aws_profile or None,
                                region_name=DEFAULT_REGION)
    model = CleanToolInputBedrockModel(
        boto_session=session,
        boto_client_config=Config(connect_timeout=5, read_timeout=settings.llm_timeout_seconds,
                                  # adaptive: backs off on Bedrock throttling (low on-demand quotas)
                                  retries={"max_attempts": 4, "mode": "adaptive"}),
        endpoint_url=settings.bedrock_endpoint_url or None,
        model_id=settings.bedrock_model_id, max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature, streaming=False,
        additional_request_fields=reasoning_fields(settings))
    endpoint = "vpc" if settings.bedrock_endpoint_url else "public"
    return ModelChoice("bedrock", settings.bedrock_model_id,
                       f"{friendly_model_name(settings.bedrock_model_id)} on Bedrock "
                       f"({session.region_name}, {endpoint} endpoint)", endpoint, model)


def ollama(settings: LlmSettings) -> ModelChoice:
    from strands.models.ollama import OllamaModel

    model = OllamaModel(settings.ollama_base_url, model_id=settings.ollama_model,
                        max_tokens=settings.llm_max_tokens,
                        temperature=settings.llm_temperature,
                        ollama_client_args={"timeout": settings.llm_timeout_seconds})
    return ModelChoice("ollama", settings.ollama_model,
                       f"{friendly_model_name(settings.ollama_model)} on Ollama (local)",
                       "local", model)


def ollama_ready(settings: LlmSettings) -> str | None:
    """None if Ollama runs and has the model; otherwise why not (checked before trying)."""
    try:
        tags = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        return f"Ollama not reachable at {settings.ollama_base_url}"
    names = {m.get("name") for m in tags.get("models", [])}
    if settings.ollama_model not in names:
        return f"Ollama has no {settings.ollama_model} (ollama pull {settings.ollama_model})"
    return None


BUILDERS = {"bedrock": bedrock, "ollama": ollama}
