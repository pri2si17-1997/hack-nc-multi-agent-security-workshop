"""Run the approval service: ``python -m hacknc.approval_service`` (port 8007)."""

from __future__ import annotations

import os

import uvicorn

from hacknc.approval_service.app import ApprovalSettings, create_app
from hacknc.approval_service.store import ApprovalStore
from hacknc.common.audit import AuditClient
from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.common.policy import OpaClient
from hacknc.common.telemetry import setup_tracing

SERVICE = "approval-service"


def main() -> None:
    settings = get_settings()
    setup_tracing(SERVICE, settings.otel_exporter_otlp_endpoint)
    kc = KeycloakClient(settings.keycloak_url)
    validator = JwtValidator(settings.keycloak_issuer, SERVICE, jwks_uri=kc.jwks_uri)
    audit_tokens = ServiceTokenProvider(kc, SERVICE, client_secret(SERVICE), "audit-service",
                                        ["audit:write"])
    app = create_app(
        store=ApprovalStore(os.environ.get("APPROVAL_DB_PATH", "/data/approvals.db")),
        auth=BearerAuth(validator),
        opa=OpaClient(os.environ.get("OPA_URL", "http://opa:8181")),
        audit=AuditClient(os.environ.get("AUDIT_URL", "http://audit-service:8006"),
                          audit_tokens),
        settings=ApprovalSettings(
            approval_ttl_seconds=int(os.environ.get("APPROVAL_TTL_SECONDS", "600")),
            break_glass_ttl_seconds=int(os.environ.get("BREAK_GLASS_TTL_SECONDS", "900")),
            demo_admin_key=os.environ.get("DEMO_ADMIN_KEY", ""),
        ),
    )
    uvicorn.run(app, host="0.0.0.0", port=8007)


if __name__ == "__main__":
    main()
