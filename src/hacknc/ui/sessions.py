"""Server-side session store: tokens live here, the browser only gets a random session id.

In-memory and single-process, which is fine for a demo. A real deployment would use
Redis or a database so sessions survive restarts and work across replicas.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, replace

from hacknc.common.auth import TokenClaims, TokenSet, peek_claims


@dataclass(frozen=True)
class UserSession:
    """One logged-in user."""

    id: str
    tokens: TokenSet
    user: TokenClaims  # verified ID-token claims: who logged in
    created_at: float

    @property
    def access_claims(self) -> TokenClaims:
        """Claims of the current access token (already validated by Keycloak's token endpoint)."""
        return peek_claims(self.tokens.access_token)

    def access_expires_within(self, seconds: float, now: float | None = None) -> bool:
        return self.access_claims.expires_at - (now or time.time()) < seconds


class SessionStore:
    """Thread-safe in-memory map of session id -> ``UserSession``.

    Args:
        max_age_seconds: sessions older than this are discarded (default 8 hours).
    """

    def __init__(self, max_age_seconds: float = 8 * 3600) -> None:
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()
        self._max_age = max_age_seconds

    def create(self, tokens: TokenSet, user: TokenClaims) -> UserSession:
        session = UserSession(secrets.token_urlsafe(32), tokens, user, time.time())
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str | None) -> UserSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session and time.time() - session.created_at > self._max_age:
                del self._sessions[session_id]
                return None
            return session

    def update_tokens(self, session_id: str, tokens: TokenSet) -> UserSession | None:
        """Swap in refreshed tokens. Keycloak may omit the refresh token, so keep the old one."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if tokens.refresh_token is None:
                tokens = replace(tokens, refresh_token=session.tokens.refresh_token)
            if tokens.id_token is None:
                tokens = replace(tokens, id_token=session.tokens.id_token)
            session = replace(session, tokens=tokens)
            self._sessions[session_id] = session
            return session

    def delete(self, session_id: str | None) -> UserSession | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.pop(session_id, None)
