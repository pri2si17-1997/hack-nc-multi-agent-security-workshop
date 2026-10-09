"""What every agent (and the orchestrator) needs: delegate a token, call an MCP server.

``delegate`` is RFC 8693 token exchange wrapped in a span, so each hand-off shows up in the
trace (and in the UI's activity feed) with who asked, for whom, and what was granted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import trace

from hacknc.common.audit import HOSPITAL_ROLES
from hacknc.common.auth import KeycloakClient, TokenClaims, TokenRequestError, peek_claims

tracer = trace.get_tracer(__name__)


class AgentError(Exception):
    """Something the agent could not do; the message is shown to the user."""


class ToolDenied(AgentError):
    """An MCP tool returned an error (e.g. 'DENIED by policy: ...')."""


@dataclass(frozen=True)
class Delegation:
    token: str
    claims: TokenClaims


async def delegate(keycloak: KeycloakClient, secrets: Callable[[str], str], requester: str,
                   subject_token: str, audience: str, scopes: Sequence[str]) -> Delegation:
    """Exchange ``subject_token`` for a token addressed to ``audience`` with ``scopes``.

    Raises ``AgentError`` if Keycloak refuses, or grants the token without the permissions.
    """
    with tracer.start_as_current_span(f"token_exchange {requester} -> {audience}") as span:
        span.set_attributes({"token.requester": requester, "token.requested_audience": audience,
                             "token.requested_scopes": " ".join(scopes)})
        try:
            tokens = await anyio.to_thread.run_sync(
                lambda: keycloak.exchange_token(requester, secrets(requester), subject_token,
                                                audience, scopes))
        except TokenRequestError as exc:
            reason = exc.description or exc.error
            span.set_attributes({"token.granted": False, "token.reason": reason})
            raise AgentError(f"Keycloak refused {requester} a token for {audience}: "
                             f"{reason}") from exc
        # Decoded only to report what was granted; the receiving service validates it.
        claims = peek_claims(tokens.access_token)
        span.set_attributes({
            "token.aud": ",".join(claims.audience), "token.azp": claims.authorized_party,
            "token.scope": " ".join(sorted(claims.scopes)), "user.name": claims.username,
            "user.roles": ",".join(sorted(claims.realm_roles & set(HOSPITAL_ROLES))),
        })
        missing = set(scopes) - claims.scopes
        if missing:
            reason = (f"{', '.join(sorted(missing))} not granted: {claims.username}'s role "
                      "does not allow it")
            span.set_attributes({"token.granted": False, "token.reason": reason})
            raise AgentError(f"Keycloak issued {audience} a token without "
                             f"{', '.join(sorted(missing))}: {claims.username}'s role "
                             "does not allow it")
        span.set_attributes({"token.granted": True, "token.reason": f"valid for {audience}"})
        return Delegation(tokens.access_token, claims)


@asynccontextmanager
async def mcp_session(url: str, token: str, timeout: float = 60) -> AsyncIterator[Client]:
    """An MCP client session authenticated with ``token`` (Streamable HTTP). The SDK leaves a
    caller-supplied HTTP client open, so it is closed here."""
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"},
                                  timeout=timeout) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            yield client


async def call_tool(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call an MCP tool; raise ``ToolDenied`` with the server's message if it failed."""
    result = await client.call_tool(name, arguments)
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content).strip()
        raise ToolDenied(text or f"{name} failed")
    return result.structured_content or {}


async def visible_tools(client: Client) -> list[str]:
    """What this agent may use, according to the server (tools/list is filtered per caller)."""
    tools = (await client.list_tools()).tools
    names = sorted(t.name for t in tools)
    trace.get_current_span().set_attribute("agent.tools_visible", ",".join(names))
    return names
