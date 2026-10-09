"""Run the audit service: ``python -m hacknc.audit_service`` (port 8006)."""

from __future__ import annotations

import os

import uvicorn

from hacknc.audit_service.app import create_app
from hacknc.audit_service.store import AuditStore
from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.auth.fastapi import BearerAuth
from hacknc.common.config import get_settings
from hacknc.common.telemetry import setup_log_export, setup_tracing

SERVICE = "audit-service"


def main() -> None:
    settings = get_settings()
    setup_tracing(SERVICE, settings.otel_exporter_otlp_endpoint)
    setup_log_export(SERVICE, settings.otel_exporter_otlp_endpoint, "hacknc.audit")
    validator = JwtValidator(settings.keycloak_issuer, SERVICE,
                             jwks_uri=KeycloakClient(settings.keycloak_url).jwks_uri)
    store = AuditStore(os.environ.get("AUDIT_DB_PATH", "/data/audit.db"))
    app = create_app(store, BearerAuth(validator), os.environ.get("OPA_LOG_TOKEN", ""))
    uvicorn.run(app, host="0.0.0.0", port=8006)


if __name__ == "__main__":
    main()
