"""Run one agent: ``python -m hacknc.agents <name>`` (see ``servers.BUILDERS`` for names)."""

from __future__ import annotations

import os
import sys

from hacknc.agents.servers import BUILDERS, LLM_AGENTS, AgentDeps
from hacknc.approval_service.client import ApprovalApi
from hacknc.common.audit import AuditClient
from hacknc.common.auth import KeycloakClient
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.common.flags import remote_flags
from hacknc.common.mcp_server import auth_kwargs, serve
from hacknc.common.telemetry import setup_tracing
from hacknc.llm import LlmRunner, get_llm_settings


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else ""
    if name not in BUILDERS:
        raise SystemExit(f"usage: python -m hacknc.agents {{{','.join(BUILDERS)}}}")
    build, port = BUILDERS[name]
    settings = get_settings()
    setup_tracing(name, settings.otel_exporter_otlp_endpoint)
    kc = KeycloakClient(settings.keycloak_url)
    approvals = ApprovalApi(os.environ.get("APPROVAL_URL", "http://approval-service:8007"))
    flags = remote_flags(approvals)
    deps = AgentDeps(
        keycloak=kc,
        secrets=client_secret,
        ehr_url=os.environ.get("EHR_MCP_URL", "http://ehr-mcp:8010/mcp"),
        disclosure_url=os.environ.get("DISCLOSURE_MCP_URL", "http://disclosure-mcp:8011/mcp"),
        flags=flags,
        # Agents that decide or write get a model; the rest (sending signed items) don't.
        llm=LlmRunner(get_llm_settings(), enabled=lambda: flags.on("ai.model"))
        if name in LLM_AGENTS else None,
        # Each agent writes its own decisions to the audit trail, as itself.
        audit=AuditClient(os.environ.get("AUDIT_URL", "http://audit-service:8006"),
                          ServiceTokenProvider(kc, name, client_secret(name), "audit-service",
                                               ["audit:write"])),
    )
    public_url = os.environ.get("AGENT_PUBLIC_URL", f"http://localhost:{port}")
    server = build(deps, **auth_kwargs(name, settings.keycloak_issuer, kc, public_url))
    serve(server, name, port)


if __name__ == "__main__":
    main()
