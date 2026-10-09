"""Errors raised by the auth helpers. Callers can catch ``AuthError`` for all of them."""

from __future__ import annotations


class AuthError(Exception):
    """Base class for every authentication / authorization failure."""


class TokenRequestError(AuthError):
    """Keycloak refused to issue a token (bad credentials, scope or audience not allowed...)."""

    def __init__(self, status_code: int, error: str, description: str = "") -> None:
        self.status_code = status_code
        self.error = error
        self.description = description
        super().__init__(f"{status_code} {error}: {description}".rstrip(": "))


class TokenValidationError(AuthError):
    """A presented token is not acceptable (bad signature, expired, wrong audience...)."""


class InsufficientScopeError(TokenValidationError):
    """The token is valid but lacks scopes the operation requires."""

    def __init__(self, missing: set[str]) -> None:
        self.missing = missing
        super().__init__(f"token is missing required scope(s): {', '.join(sorted(missing))}")
