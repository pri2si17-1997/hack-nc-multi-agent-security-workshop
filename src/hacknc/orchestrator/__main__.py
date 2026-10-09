"""Run the discharge coordinator: ``python -m hacknc.orchestrator`` (port 8001)."""

from __future__ import annotations

import os

import uvicorn

from hacknc.agents.servers import BUILDERS
from hacknc.approval_service.client import ApprovalApi
from hacknc.audit_service.client import AuditApi
from hacknc.common.audit import AuditClient
from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.common.flags import remote_flags
from hacknc.common.telemetry import setup_tracing
from hacknc.llm import LlmRunner, get_llm_settings
from hacknc.orchestrator.app import NAME, OrchestratorDeps, create_app
from hacknc.orchestrator.watch import AlertWatch, TaskAssigner


def main() -> None:
    settings = get_settings()
    setup_tracing(NAME, settings.otel_exporter_otlp_endpoint)
    kc = KeycloakClient(settings.keycloak_url)
    admin_key = os.environ.get("DEMO_ADMIN_KEY", "")
    approvals = ApprovalApi(os.environ.get("APPROVAL_URL", "http://approval-service:8007"))
    flags = remote_flags(approvals)
    ehr_url = os.environ.get("EHR_MCP_URL", "http://ehr-mcp:8010/mcp")
    audit_url = os.environ.get("AUDIT_URL", "http://audit-service:8006")
    deps = OrchestratorDeps(
        keycloak=kc,
        secrets=client_secret,
        # Each agent is reachable at http://<name>:<port>/mcp inside Docker.
        agent_urls={name: f"http://{name}:{port}/mcp" for name, (_, port) in BUILDERS.items()},
        ehr_url=os.environ.get("EHR_MCP_URL", "http://ehr-mcp:8010/mcp"),
        audit=AuditClient(os.environ.get("AUDIT_URL", "http://audit-service:8006"),
                          ServiceTokenProvider(kc, NAME, client_secret(NAME), "audit-service",
                                               ["audit:write"])),
        flags=flags,
        admin_key=admin_key,
        llm=LlmRunner(get_llm_settings(), enabled=lambda: flags.on("ai.model")),
        assigner=TaskAssigner(ehr_url, ServiceTokenProvider(
            kc, NAME, client_secret(NAME), "ehr-mcp", ["task:assign"])),
        watch=AlertWatch(AuditApi(audit_url), ServiceTokenProvider(
            kc, NAME, client_secret(NAME), "audit-service", ["audit:read"]).token),
    )
    auth = BearerAuth(JwtValidator(settings.keycloak_issuer, NAME, jwks_uri=kc.jwks_uri))
    uvicorn.run(create_app(deps, auth), host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
