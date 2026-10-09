"""Tokens a service gets for *itself* (OAuth client credentials), cached until near expiry.

Used when no human is involved, e.g. a service appending to the audit log.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable

from hacknc.common.auth.keycloak_client import KeycloakClient, audience_scope
from hacknc.common.auth.models import TokenSet

RENEW_MARGIN_SECONDS = 30


class ServiceTokenProvider:
    """Hands out a valid access token for ``client_id`` addressed to ``audience``.

    Args:
        keycloak: Keycloak client.
        client_id / client_secret: this service's own credentials.
        audience: the service the token is for (adds the ``aud-<audience>`` scope).
        scopes: permission scopes to request.
        clock: injectable time source for tests.
    """

    def __init__(
        self,
        keycloak: KeycloakClient,
        client_id: str,
        client_secret: str,
        audience: str,
        scopes: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._kc = keycloak
        self.client_id = client_id
        self._secret = client_secret
        self._scopes = [audience_scope(audience), *scopes]
        self._clock = clock
        self._token: TokenSet | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token is None or self._clock() > self._expires_at - RENEW_MARGIN_SECONDS:
                self._token = self._kc.client_credentials(self.client_id, self._secret,
                                                          self._scopes)
                self._expires_at = self._clock() + self._token.expires_in
            return self._token.access_token
