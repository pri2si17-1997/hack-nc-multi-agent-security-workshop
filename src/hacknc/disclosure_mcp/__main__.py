"""Run the disclosure gateway: ``python -m hacknc.disclosure_mcp`` (port 8011, path ``/mcp``)."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from hacknc.approval_service.client import ApprovalApi
from hacknc.common.audit import AuditClient
from hacknc.common.auth import KeycloakClient
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.common.flags import remote_flags
from hacknc.common.mcp_server import auth_kwargs, serve
from hacknc.common.policy import OpaClient
from hacknc.common.telemetry import setup_tracing
from hacknc.disclosure_mcp.server import SERVER_NAME, DisclosureDeps, DisclosureMcpServer
from hacknc.ehr.records import EhrRepository
from hacknc.partners.client import PartnerApi

PORT = 8011


def consent_reader(ehr_url: str, tokens: ServiceTokenProvider) -> Callable[[str], dict[str, Any]]:
    """Read a patient's current consent from the EHR gateway. Fails closed: {} = no consent."""
    base = ehr_url.rstrip("/").removesuffix("/mcp")
    http = httpx.Client(timeout=5.0)

    def read(patient_id: str) -> dict[str, Any]:
        try:
            response = http.get(f"{base}/consent/{patient_id}",
                                headers={"Authorization": f"Bearer {tokens.token()}"})
            response.raise_for_status()
            return response.json().get("consent", {})
        except Exception:  # noqa: BLE001 - unknown consent means no consent
            return {}

    return read


def assignment_reader(ehr_url: str, tokens: ServiceTokenProvider
                      ) -> Callable[[str, str], list[str]]:
    """Ask the EHR gateway which patients an agent's current task is about. Fails closed:
    [] (no task for anyone) when it can't be read."""
    base = ehr_url.rstrip("/").removesuffix("/mcp")
    http = httpx.Client(timeout=5.0)

    def read(agent: str, user: str) -> list[str]:
        try:
            response = http.get(f"{base}/tasks", params={"agent": agent, "user": user},
                                headers={"Authorization": f"Bearer {tokens.token()}"})
            response.raise_for_status()
            return list(response.json().get("patients", []))
        except Exception:  # noqa: BLE001 - unknown assignment means no assignment
            return []

    return read


def main() -> None:
    settings = get_settings()
    setup_tracing(SERVER_NAME, settings.otel_exporter_otlp_endpoint)
    kc = KeycloakClient(settings.keycloak_url)
    data_dir = Path(os.environ.get("EHR_DATA_DIR", "data"))
    approvals = ApprovalApi(os.environ.get("APPROVAL_URL", "http://approval-service:8007"))
    ehr_url = os.environ.get("EHR_MCP_URL", "http://ehr-mcp:8010/mcp")
    ehr_tokens = ServiceTokenProvider(kc, SERVER_NAME, client_secret(SERVER_NAME), "ehr-mcp")
    deps = DisclosureDeps(
        repo=EhrRepository.load(data_dir / "curated", data_dir / "overlay"),
        opa=OpaClient(os.environ.get("OPA_URL", "http://opa:8181")),
        keycloak=kc,
        secrets=client_secret,
        partners=PartnerApi(os.environ.get("PARTNERS_URL", "http://partners:8020")),
        approvals=approvals,
        flags=remote_flags(approvals),
        audit=AuditClient(
            os.environ.get("AUDIT_URL", "http://audit-service:8006"),
            ServiceTokenProvider(kc, SERVER_NAME, client_secret(SERVER_NAME), "audit-service",
                                 ["audit:write"])),
        consent_source=consent_reader(ehr_url, ehr_tokens),
        assignment_source=assignment_reader(ehr_url, ehr_tokens),
    )
    public_url = os.environ.get("DISCLOSURE_MCP_PUBLIC_URL", f"http://localhost:{PORT}")
    server = DisclosureMcpServer(deps, **auth_kwargs(SERVER_NAME, settings.keycloak_issuer, kc,
                                                     public_url))
    serve(server, SERVER_NAME, PORT)


if __name__ == "__main__":
    main()
