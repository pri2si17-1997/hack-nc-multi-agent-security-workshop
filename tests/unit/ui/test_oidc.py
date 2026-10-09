from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from hacknc.common.auth import JwtValidator, TokenSet, TokenValidationError
from hacknc.ui.oidc import OidcLogin, pkce_pair
from tests.conftest import ISSUER


class FakeKeycloak:
    def __init__(self, tokens: TokenSet) -> None:
        self.tokens = tokens
        self.calls: list[tuple] = []

    def authorization_code(self, *args):
        self.calls.append(("authorization_code", *args))
        return self.tokens

    def refresh(self, *args):
        self.calls.append(("refresh", *args))
        return self.tokens


def make_login(key_provider, tokens: TokenSet | None = None) -> tuple[OidcLogin, FakeKeycloak]:
    kc = FakeKeycloak(tokens or TokenSet(access_token="a"))
    login = OidcLogin(
        issuer=ISSUER,
        client_id="hospital-ui",
        client_secret="secret",
        redirect_uri="http://localhost:8000/auth/callback",
        keycloak=kc,
        id_token_validator=JwtValidator(ISSUER, "hospital-ui", key_provider=key_provider),
        scopes=["patient:read"],
    )
    return login, kc


def test_pkce_challenge_is_sha256_of_verifier() -> None:
    verifier, challenge = pkce_pair()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    assert 43 <= len(verifier) <= 128


def test_start_builds_keycloak_authorize_url(key_provider) -> None:
    login, _ = make_login(key_provider)
    pending = login.start(login_hint="nurse.gawain")

    url = urlparse(pending.authorization_url)
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.path.endswith("/protocol/openid-connect/auth")
    assert params["client_id"] == "hospital-ui"
    assert params["response_type"] == "code"
    assert params["scope"] == "openid patient:read"
    assert params["state"] == pending.state
    assert params["nonce"] == pending.nonce
    assert params["code_challenge_method"] == "S256"
    assert params["login_hint"] == "nurse.gawain"
    assert pending.code_verifier not in pending.authorization_url  # only the hash leaves


def test_each_login_attempt_is_unique(key_provider) -> None:
    login, _ = make_login(key_provider)
    first, second = login.start(), login.start()
    assert first.state != second.state
    assert first.code_verifier != second.code_verifier


def test_finish_returns_verified_id_token_claims(key_provider, make_token) -> None:
    id_token = make_token(aud="hospital-ui", nonce="n-1", preferred_username="nurse.gawain")
    login, kc = make_login(key_provider, TokenSet(access_token="a", id_token=id_token))

    tokens, user = login.finish("code-1", "verifier-1", "n-1")

    assert user.username == "nurse.gawain"
    assert tokens.access_token == "a"
    assert kc.calls[0] == ("authorization_code", "hospital-ui", "secret", "code-1",
                           "http://localhost:8000/auth/callback", "verifier-1")


def test_finish_rejects_wrong_nonce(key_provider, make_token) -> None:
    id_token = make_token(aud="hospital-ui", nonce="someone-elses")
    login, _ = make_login(key_provider, TokenSet(access_token="a", id_token=id_token))
    with pytest.raises(TokenValidationError, match="nonce"):
        login.finish("c", "v", "mine")


def test_finish_requires_id_token(key_provider) -> None:
    login, _ = make_login(key_provider, TokenSet(access_token="a"))
    with pytest.raises(TokenValidationError, match="ID token"):
        login.finish("c", "v", "n")


def test_finish_rejects_id_token_for_another_client(key_provider, make_token) -> None:
    id_token = make_token(aud="other-app", nonce="n")
    login, _ = make_login(key_provider, TokenSet(access_token="a", id_token=id_token))
    with pytest.raises(TokenValidationError):
        login.finish("c", "v", "n")


def test_logout_url_includes_hint_and_return_address(key_provider) -> None:
    login, _ = make_login(key_provider)
    params = parse_qs(urlparse(login.logout_url("id-tok", "http://localhost:8000/")).query)
    assert params["id_token_hint"] == ["id-tok"]
    assert params["post_logout_redirect_uri"] == ["http://localhost:8000/"]
