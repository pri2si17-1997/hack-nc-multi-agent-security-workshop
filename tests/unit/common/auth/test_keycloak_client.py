from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from hacknc.common.auth.errors import TokenRequestError
from hacknc.common.auth.keycloak_client import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT,
    KeycloakClient,
    audience_scope,
)

REALM = "http://kc.test/realms/hacknc-hospital"
TOKEN_URL = f"{REALM}/protocol/openid-connect/token"


class Recorder:
    """httpx transport that records the request and replies with a canned response."""

    def __init__(self, status: int = 200, body: dict | str | None = None) -> None:
        self.status = status
        self.body = body if body is not None else {"access_token": "new-token", "expires_in": 60}
        self.request: httpx.Request | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if isinstance(self.body, str):
            return httpx.Response(self.status, text=self.body)
        return httpx.Response(self.status, json=self.body)

    @property
    def form(self) -> dict[str, str]:
        assert self.request is not None
        return {k: v[0] for k, v in parse_qs(self.request.content.decode()).items()}

    @property
    def basic_auth(self) -> tuple[str, str]:
        assert self.request is not None
        encoded = self.request.headers["authorization"].removeprefix("Basic ")
        user, _, password = base64.b64decode(encoded).decode().partition(":")
        return user, password


def client_with(recorder: Recorder) -> KeycloakClient:
    return KeycloakClient(REALM, http=httpx.Client(transport=httpx.MockTransport(recorder)))


def test_endpoints_derive_from_realm_url() -> None:
    kc = KeycloakClient(REALM + "/")
    assert kc.token_endpoint == TOKEN_URL
    assert kc.jwks_uri == f"{REALM}/protocol/openid-connect/certs"


def test_audience_scope_naming() -> None:
    assert audience_scope("ehr-mcp") == "aud-ehr-mcp"


def test_exchange_token_sends_rfc8693_request() -> None:
    recorder = Recorder()
    tokens = client_with(recorder).exchange_token(
        "orchestrator", "secret", "user-token", "summary-agent", ["patient:read"]
    )

    assert tokens.access_token == "new-token"
    assert str(recorder.request.url) == TOKEN_URL
    assert recorder.basic_auth == ("orchestrator", "secret")
    form = recorder.form
    assert form["grant_type"] == TOKEN_EXCHANGE_GRANT
    assert form["subject_token"] == "user-token"
    assert form["subject_token_type"] == ACCESS_TOKEN_TYPE
    assert form["requested_token_type"] == ACCESS_TOKEN_TYPE
    assert form["audience"] == "summary-agent"
    assert form["scope"] == "aud-summary-agent patient:read"


def test_password_login_always_requests_openid() -> None:
    recorder = Recorder()
    client_with(recorder).password_login("workshop-cli", "dr.morgana", "pw", ["patient:read"])
    form = recorder.form
    assert form["grant_type"] == "password"
    assert form["client_id"] == "workshop-cli"
    assert form["scope"] == "openid patient:read"
    assert "authorization" not in recorder.request.headers  # public client: no secret


def test_client_credentials_omits_empty_scope() -> None:
    recorder = Recorder()
    client_with(recorder).client_credentials("ehr-mcp", "secret")
    assert recorder.form == {"grant_type": "client_credentials"}
    assert recorder.basic_auth == ("ehr-mcp", "secret")


def test_duplicate_scopes_are_dropped() -> None:
    recorder = Recorder()
    client_with(recorder).exchange_token("a", "s", "t", "ehr-mcp", ["aud-ehr-mcp", "x", "x"])
    assert recorder.form["scope"] == "aud-ehr-mcp x"


def test_keycloak_error_becomes_token_request_error() -> None:
    recorder = Recorder(400, {"error": "invalid_scope", "error_description": "nope"})
    with pytest.raises(TokenRequestError) as info:
        client_with(recorder).exchange_token("a", "s", "t", "ehr-mcp")
    assert info.value.status_code == 400
    assert info.value.error == "invalid_scope"
    assert info.value.description == "nope"


def test_non_json_error_body_is_handled() -> None:
    recorder = Recorder(502, "Bad Gateway")
    with pytest.raises(TokenRequestError) as info:
        client_with(recorder).client_credentials("a", "s")
    assert info.value.error == "request_failed"
    assert "Bad Gateway" in info.value.description


def test_authorization_code_sends_pkce_verifier() -> None:
    recorder = Recorder(body={"access_token": "a", "id_token": "i", "refresh_token": "r"})
    tokens = client_with(recorder).authorization_code(
        "hospital-ui", "secret", "the-code", "http://localhost:8000/auth/callback", "verifier"
    )
    assert recorder.form == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "http://localhost:8000/auth/callback",
        "code_verifier": "verifier",
    }
    assert recorder.basic_auth == ("hospital-ui", "secret")
    assert (tokens.id_token, tokens.refresh_token) == ("i", "r")


def test_refresh_sends_refresh_token() -> None:
    recorder = Recorder()
    client_with(recorder).refresh("hospital-ui", "secret", "old-refresh")
    assert recorder.form == {"grant_type": "refresh_token", "refresh_token": "old-refresh"}
