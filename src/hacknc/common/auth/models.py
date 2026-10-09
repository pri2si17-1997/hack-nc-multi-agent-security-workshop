"""Plain data objects for tokens and the claims inside them."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import jwt


@dataclass(frozen=True)
class TokenSet:
    """What Keycloak's token endpoint returns."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 0
    scope: str = ""
    refresh_token: str | None = None
    id_token: str | None = None

    @classmethod
    def from_response(cls, body: dict[str, Any]) -> TokenSet:
        return cls(
            access_token=body["access_token"],
            token_type=body.get("token_type", "Bearer"),
            expires_in=int(body.get("expires_in", 0)),
            scope=body.get("scope", ""),
            refresh_token=body.get("refresh_token"),
            id_token=body.get("id_token"),
        )


@dataclass(frozen=True)
class TokenClaims:
    """Convenient, read-only access to the claims we care about in a Keycloak access token.

    Claim cheat-sheet:
        sub                 who the token is about (the human, even after exchanges)
        preferred_username  readable name of that human
        azp                 "authorized party": the client (agent) that requested this token
        aud                 who the token is *for*: the only service that should accept it
        scope               space-separated permissions
        realm_access.roles  the human's roles (physician, nurse, admin, patient)
    """

    raw: dict[str, Any] = field(repr=False)

    @property
    def subject(self) -> str:
        return self.raw.get("sub", "")

    @property
    def username(self) -> str:
        return self.raw.get("preferred_username", "")

    @property
    def issuer(self) -> str:
        return self.raw.get("iss", "")

    @property
    def authorized_party(self) -> str:
        return self.raw.get("azp", "")

    @property
    def audience(self) -> list[str]:
        aud = self.raw.get("aud", [])
        return [aud] if isinstance(aud, str) else list(aud)

    @property
    def scopes(self) -> set[str]:
        return set(self.raw.get("scope", "").split())

    @property
    def realm_roles(self) -> set[str]:
        return set(self.raw.get("realm_access", {}).get("roles", []))

    @property
    def expires_at(self) -> int:
        return int(self.raw.get("exp", 0))


def peek_claims(token: str) -> TokenClaims:
    """Decode a JWT **without verifying it**, for display and debugging only.

    Never use this to make an access decision; use ``JwtValidator.validate`` instead.
    """
    return TokenClaims(jwt.decode(token, options={"verify_signature": False}))
