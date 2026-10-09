"""Talks to Keycloak's OAuth 2.0 token endpoint.

Ways to get a token:

* ``authorization_code`` the browser login flow: Keycloak redirects back with a one-time
                         code, and the app's backend trades it (plus the PKCE verifier)
                         for tokens. This is how the UI logs people in.
* ``refresh``            get a fresh access token before the old one expires.
* ``password_login``     a user logs in with username + password.
                         TEST HARNESS ONLY - real apps use the browser login flow.
* ``client_credentials`` a client (agent) gets a token as *itself*, no human involved.
* ``exchange_token``     RFC 8693 Token Exchange: an agent trades the token it received
                         for a new one addressed to the next service, still on behalf
                         of the same human. This is how delegation works in this system.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

from hacknc.common.auth.errors import TokenRequestError
from hacknc.common.auth.models import TokenSet

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def audience_scope(audience: str) -> str:
    """Name of the Keycloak client scope that permits requesting tokens for ``audience``.

    In our realm, a client may only obtain a token addressed to service X if it has
    the optional client scope ``aud-X``. That makes "who may call whom" visible and
    editable in the Keycloak admin console.
    """
    return f"aud-{audience}"


class KeycloakClient:
    """Minimal client for one Keycloak realm.

    Args:
        realm_url: e.g. ``http://localhost:8080/realms/hacknc-hospital``.
        http: optional ``httpx.Client`` (tests pass one with a mock transport).
    """

    def __init__(self, realm_url: str, http: httpx.Client | None = None) -> None:
        self.realm_url = realm_url.rstrip("/")
        self._http = http or httpx.Client(timeout=10.0)

    @property
    def token_endpoint(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/token"

    @property
    def jwks_uri(self) -> str:
        return f"{self.realm_url}/protocol/openid-connect/certs"

    def authorization_code(
        self,
        client_id: str,
        client_secret: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> TokenSet:
        """Trade the one-time login ``code`` for tokens (Authorization Code + PKCE)."""
        return self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            auth=(client_id, client_secret),
        )

    def refresh(self, client_id: str, client_secret: str, refresh_token: str) -> TokenSet:
        """Use a refresh token to get a new access token for the same login session."""
        return self._request_token(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(client_id, client_secret),
        )

    def password_login(
        self, client_id: str, username: str, password: str, scopes: Iterable[str] = ()
    ) -> TokenSet:
        """Log a user in directly with their password. TEST HARNESS ONLY."""
        return self._request_token(
            {
                "grant_type": "password",
                "client_id": client_id,
                "username": username,
                "password": password,
                "scope": _scope_param(["openid", *scopes]),
            }
        )

    def client_credentials(
        self, client_id: str, client_secret: str, scopes: Iterable[str] = ()
    ) -> TokenSet:
        """Get a token for the client itself (no user)."""
        return self._request_token(
            {"grant_type": "client_credentials", "scope": _scope_param(scopes)},
            auth=(client_id, client_secret),
        )

    def exchange_token(
        self,
        client_id: str,
        client_secret: str,
        subject_token: str,
        audience: str,
        scopes: Iterable[str] = (),
    ) -> TokenSet:
        """Trade ``subject_token`` for a new token addressed to ``audience``.

        The requesting client (``client_id``) must itself be in the subject token's
        ``aud`` claim, otherwise Keycloak rejects the exchange. That rule is what
        stops a token from being replayed by a service it was never meant for.
        """
        return self._request_token(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": subject_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "requested_token_type": ACCESS_TOKEN_TYPE,
                "audience": audience,
                "scope": _scope_param([audience_scope(audience), *scopes]),
            },
            auth=(client_id, client_secret),
        )

    def _request_token(
        self, form: dict[str, Any], auth: tuple[str, str] | None = None
    ) -> TokenSet:
        form = {k: v for k, v in form.items() if v}  # drop empty scope etc.
        response = self._http.post(self.token_endpoint, data=form, auth=auth)
        if response.status_code != 200:
            body = _json_or_empty(response)
            raise TokenRequestError(
                response.status_code,
                body.get("error", "request_failed"),
                body.get("error_description", response.text[:200]),
            )
        return TokenSet.from_response(response.json())


def _scope_param(scopes: Iterable[str]) -> str:
    """Join scopes into the space-separated form OAuth expects, dropping duplicates."""
    return " ".join(dict.fromkeys(s for s in scopes if s))


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
