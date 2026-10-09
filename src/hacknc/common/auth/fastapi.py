"""FastAPI glue: turn ``Authorization: Bearer <token>`` into validated claims.

Usage::

    auth = BearerAuth(JwtValidator(issuer, "audit-service", jwks_uri=...))

    @app.get("/events")
    def list_events(claims: TokenClaims = Depends(auth)): ...

A missing or invalid token gets ``401``. Scope and policy checks happen afterwards,
in the route, so they can explain *why* a request was refused.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from hacknc.common.auth.errors import TokenValidationError
from hacknc.common.auth.jwt_validator import JwtValidator
from hacknc.common.auth.models import TokenClaims


class BearerAuth:
    """FastAPI dependency that validates the bearer token for one service (audience)."""

    def __init__(self, validator: JwtValidator) -> None:
        self.validator = validator

    def __call__(self, request: Request) -> TokenClaims:
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise _unauthorized("missing bearer token")
        try:
            return self.validator.validate(token)
        except TokenValidationError as exc:
            raise _unauthorized(str(exc)) from exc


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=reason,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'},
    )
