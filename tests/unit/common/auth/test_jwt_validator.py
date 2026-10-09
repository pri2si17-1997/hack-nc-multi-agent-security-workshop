from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from hacknc.common.auth.errors import InsufficientScopeError, TokenValidationError
from hacknc.common.auth.jwt_validator import JwtValidator
from tests.conftest import ISSUER


@pytest.fixture
def validator(key_provider) -> JwtValidator:
    return JwtValidator(ISSUER, "summary-agent", key_provider=key_provider)


def test_valid_token_returns_claims(validator, make_token) -> None:
    claims = validator.validate(make_token(), required_scopes=["patient:read"])
    assert claims.username == "dr.morgana"
    assert claims.authorized_party == "orchestrator"


def test_audience_list_containing_service_is_accepted(validator, make_token) -> None:
    validator.validate(make_token(aud=["account", "summary-agent"]))


def test_token_for_another_service_is_rejected(validator, make_token) -> None:
    with pytest.raises(TokenValidationError, match="not addressed to 'summary-agent'"):
        validator.validate(make_token(aud="billing-agent"))


def test_expired_token_is_rejected(validator, make_token) -> None:
    with pytest.raises(TokenValidationError, match="expired"):
        validator.validate(make_token(exp=int(time.time()) - 3600))


def test_foreign_issuer_is_rejected(validator, make_token) -> None:
    with pytest.raises(TokenValidationError, match="not issued by our Keycloak"):
        validator.validate(make_token(iss="http://evil.example/realms/x"))


def test_token_signed_by_another_key_is_rejected(validator, make_token) -> None:
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(TokenValidationError, match="invalid token"):
        validator.validate(make_token(key=attacker_key))


def test_token_without_subject_is_rejected(validator, make_token) -> None:
    with pytest.raises(TokenValidationError):
        validator.validate(make_token(sub=None))


def test_missing_scope_raises_insufficient_scope(validator, make_token) -> None:
    with pytest.raises(InsufficientScopeError) as info:
        validator.validate(make_token(scope="patient:read"), ["patient:read", "rx:draft"])
    assert info.value.missing == {"rx:draft"}


def test_requires_a_key_source() -> None:
    with pytest.raises(ValueError):
        JwtValidator(ISSUER, "summary-agent")
