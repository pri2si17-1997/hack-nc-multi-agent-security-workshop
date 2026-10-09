"""Validates incoming access tokens. Every service runs this before doing anything.

A token is accepted only if ALL of these hold:

1. Signature   - signed by Keycloak's current key (fetched from the realm's JWKS endpoint).
2. Issuer      - ``iss`` is our realm.
3. Audience    - ``aud`` contains *this* service. A token meant for another service is
                 refused even if everything else is fine (no token passthrough).
4. Expiry      - ``exp`` is in the future (with a little clock-skew leeway).
5. Scopes      - the operation's required scopes are all present.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

import jwt

from hacknc.common.auth.errors import InsufficientScopeError, TokenValidationError
from hacknc.common.auth.models import TokenClaims

ALLOWED_ALGORITHMS = ["RS256"]


class SigningKeyProvider(Protocol):
    """Anything that can find the public key for a JWT (``jwt.PyJWKClient`` does this)."""

    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class JwtValidator:
    """Validates tokens for one audience (one service).

    Args:
        issuer: expected ``iss`` claim.
        audience: this service's client id; must appear in ``aud``.
        jwks_uri: Keycloak's JWKS URL. Ignored if ``key_provider`` is given.
        key_provider: injectable key source (tests use a fake).
        leeway_seconds: tolerated clock skew when checking ``exp``.
    """

    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_uri: str | None = None,
        key_provider: SigningKeyProvider | None = None,
        leeway_seconds: int = 10,
    ) -> None:
        if key_provider is None:
            if not jwks_uri:
                raise ValueError("either jwks_uri or key_provider is required")
            key_provider = jwt.PyJWKClient(jwks_uri, cache_keys=True)
        self.issuer = issuer
        self.audience = audience
        self._keys = key_provider
        self._leeway = leeway_seconds

    def validate(self, token: str, required_scopes: Iterable[str] = ()) -> TokenClaims:
        """Return the token's claims, or raise ``TokenValidationError`` explaining why not."""
        try:
            signing_key = self._keys.get_signing_key_from_jwt(token)
            raw = jwt.decode(
                token,
                key=getattr(signing_key, "key", signing_key),
                algorithms=ALLOWED_ALGORITHMS,
                issuer=self.issuer,
                audience=self.audience,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenValidationError("token has expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenValidationError(f"token is not addressed to '{self.audience}'") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenValidationError("token was not issued by our Keycloak realm") from exc
        except jwt.PyJWTError as exc:
            raise TokenValidationError(f"invalid token: {exc}") from exc

        claims = TokenClaims(raw)
        missing = set(required_scopes) - claims.scopes
        if missing:
            raise InsufficientScopeError(missing)
        return claims
