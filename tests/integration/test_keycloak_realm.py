"""Checks the real Keycloak realm behaves as designed. Needs `make up` first.

Run with: make test-integration
"""

from __future__ import annotations

import httpx
import jwt
import pytest

from hacknc.common.auth import JwtValidator, KeycloakClient, peek_claims
from hacknc.common.config import client_secret, get_settings
from hacknc.demo.delegation import (
    DENIED_CASES,
    HAPPY_PATH,
    HUMAN_SCOPES,
    LOGIN_CLIENT,
    run_chain,
    run_denied_case,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def kc(settings) -> KeycloakClient:
    client = KeycloakClient(settings.keycloak_url)
    try:
        httpx.get(f"{settings.keycloak_url}/.well-known/openid-configuration", timeout=3)
    except httpx.HTTPError:
        pytest.skip("Keycloak is not reachable; run `make up` first")
    return client


@pytest.fixture(scope="module")
def validators(kc, settings):
    keys = jwt.PyJWKClient(kc.jwks_uri)
    return lambda audience: JwtValidator(settings.keycloak_issuer, audience, key_provider=keys)


def login(kc: KeycloakClient, settings, username: str) -> str:
    password = settings.demo_user_password.get_secret_value()
    return kc.password_login(LOGIN_CLIENT, username, password, HUMAN_SCOPES).access_token


@pytest.mark.parametrize(
    ("username", "expected_scopes"),
    [
        ("dr.morgana", {"agent:delegate", "patient:read", "discharge:order", "rx:approve"}),
        ("nurse.gawain", {"agent:delegate", "patient:read"}),
        ("admin.percival", set()),
    ],
)
def test_user_token_scopes_follow_roles(kc, settings, username, expected_scopes) -> None:
    claims = peek_claims(login(kc, settings, username))
    assert set(HUMAN_SCOPES) & claims.scopes == expected_scopes
    assert "orchestrator" in claims.audience


@pytest.mark.parametrize("username", ["dr.morgana", "nurse.gawain"])
def test_clinicians_can_walk_the_whole_chain(kc, settings, validators, username) -> None:
    results = run_chain(kc, client_secret, validators, login(kc, settings, username), HAPPY_PATH)

    assert [r.granted for r in results] == [True] * len(HAPPY_PATH), [r.reason for r in results]
    assert all(r.claims.username == username for r in results)
    assert [r.claims.authorized_party for r in results] == [h.requester for h in HAPPY_PATH]


def test_admin_cannot_start_a_clinical_chain(kc, settings, validators) -> None:
    results = run_chain(kc, client_secret, validators, login(kc, settings, "admin.percival"),
                        HAPPY_PATH)
    assert not results[0].granted


@pytest.mark.parametrize("case", DENIED_CASES, ids=lambda c: c.title)
def test_forbidden_requests_are_denied(kc, settings, validators, case) -> None:
    result = run_denied_case(kc, client_secret, validators, login(kc, settings, "dr.morgana"), case)
    assert not result.granted, result.reason
