from hacknc.common.auth.models import TokenClaims, TokenSet, peek_claims


def test_token_set_from_keycloak_response() -> None:
    tokens = TokenSet.from_response(
        {"access_token": "abc", "token_type": "Bearer", "expires_in": 900, "scope": "a b"}
    )
    assert tokens.access_token == "abc"
    assert tokens.expires_in == 900
    assert tokens.refresh_token is None


def test_claims_expose_identity_and_permissions() -> None:
    claims = TokenClaims(
        {
            "sub": "u1",
            "preferred_username": "nurse.gawain",
            "azp": "orchestrator",
            "aud": ["summary-agent", "account"],
            "scope": "patient:read profile",
            "realm_access": {"roles": ["nurse"]},
            "exp": 123,
        }
    )
    assert claims.username == "nurse.gawain"
    assert claims.authorized_party == "orchestrator"
    assert claims.audience == ["summary-agent", "account"]
    assert claims.scopes == {"patient:read", "profile"}
    assert claims.realm_roles == {"nurse"}


def test_single_string_audience_is_normalised_to_list() -> None:
    assert TokenClaims({"aud": "ehr-mcp"}).audience == ["ehr-mcp"]


def test_missing_claims_have_safe_defaults() -> None:
    claims = TokenClaims({})
    assert claims.scopes == set()
    assert claims.realm_roles == set()
    assert claims.audience == []


def test_peek_claims_decodes_without_verification(make_token) -> None:
    assert peek_claims(make_token(preferred_username="bill.clerk")).username == "bill.clerk"
