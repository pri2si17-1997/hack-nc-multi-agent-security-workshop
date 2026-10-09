"""Browser login with OpenID Connect: Authorization Code flow + PKCE.

The flow, step by step:

1. ``start``  - the UI generates three random values and sends the browser to Keycloak:
                 * ``state``         ties the callback to this login attempt (stops CSRF)
                 * ``nonce``         ties the ID token to this login attempt (stops replay)
                 * ``code_verifier`` PKCE secret; only its SHA-256 hash goes to Keycloak
2. The user types their password **on Keycloak's page**, never on ours.
3. Keycloak redirects back with a one-time ``code``.
4. ``finish`` - the backend trades code + verifier for tokens and checks the ID token.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlencode

from hacknc.common.auth import (
    JwtValidator,
    KeycloakClient,
    TokenClaims,
    TokenSet,
    TokenValidationError,
)


def pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` using the S256 method (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass(frozen=True)
class PendingLogin:
    """Values remembered between ``start`` and ``finish`` (kept in the signed session cookie)."""

    state: str
    nonce: str
    code_verifier: str
    authorization_url: str

    def to_session(self) -> dict[str, str]:
        return {"state": self.state, "nonce": self.nonce, "code_verifier": self.code_verifier}


class OidcLogin:
    """Runs the login and logout flows for one OIDC client (``hospital-ui``).

    Args:
        issuer: public realm URL, used for browser redirects (authorize, logout).
        client_id / client_secret: the UI's own Keycloak client.
        redirect_uri: where Keycloak sends the browser after login.
        keycloak: client used for back-channel calls (token endpoint).
        id_token_validator: validates ID tokens (audience = ``client_id``).
        scopes: permissions requested at login; Keycloak grants the subset the user's role allows.
    """

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        keycloak: KeycloakClient,
        id_token_validator: JwtValidator,
        scopes: Iterable[str],
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._kc = keycloak
        self._id_tokens = id_token_validator
        self.scopes = ("openid", *scopes)

    def start(self, login_hint: str | None = None) -> PendingLogin:
        """Create a login attempt and the Keycloak URL to send the browser to."""
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if login_hint:
            params["login_hint"] = login_hint  # pre-fills the username box on Keycloak's page
        url = f"{self.issuer}/protocol/openid-connect/auth?{urlencode(params)}"
        return PendingLogin(state, nonce, verifier, url)

    def finish(
        self, code: str, code_verifier: str, expected_nonce: str
    ) -> tuple[TokenSet, TokenClaims]:
        """Trade the login code for tokens; return them with the verified ID-token claims."""
        tokens = self._kc.authorization_code(
            self.client_id, self._client_secret, code, self.redirect_uri, code_verifier
        )
        if not tokens.id_token:
            raise TokenValidationError("Keycloak did not return an ID token")
        user = self._id_tokens.validate(tokens.id_token)
        if user.raw.get("nonce") != expected_nonce:
            raise TokenValidationError("ID token nonce does not match this login attempt")
        return tokens, user

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._kc.refresh(self.client_id, self._client_secret, refresh_token)

    def logout_url(self, id_token: str | None, post_logout_redirect_uri: str) -> str:
        """Keycloak URL that ends the single-sign-on session, then returns to the UI."""
        params = {"post_logout_redirect_uri": post_logout_redirect_uri, "client_id": self.client_id}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{self.issuer}/protocol/openid-connect/logout?{urlencode(params)}"
