"""Shared test fixtures: an RSA key pair and a helper to mint Keycloak-like JWTs offline."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "http://localhost:8080/realms/hacknc-hospital"

TokenFactory = Callable[..., str]


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StaticKeyProvider:
    """Stands in for Keycloak's JWKS endpoint: always returns the same public key."""

    def __init__(self, public_key: Any) -> None:
        self.key = public_key

    def get_signing_key_from_jwt(self, token: str) -> StaticKeyProvider:
        return self


@pytest.fixture
def key_provider(signing_key: rsa.RSAPrivateKey) -> StaticKeyProvider:
    return StaticKeyProvider(signing_key.public_key())


@pytest.fixture
def make_token(signing_key: rsa.RSAPrivateKey) -> TokenFactory:
    """Build a signed access token.

    Pass claim overrides as keyword args; an override of ``None`` removes that claim.
    """

    def _make(key: rsa.RSAPrivateKey | None = None, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": "user-123",
            "preferred_username": "dr.morgana",
            "aud": "summary-agent",
            "azp": "orchestrator",
            "scope": "patient:read",
            "realm_access": {"roles": ["physician"]},
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, key or signing_key, algorithm="RS256")

    return _make
