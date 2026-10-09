"""Landing page, login (OIDC Authorization Code + PKCE), callback and logout."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

from hacknc.common.auth import AuthError, peek_claims
from hacknc.ui import presenters
from hacknc.ui.context import LOGIN_KEY, SESSION_KEY, UiContext


def register(app: FastAPI, ctx: UiContext) -> None:
    deps = ctx.deps

    def landing_error(request: Request, message: str) -> Response:
        ctx.audit(None, "auth.login", decision="deny", severity="warning", reason=message)
        return ctx.render(request, "landing.html", None, personas=presenters.PERSONAS,
                          patients=presenters.PORTAL_PATIENTS,
                          error=message, status_code=400)

    @app.get("/")
    def landing(request: Request, error: str | None = None) -> Response:
        session = ctx.current_session(request)
        if session and ctx.role(session) in presenters.ROLES:
            return RedirectResponse(presenters.home_for(ctx.role(session)), status_code=303)
        if error == "no-role":
            error = ("Your account has no role in this hospital app (physician, nurse, admin "
                     "or patient). Ask an administrator, or log in as one of the personas.")
        return ctx.render(request, "landing.html", None, personas=presenters.PERSONAS,
                          patients=presenters.PORTAL_PATIENTS,
                          error=error)

    @app.get("/login")
    def login(request: Request, persona: str | None = None) -> Response:
        hint = persona if presenters.can_log_in_as(persona or "") else None
        pending = deps.oidc.start(login_hint=hint)
        request.session[LOGIN_KEY] = pending.to_session()
        return RedirectResponse(pending.authorization_url, status_code=303)

    @app.get("/auth/callback")
    def callback(request: Request, code: str | None = None, state: str | None = None,
                 error: str | None = None, error_description: str | None = None) -> Response:
        pending = request.session.pop(LOGIN_KEY, None)
        if error:
            return landing_error(request, error_description or error)
        if not pending or not code or state != pending.get("state"):
            return landing_error(request, "Login attempt expired or did not match. Try again.")
        try:
            tokens, user = deps.oidc.finish(code, pending["code_verifier"], pending["nonce"])
        except AuthError as exc:
            return landing_error(request, f"Login failed: {exc}")
        session = deps.sessions.create(tokens, user)
        request.session[SESSION_KEY] = session.id
        claims = peek_claims(tokens.access_token)
        ctx.audit(claims, "auth.login", decision="allow", reason="logged in to the hospital app")
        return RedirectResponse(presenters.home_for(presenters.primary_role(claims)),
                                status_code=303)

    @app.get("/logout")
    def logout(request: Request) -> Response:
        session = deps.sessions.delete(request.session.get(SESSION_KEY))
        request.session.clear()
        if session is None:
            return RedirectResponse("/", status_code=303)
        ctx.audit(session.access_claims, "auth.logout", reason="logged out")
        home = f"{deps.settings.ui_base_url.rstrip('/')}/"
        return RedirectResponse(deps.oidc.logout_url(session.tokens.id_token, home), 303)
