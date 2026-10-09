"""Authentication helpers: get tokens from Keycloak, exchange them, and validate them."""

from hacknc.common.auth.errors import (
    AuthError,
    InsufficientScopeError,
    TokenRequestError,
    TokenValidationError,
)
from hacknc.common.auth.jwt_validator import JwtValidator
from hacknc.common.auth.keycloak_client import KeycloakClient, audience_scope
from hacknc.common.auth.models import TokenClaims, TokenSet, peek_claims

__all__ = [
    "AuthError",
    "InsufficientScopeError",
    "JwtValidator",
    "KeycloakClient",
    "TokenClaims",
    "TokenRequestError",
    "TokenSet",
    "TokenValidationError",
    "audience_scope",
    "peek_claims",
]
