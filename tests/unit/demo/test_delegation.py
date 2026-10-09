"""Tests for the walkthrough logic using a fake Keycloak (no network)."""

from __future__ import annotations

import pytest

from hacknc.common.auth import JwtValidator
from hacknc.demo.delegation import (
    DENIED_CASES,
    HAPPY_PATH,
    DeniedCase,
    Hop,
    perform_hop,
    run_chain,
    run_denied_case,
)
from tests.conftest import ISSUER
from tests.fakes import ALLOWED, FakeKeycloak


@pytest.fixture
def user_token(make_token) -> str:
    """What the user gets at login: addressed to the orchestrator."""
    return make_token(aud="orchestrator", azp="workshop-cli", scope="patient:read")


@pytest.fixture
def validators(key_provider):
    return lambda audience: JwtValidator(ISSUER, audience, key_provider=key_provider)


def secrets(client_id: str) -> str:
    return f"{client_id}-secret"


def test_happy_path_grants_every_hop(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, ALLOWED)
    results = run_chain(kc, secrets, validators, user_token, HAPPY_PATH)

    assert [r.granted for r in results] == [True, True]
    assert [r.claims.audience for r in results] == [["summary-agent"], ["ehr-mcp"]]
    assert [r.claims.authorized_party for r in results] == ["orchestrator", "summary-agent"]


def test_each_hop_uses_the_previous_hops_token(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, ALLOWED)
    results = run_chain(kc, secrets, validators, user_token, HAPPY_PATH)

    subject_tokens = [call[2] for call in kc.calls]
    assert subject_tokens[0] == user_token
    assert subject_tokens[1] == results[0].token


def test_chain_stops_at_first_refusal(make_token, validators, user_token) -> None:
    rules = {k: v for k, v in ALLOWED.items() if k != ("summary-agent", "ehr-mcp")}
    kc = FakeKeycloak(make_token, rules)
    results = run_chain(kc, secrets, validators, user_token, HAPPY_PATH)

    assert [r.granted for r in results] == [True, False]
    assert "Keycloak refused" in results[1].reason


def test_missing_scope_counts_as_denied(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, {("orchestrator", "summary-agent"): set()})
    result = perform_hop(kc, secrets, validators, user_token, HAPPY_PATH[0])

    assert not result.granted
    assert result.reason == "scope not granted: patient:read"
    assert result.claims is not None  # the token existed, it was just too weak


def test_token_with_wrong_audience_is_rejected(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, ALLOWED, wrong_aud=True)
    result = perform_hop(kc, secrets, validators, user_token, HAPPY_PATH[0])

    assert not result.granted
    assert "rejected by summary-agent" in result.reason


def test_all_builtin_denied_cases_are_denied(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, ALLOWED)
    for case in DENIED_CASES:
        assert not run_denied_case(kc, secrets, validators, user_token, case).granted, case.title


def test_denied_case_reports_failed_setup(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, {})
    case = DeniedCase("x", "y", setup=(Hop("orchestrator", "scheduling-agent", ("patient:read",)),),
                      attempt=Hop("scheduling-agent", "ehr-mcp", ("patient:read",)))
    result = run_denied_case(kc, secrets, validators, user_token, case)

    assert not result.granted
    assert result.reason.startswith("setup step denied")


def test_replay_case_uses_user_token_directly(make_token, validators, user_token) -> None:
    kc = FakeKeycloak(make_token, ALLOWED)
    replay = next(c for c in DENIED_CASES if c.use_user_token_directly)

    result = run_denied_case(kc, secrets, validators, user_token, replay)

    assert kc.calls[-1] == ("benefits-agent", "disclosure-mcp", user_token)
    assert "not within the token audience" in result.reason
