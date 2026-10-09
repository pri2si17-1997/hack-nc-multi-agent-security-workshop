"""The language model behind the agents: Strands Agents on AWS Bedrock, Ollama as fallback.

    settings.py   which provider and model (env: LLM_PROVIDER, BEDROCK_MODEL_ID, ...)
    providers.py  build Strands model objects; Bedrock public or VPC endpoint, local Ollama
    runner.py     run a Strands agent, falling back provider by provider, then to rules
    guards.py     guardrails around the model: patient pinning, facts from the gateway,
                  output checks

The model writes and chooses tools. It never holds a permission: every tool call still goes
through Keycloak-scoped tokens, the MCP gateways and OPA.
"""

from hacknc.llm.runner import LlmOutcome, LlmRunner
from hacknc.llm.settings import LlmSettings, get_llm_settings

__all__ = ["LlmOutcome", "LlmRunner", "LlmSettings", "get_llm_settings"]
