"""Shared plumbing for our MCP servers (the EHR gateway and every agent).

* ``KeycloakTokenVerifier`` plugs Keycloak JWT validation into the MCP SDK's OAuth
  resource-server support: every HTTP request to ``/mcp`` must carry a token addressed to
  this server, or it gets ``401`` with a pointer to Keycloak.
* ``current_caller`` tells a tool who is calling: the agent (``azp``) and the human it acts for.
* ``serve`` runs an ``MCPServer`` over Streamable HTTP with a ``/healthz`` endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass

import anyio
import uvicorn
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from hacknc.common.auth import JwtValidator, KeycloakClient, TokenClaims, TokenValidationError


@dataclass(frozen=True)
class Caller:
    """Who is calling: the agent/app that holds the token, and the human it acts for."""

    claims: TokenClaims
    token: str

    @property
    def agent(self) -> str:
        return self.claims.authorized_party

    @property
    def user(self) -> str:
        return self.claims.username


def current_caller() -> Caller | None:
    """The caller of the current MCP request (``None`` outside an authenticated request)."""
    access = get_access_token()
    if access is None or access.claims is None:
        return None
    return Caller(TokenClaims(access.claims), access.token)


class KeycloakTokenVerifier:
    def __init__(self, validator: JwtValidator) -> None:
        self._validator = validator

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # Validation may fetch Keycloak's signing keys over HTTP: keep it off the event loop.
            claims = await anyio.to_thread.run_sync(self._validator.validate, token)
        except TokenValidationError:
            return None
        return AccessToken(
            token=token,
            client_id=claims.authorized_party,
            scopes=sorted(claims.scopes),
            expires_at=claims.expires_at,
            subject=claims.subject,
            resource=self._validator.audience,
            claims=claims.raw,
        )


def auth_kwargs(name: str, issuer: str, keycloak: KeycloakClient, public_url: str) -> dict:
    """``MCPServer`` keyword arguments that make ``name`` an OAuth resource server."""
    return {
        "token_verifier": KeycloakTokenVerifier(
            JwtValidator(issuer, name, jwks_uri=keycloak.jwks_uri)),
        "auth": AuthSettings(
            issuer_url=issuer,
            resource_server_url=f"{public_url}/mcp",
            # Our verifier checks the token's audience (aud = this server) itself.
            validate_token_resource=False,
        ),
    }


def serve(server: MCPServer, name: str, port: int) -> None:
    """Run ``server`` on ``port`` at ``/mcp`` (stateless Streamable HTTP, JSON responses)."""

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "server": name})

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,   # every request carries its own token; no server-side sessions
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"localhost:{port}", f"127.0.0.1:{port}", f"{name}:{port}"]),
    )
    uvicorn.run(app, host="0.0.0.0", port=port)
