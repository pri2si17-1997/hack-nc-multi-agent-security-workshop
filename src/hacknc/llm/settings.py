"""LLM settings, read from the environment (and ``.env``).

    LLM_PROVIDER          bedrock | ollama | rules        first choice
    LLM_FALLBACK          comma list, e.g. "ollama"       tried next; rules are always last
    LLM_TIMEOUT_SECONDS   per model call
    LLM_MAX_TOKENS        output limit per call (reasoning models spend part of it thinking)
    LLM_REASONING_EFFORT  low | medium | high, for reasoning models (gpt-oss); empty: default
    AWS_PROFILE           profile in ~/.aws (mounted read-only into the LLM agents)
    AWS_REGION            empty: the profile's region (else us-east-1)
    BEDROCK_MODEL_ID      model id or inference profile
    BEDROCK_ENDPOINT_URL  empty: public regional endpoint; set: VPC interface endpoint
    OLLAMA_BASE_URL / OLLAMA_MODEL
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["bedrock", "ollama", "rules"]


class LlmSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    llm_provider: Provider = "bedrock"
    llm_fallback: str = "ollama"
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 4000
    llm_reasoning_effort: str = "low"
    aws_profile: str = "default"
    aws_region: str = ""
    bedrock_model_id: str = "openai.gpt-oss-20b-1:0"
    bedrock_endpoint_url: str = ""
    ollama_base_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen2.5:7b"

    def chain(self) -> list[Provider]:
        """Providers to try in order (without duplicates); ``rules`` ends the chain."""
        names = [self.llm_provider, *(p.strip() for p in self.llm_fallback.split(","))]
        chain: list[Provider] = []
        for name in names:
            if name == "rules":
                break
            if name in ("bedrock", "ollama") and name not in chain:
                chain.append(name)
        return chain

    def models(self) -> str:
        """The configured model ids in the order they are tried, for the UI."""
        names = {"bedrock": f"{self.bedrock_model_id} (Bedrock)",
                 "ollama": f"{self.ollama_model} (Ollama)"}
        return ", then ".join(names[p] for p in self.chain()) or "none (rules only)"


@lru_cache
def get_llm_settings() -> LlmSettings:
    load_dotenv(override=False)
    return LlmSettings()
