"""Calls from the hospital app to the services behind it, always with the user's own token
(exchanged per audience where needed)."""

from __future__ import annotations

from typing import Any

import anyio
import httpx

from hacknc.agents.runtime import ToolDenied, call_tool, mcp_session
from hacknc.common.auth import AuthError
from hacknc.ui.context import UiContext
from hacknc.ui.sessions import UserSession


class GatewayUnavailable(RuntimeError):
    """The EHR gateway could not be reached (restarting, down...)."""


async def coordinator(ctx: UiContext, session: UserSession, method: str, path: str,
                      json: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """The discharge coordinator accepts the login token as is (its audience)."""
    url = f"{ctx.deps.settings.orchestrator_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=330) as http:  # a model-driven discharge takes minutes
        response = await http.request(method, url, json=json, headers={
            "Authorization": f"Bearer {session.tokens.access_token}"})
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:200]}
    return response.status_code, body if isinstance(body, dict) else {"data": body}


async def exchanged(ctx: UiContext, session: UserSession, audience: str,
                    scopes: list[str]) -> str:
    return await anyio.to_thread.run_sync(
        lambda: ctx.user_token_for(session, audience, scopes))


async def ehr(ctx: UiContext, session: UserSession,
              calls: list[tuple[str, dict[str, Any]]],
              scopes: tuple[str, ...] = ("patient:read",)) -> list[dict[str, Any]]:
    """Several EHR gateway tools in one MCP session, as the hospital app (human's view).
    Clinicians use ``patient:read``; a patient in the portal uses ``portal:self``."""
    token = await exchanged(ctx, session, "ehr-mcp", list(scopes))
    try:
        async with mcp_session(ctx.deps.settings.ehr_mcp_url, token) as client:
            return [await call_tool(client, tool, args) for tool, args in calls]
    except (AuthError, ToolDenied):
        raise
    except Exception as exc:
        # Errors raised inside the MCP session arrive wrapped in exception groups: a refusal
        # must still read as a refusal, not as "the gateway is down".
        refusal = _find(exc, (AuthError, ToolDenied))
        if refusal is not None:
            raise refusal from None
        raise GatewayUnavailable("The EHR gateway is not reachable right now. "
                                 "Try again in a moment.") from exc


def _find(exc: BaseException, kinds: tuple[type, ...]) -> BaseException | None:
    if isinstance(exc, kinds):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        if found := _find(inner, kinds):
            return found
    return None


async def audit_events(ctx: UiContext, session: UserSession, **filters: Any) -> dict[str, Any]:
    token = await exchanged(ctx, session, "audit-service", ["audit:read"])
    result = await anyio.to_thread.run_sync(
        lambda: ctx.deps.audit_reader.events(token, **filters))
    return {"ok": result.ok, "reason": "" if result.ok else result.reason, **result.body}
