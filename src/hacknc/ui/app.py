"""FastAPI app for the hospital UI (a "backend for frontend": tokens stay on the server).

Routes live in ``hacknc.ui.routes``, one module per feature:

    auth        /  /login  /auth/callback  /logout
    clinical    /patients  /patients/{id}  /patients/{id}/chart  /api/discharges/*  ...
    admin       /admin  /admin/audit  /api/admin/*
    runs        /api/runs/{id}/events  /otlp/v1/traces
"""

from __future__ import annotations

import jwt
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from hacknc.approval_service.client import ApprovalApi
from hacknc.audit_service.client import AuditApi
from hacknc.common.audit import AuditClient
from hacknc.common.auth import JwtValidator, KeycloakClient
from hacknc.common.auth.service_tokens import ServiceTokenProvider
from hacknc.common.config import Settings, client_secret
from hacknc.common.telemetry import FASTAPI_TELEMETRY
from hacknc.demo.delegation import HUMAN_SCOPES
from hacknc.partners.client import PartnerApi
from hacknc.ui.context import HERE, UI_CLIENT, UiContext, UiDependencies
from hacknc.ui.oidc import OidcLogin
from hacknc.ui.routes import admin, auth, clinical, portal, runs
from hacknc.ui.sessions import SessionStore
from hacknc.ui.settings import UiSettings


def build_dependencies(common: Settings, ui: UiSettings) -> UiDependencies:
    """Wire up the real Keycloak-backed dependencies."""
    kc = KeycloakClient(common.keycloak_url)
    keys = jwt.PyJWKClient(kc.jwks_uri, cache_keys=True)

    def validators(audience: str) -> JwtValidator:
        return JwtValidator(common.keycloak_issuer, audience, key_provider=keys)

    oidc = OidcLogin(
        issuer=common.keycloak_issuer,
        client_id=ui.ui_client_id,
        client_secret=client_secret(ui.ui_client_id),
        redirect_uri=ui.redirect_uri,
        keycloak=kc,
        id_token_validator=validators(ui.ui_client_id),
        scopes=HUMAN_SCOPES,
    )
    approvals_api = ApprovalApi(ui.approval_url)
    audit_tokens = ServiceTokenProvider(kc, UI_CLIENT, client_secret(UI_CLIENT), "audit-service",
                                        ["audit:write"])
    return UiDependencies(
        settings=ui,
        oidc=oidc,
        keycloak=kc,
        validators=validators,
        secrets=client_secret,
        sessions=SessionStore(),
        approvals=approvals_api,
        audit_reader=AuditApi(ui.audit_url),
        audit=AuditClient(ui.audit_url, audit_tokens),
        partners=PartnerApi(ui.partners_url),
    )


def create_app(deps: UiDependencies) -> FastAPI:
    app = FastAPI(title="HackNC Hospital UI", docs_url=None, redoc_url=None,
                  telemetry=FASTAPI_TELEMETRY)
    app.state.deps = deps
    app.add_middleware(
        SessionMiddleware,
        secret_key=deps.settings.ui_session_secret.get_secret_value(),
        session_cookie="hacknc_ui",
        same_site="lax",
        https_only=False,  # demo runs on http://localhost
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    ctx = UiContext(deps)

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next):  # noqa: ANN001, ANN202
        # Always check the ETag: after a rebuild the browser gets the new CSS/JS, not a stale copy.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    for feature in (auth, runs, clinical, portal, admin):
        feature.register(app, ctx)

    return app
