"""Run ehr-mcp: ``python -m hacknc.ehr_mcp`` (Streamable HTTP on port 8010, path ``/mcp``).

The MCP SDK acts as an OAuth 2.1 *resource server*: requests without a valid Keycloak token
addressed to ``ehr-mcp`` get ``401`` plus a pointer to
``/.well-known/oauth-protected-resource``, which names Keycloak as the authorization server.
"""

from __future__ import annotations

import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from hacknc.approval_service.client import ApprovalApi
from hacknc.common.audit import AuditClient
from hacknc.common.auth import JwtValidator, KeycloakClient, TokenValidationError
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import client_secret, get_settings
from hacknc.common.flags import remote_flags
from hacknc.common.mcp_server import auth_kwargs, serve
from hacknc.common.policy import OpaClient
from hacknc.common.telemetry import setup_tracing
from hacknc.ehr.records import EhrRepository
from hacknc.ehr_mcp.gate import EhrGate
from hacknc.ehr_mcp.server import SERVER_NAME, EhrDependencies, EhrMcpServer

PORT = 8010


def build_server() -> EhrMcpServer:
    settings = get_settings()
    kc = KeycloakClient(settings.keycloak_url)
    data_dir = Path(os.environ.get("EHR_DATA_DIR", "data"))
    public_url = os.environ.get("EHR_MCP_PUBLIC_URL", f"http://localhost:{PORT}")
    approvals = ApprovalApi(os.environ.get("APPROVAL_URL", "http://approval-service:8007"))
    deps = EhrDependencies(
        repo=EhrRepository.load(data_dir / "curated", data_dir / "overlay"),
        gate=EhrGate(OpaClient(os.environ.get("OPA_URL", "http://opa:8181"))),
        keycloak=kc,
        secrets=client_secret,
        approvals=approvals,
        flags=remote_flags(approvals),
        audit=AuditClient(
            os.environ.get("AUDIT_URL", "http://audit-service:8006"),
            ServiceTokenProvider(kc, SERVER_NAME, client_secret(SERVER_NAME), "audit-service",
                                 ["audit:write"])),
    )
    server = EhrMcpServer(deps, **auth_kwargs(SERVER_NAME, settings.keycloak_issuer, kc,
                                              public_url))
    admin_key = os.environ.get("DEMO_ADMIN_KEY", "")

    @server.custom_route("/admin/reset", methods=["POST"])
    async def reset(request: Request) -> JSONResponse:
        """Demo reset: forget posted patient instructions (admin key required)."""
        if not admin_key or request.headers.get("x-demo-admin-key") != admin_key:
            return JSONResponse({"detail": "demo admin key required"}, status_code=401)
        deps.repo.reset_runtime_state()
        deps.assignments.reset()
        return JSONResponse({"status": "reset"})

    validator = JwtValidator(settings.keycloak_issuer, SERVER_NAME, jwks_uri=kc.jwks_uri)

    @server.custom_route("/consent/{patient_id}", methods=["GET"])
    async def consent(request: Request) -> JSONResponse:
        """The patient's current consent, for the disclosure gateway (its own token)."""
        return consent_for(deps, validator, request.headers.get("authorization", ""),
                           request.path_params["patient_id"])

    @server.custom_route("/tasks/assign", methods=["POST"])
    async def assign(request: Request) -> JSONResponse:
        """The coordinator binds an agent (acting for a user) to its task's patient."""
        return assign_task(deps, validator, request.headers.get("authorization", ""),
                           await request.json())

    @server.custom_route("/tasks", methods=["GET"])
    async def tasks(request: Request) -> JSONResponse:
        """The patients an agent's current task (for a user) is about, for the disclosure
        gateway: an agent bound to Jane can't send Kermit's data out either."""
        return task_patients(deps, validator, request.headers.get("authorization", ""),
                             request.query_params.get("agent", ""),
                             request.query_params.get("user", ""))

    @server.custom_route("/tasks/release", methods=["POST"])
    async def release(request: Request) -> JSONResponse:
        """The coordinator ends that binding when the agent's task is over."""
        return assign_task(deps, validator, request.headers.get("authorization", ""),
                           await request.json(), release=True)

    return server


def consent_for(deps: EhrDependencies, validator: JwtValidator, header: str,
                patient_id: str) -> JSONResponse:
    try:
        claims = validator.validate(header.removeprefix("Bearer ").strip())
    except TokenValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if claims.authorized_party != "disclosure-mcp":
        return JSONResponse({"detail": "only the disclosure gateway reads consent here"},
                            status_code=403)
    try:
        record = deps.repo.get(patient_id)
    except KeyError:
        return JSONResponse({"detail": "no such patient"}, status_code=404)
    return JSONResponse({"consent": record.preferences.get("consent", {})})


def task_patients(deps: EhrDependencies, validator: JwtValidator, header: str, agent: str,
                  user: str) -> JSONResponse:
    try:
        claims = validator.validate(header.removeprefix("Bearer ").strip())
    except TokenValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if claims.authorized_party != "disclosure-mcp":
        return JSONResponse({"detail": "only the disclosure gateway reads task assignments"},
                            status_code=403)
    return JSONResponse({"patients": deps.assignments.patients_for(agent, user)})


def assign_task(deps: EhrDependencies, validator: JwtValidator, header: str,
                body: dict, release: bool = False) -> JSONResponse:
    try:
        claims = validator.validate(header.removeprefix("Bearer ").strip())
    except TokenValidationError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    if claims.authorized_party != "orchestrator" or "task:assign" not in claims.scopes:
        return JSONResponse({"detail": "only the discharge coordinator may assign tasks "
                                       "(scope task:assign)"}, status_code=403)
    if not all(isinstance(body.get(k), str) and body[k] for k in ("agent", "user", "patient_id")):
        return JSONResponse({"detail": "agent, user and patient_id are required"},
                            status_code=422)
    if release:
        deps.assignments.release(body["agent"], body["user"], body["patient_id"])
        return JSONResponse({"status": "released"})
    deps.assignments.assign(body["agent"], body["user"], body["patient_id"])
    return JSONResponse({"status": "assigned"})


def main() -> None:
    setup_tracing(SERVER_NAME, get_settings().otel_exporter_otlp_endpoint)
    serve(build_server(), SERVER_NAME, PORT)


if __name__ == "__main__":
    main()
