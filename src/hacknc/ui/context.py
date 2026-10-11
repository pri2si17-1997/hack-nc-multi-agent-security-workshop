"""Shared plumbing for every UI route: dependencies, sessions, rendering, tokens, audit.

Route modules (``hacknc.ui.routes.*``) receive one ``UiContext`` and use its helpers, so each
feature file only contains what is specific to that feature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from hacknc.approval_service.client import ApprovalApi
from hacknc.audit_service.client import AuditApi
from hacknc.common.audit import AuditClient, AuditEvent, actor_fields
from hacknc.common.auth import AuthError, JwtValidator, KeycloakClient, TokenClaims
from hacknc.llm.settings import get_llm_settings
from hacknc.partners.client import PartnerApi
from hacknc.ui import presenters
from hacknc.ui.oidc import OidcLogin
from hacknc.ui.runs import RunHub
from hacknc.ui.sessions import SessionStore, UserSession
from hacknc.ui.settings import UiSettings

HERE = Path(__file__).parent


def _static_url(path: str) -> str:
    """``/static/<path>?v=<content hash>``: a changed file gets a new URL, so no browser
    can keep running a stale copy after a rebuild."""
    digest = hashlib.sha256((HERE / "static" / path).read_bytes()).hexdigest()[:10]
    return f"/static/{path}?v={digest}"
SESSION_KEY = "sid"
LOGIN_KEY = "login"
REFRESH_MARGIN_SECONDS = 60
UI_CLIENT = "hospital-ui"

SecretLookup = Callable[[str], str]
ValidatorFactory = Callable[[str], JwtValidator]


@dataclass
class UiDependencies:
    """Everything the UI needs from the outside world (tests pass fakes)."""

    settings: UiSettings
    oidc: OidcLogin
    keycloak: KeycloakClient
    validators: ValidatorFactory
    secrets: SecretLookup
    sessions: SessionStore
    approvals: ApprovalApi
    audit_reader: AuditApi
    audit: AuditClient | None = None
    runs: RunHub = field(default_factory=RunHub)
    partners: PartnerApi | None = None          # the admin console reads their logs


class UiContext:
    def __init__(self, deps: UiDependencies) -> None:
        self.deps = deps
        self.templates = Jinja2Templates(directory=HERE / "templates")
        self.templates.env.globals["static"] = _static_url
        self.templates.env.globals["llm_models"] = get_llm_settings().models()

    # ----------------------------------------------------------------- sessions

    def current_session(self, request: Request) -> UserSession | None:
        """The logged-in user, refreshing the access token if it is about to expire."""
        deps = self.deps
        session = deps.sessions.get(request.session.get(SESSION_KEY))
        if session is None or not session.access_expires_within(REFRESH_MARGIN_SECONDS):
            return session
        if session.tokens.refresh_token:
            try:
                return deps.sessions.update_tokens(
                    session.id, deps.oidc.refresh(session.tokens.refresh_token))
            except AuthError:
                pass
        deps.sessions.delete(session.id)
        request.session.clear()
        return None

    def page_session(self, request: Request) -> UserSession | RedirectResponse:
        """For page routes: the session, or a redirect to the landing page."""
        return self.current_session(request) or RedirectResponse("/", status_code=303)

    def api_session(self, request: Request) -> UserSession:
        """For JSON routes: the session, or 401."""
        session = self.current_session(request)
        if session is None:
            raise HTTPException(401, detail="Your session expired. Please log in again.")
        return session

    @staticmethod
    def role(session: UserSession) -> str:
        return presenters.primary_role(session.access_claims)

    def page_for_role(self, request: Request, *roles: str) -> UserSession | RedirectResponse:
        """Page guard: logged in *and* one of ``roles``; otherwise back to the right home."""
        session = self.page_session(request)
        if isinstance(session, UserSession) and self.role(session) not in roles:
            return RedirectResponse(presenters.home_for(self.role(session)), status_code=303)
        return session

    def api_for_role(self, request: Request, *roles: str) -> UserSession:
        session = self.api_session(request)
        if self.role(session) not in roles:
            raise HTTPException(403, detail=f"this needs one of: {', '.join(roles)}")
        return session

    # ----------------------------------------------------------------- tokens

    def user_token_for(self, session: UserSession, audience: str, scopes: list[str]) -> str:
        """Exchange the user's login token for one addressed to ``audience`` (raises AuthError).

        The new token still names the user (``sub``) and the hospital app (``azp``), which is
        how services know a *human* is acting, not an agent.
        """
        return self.deps.keycloak.exchange_token(
            UI_CLIENT, self.deps.secrets(UI_CLIENT), session.tokens.access_token, audience,
            scopes).access_token

    # ----------------------------------------------------------------- audit

    def audit(self, claims: TokenClaims | None, action: str, **fields: Any) -> None:
        if self.deps.audit is None:
            return
        fields.setdefault("agent_chain", [UI_CLIENT])
        self.deps.audit.record(AuditEvent(action=action, **actor_fields(claims), **fields))

    # ----------------------------------------------------------------- rendering

    def render(self, request: Request, template: str, session: UserSession | None,
               status_code: int = 200, **context: Any) -> Response:
        settings = self.deps.settings
        base: dict[str, Any] = {
            "nav": [],
            "active": request.url.path,
            "links": {"grafana": settings.grafana_url, "keycloak": settings.keycloak_admin_url,
                      "opa": settings.opa_url},
            "user": None,
        }
        if session is not None:
            role = presenters.primary_role(session.access_claims)
            base["user"] = {
                "name": session.user.raw.get("name") or session.user.username,
                "username": session.user.username,
                "role": role,
            }
            base["nav"] = presenters.nav_for(role)
        return self.templates.TemplateResponse(request, template, {**base, **context},
                                               status_code=status_code)

